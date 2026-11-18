"""读取固定私库提交并构建正式版本，仅发布版本号与 latest 标签。"""
import base64
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import shutil
import tempfile
import time
import urllib.parse

ROOT = Path('source').resolve()
REGISTRY = 'ghcr.io'
ORIGIN_IMAGE = REGISTRY + '/sdbcbt/cloudlinkpos'
IMAGE = 'registry.cloudlinkpos.com/sdbcbt/cloudlinkpos'
STAGE = '初始化'
MANIFEST = 'deploy/helm/cloudlinkpos/fleet/rancher-images.yaml'
# Fleet 专用部署分支：受管镜像清单迁移后只在此分支更新；名称表明该分支不能删除。
DEPLOY_BRANCH = 'fleet-deploy-do-not-delete'
# checkout 把部署分支清单另存在源码目录之外，plan 读取；文件不存在表示部署分支尚未建立。
DEPLOY_MANIFEST = ROOT.parent / 'fleet-deploy-manifest.yaml'


# 推送前的网络步骤（读源码、读私库标签、拉基础镜像、编译）遇到偶发网络故障时重试。
RETRY_ATTEMPTS = 2  # 总共最多 2 次：失败后只再试 1 次
RETRY_WAIT_SECONDS = 30
# GHCR 原始镜像只在私有包发布，CF/R2 完整核验后才写正式收据。
SLEEP = time.sleep


def retry(stage, action, retryable=(Exception,), attempts=None):
    attempts = attempts or RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except retryable:
            if attempt == attempts:
                raise
            print('::warning::' + stage + '失败，第 ' + str(attempt) + ' 次重试', flush=True)
            SLEEP(RETRY_WAIT_SECONDS * attempt)


def run(args, cwd=None, env=None, data=None, timeout=None, show_error=False, private_admin=False):
    """执行命令；show_error=True 时失败会打印输出末尾，便于判断网络、认证还是限流（不含凭据的命令才可开启）。"""
    try:
        result = subprocess.run(args, cwd=cwd, env=env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if private_admin:
            save_admin_failure(error.stdout, error.stderr)
        if show_error:
            tail(error.stdout, error.stderr)
        raise RuntimeError('阶段超时')
    if result.returncode:
        if private_admin:
            save_admin_failure(result.stdout, result.stderr)
        if show_error:
            tail(result.stdout, result.stderr)
        raise RuntimeError('阶段失败')
    return result.stdout


def save_admin_failure(stdout, stderr):
    """将后台失败输出仅保存在执行器私有临时文件，禁止向公开日志泄露私库内容。"""
    content = (stdout or b'') + b'\n' + (stderr or b'')
    target = Path(os.environ['RUNNER_TEMP']) / 'cloudlinkpos-admin-first-failure.log'
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
    except FileExistsError:
        pass
    print('后台构建失败；具体输出仅保存于私有临时日志，并由后续步骤回写私库检查。', flush=True)


def tail(stdout, stderr, lines=20):
    text = ((stdout or b'') + b'\n' + (stderr or b'')).decode(errors='replace').strip().splitlines()
    for line in text[-lines:]:
        print('  | ' + line, file=sys.stderr, flush=True)


def push_image(client, version, folder, env):
    """只向 GHCR 推送原始镜像，不输出可能含私有内容的 Docker 响应。"""
    reference = ORIGIN_IMAGE + ':' + version
    run(['docker', 'tag', 'local-fullbuild:build', reference], env=env)
    run(['docker', 'push', reference], env=env, timeout=20 * 60)
    values = json.loads(run(['docker', 'image', 'inspect', reference, '--format', '{{json .RepoDigests}}'], env=env))
    digests = [value.split('@', 1)[1] for value in values if value.startswith(ORIGIN_IMAGE + '@')]
    if len(digests) != 1 or not re.fullmatch('sha256:[0-9a-f]{64}', digests[0]):
        raise ValueError('GHCR_PUSH_DIGEST_INVALID')
    return digests[0]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def checkout():
    """读取源码；只对 git 网络失败（RuntimeError）重试，提交不属于主线等确定性错误直接停止。"""
    if not re.fullmatch('[0-9a-f]{40}', os.environ['SOURCE_SHA']):
        raise ValueError('提交格式无效')

    def attempt():
        # 重试前清理上一次留下的半成品目录。
        shutil.rmtree(ROOT, ignore_errors=True)
        if DEPLOY_MANIFEST.exists():
            DEPLOY_MANIFEST.unlink()
        checkout_once()
    retry('读取私库源码', attempt, (RuntimeError,))


def checkout_once():
    sha = os.environ['SOURCE_SHA']
    ROOT.mkdir()
    run(['git', 'init', '--quiet', str(ROOT)])
    run(['git', 'remote', 'add', 'origin', 'https://github.com/sdbcbt/CloudLinkPOS.git'], ROOT)
    token = os.environ['SOURCE_TOKEN']
    header = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env = dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader', GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + header)
    run(['git', 'fetch', '--quiet', '--no-tags', 'origin', 'refs/heads/main'], ROOT, env)
    history = run(['git', 'rev-list', '--first-parent', 'FETCH_HEAD'], ROOT).decode().splitlines()
    if sha not in history:
        raise ValueError('提交不属于主线')
    run(['git', 'checkout', '--quiet', '--detach', sha], ROOT)
    fetch_deploy_manifest(env)


def fetch_deploy_manifest(env):
    """读取 Fleet 部署分支上的受管清单。

    只有 ls-remote 成功且确认分支不存在时才沿用主线清单；网络、认证等读取失败直接停止，
    不能把它们当作尚未迁移，否则迁移后会绑定冻结主线的旧策略代次。
    部署分支在独立的临时仓库中浅拉取，不改变源码仓库的浅克隆边界与主线历史核验。
    """
    remote = 'https://github.com/sdbcbt/CloudLinkPOS.git'
    refs = run(['git', 'ls-remote', '--heads', remote, 'refs/heads/' + DEPLOY_BRANCH], ROOT, env).decode().split()
    if not refs:
        print('::warning::Fleet 部署分支尚未建立，发布策略使用主线清单')
        return
    if not re.fullmatch('[0-9a-f]{40}', refs[0]):
        raise ValueError('部署分支引用无效')
    with tempfile.TemporaryDirectory() as directory:
        run(['git', 'init', '--quiet', directory])
        run(['git', 'fetch', '--quiet', '--no-tags', '--depth=1', remote, refs[0]], directory, env)
        DEPLOY_MANIFEST.write_bytes(run(['git', 'show', 'FETCH_HEAD:' + MANIFEST], directory))


def manifest_policy(content):
    manifest = json.loads('\n'.join(row for row in content.splitlines() if not row.lstrip().startswith('#')))
    policy = manifest.get('releasePolicy')
    if not isinstance(policy, dict) or type(policy.get('grayRequired')) is not bool or type(policy.get('generation')) is not int or policy['generation'] < 1:
        raise ValueError('自动发布策略未初始化')
    return policy


def release_policy(main_content, deploy_content):
    """选取代次更高的发布策略。

    受管清单迁到部署分支前，主线是权威、部署分支由 grayconfig-api 滞后同步；迁移后部署分支是权威、主线清单冻结。
    策略每次修改都会递增 generation，取较高代次即为当前权威策略，迁移前后都不需要额外开关。
    """
    policy = manifest_policy(main_content)
    if deploy_content is not None:
        deployed = manifest_policy(deploy_content)
        if deployed['generation'] > policy['generation']:
            policy = deployed
    return policy


def deploy_manifest():
    if not DEPLOY_MANIFEST.exists():
        return None
    return DEPLOY_MANIFEST.read_text()


def next_version(service, tags, floors=()):
    pattern = re.compile(re.escape(service) + r'-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
    versions = [tuple(int(v) for v in match.groups()) for tag in list(tags) + list(floors) if (match := pattern.fullmatch(tag))]
    major, minor, patch = max(versions, default=(0, 1, 0))
    return f'{service}-v{major}.{minor}.{patch + 1}'


def all_tags(client):
    result, last = set(), ''
    for _ in range(1000):
        query = urllib.parse.urlencode({'n': 1000, **({'last': last} if last else {})})
        code, _, body = client.authenticated(f'/v2/{client.repository}/tags/list?{query}')
        if code == 404 and not last:
            return result
        value = json.loads(body)
        tags = value.get('tags') or []
        if code != 200 or value.get('name') != client.repository or not isinstance(tags, list):
            raise ValueError('仓库标签读取未核验')
        if not tags:
            return result
        if tags[-1] == last:
            raise ValueError('仓库分页停滞')
        result.update(tags)
        last = tags[-1]
    raise ValueError('仓库分页超出上限')



def fleet_records(content):
    """读取受管部署清单中的全部版本对象，涵盖正式与灰度状态。"""
    manifest = json.loads('\n'.join(row for row in content.splitlines() if not row.lstrip().startswith('#')))
    records = []
    def visit(value):
        if isinstance(value, dict):
            if 'repository' in value and 'tag' in value and 'digest' in value:
                if not re.fullmatch('sha256:[0-9a-f]{64}', value['digest']):
                    raise ValueError('FLEET_DIGEST_INVALID')
                records.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(manifest)
    if not records:
        raise ValueError('FLEET_DEPLOYMENT_RECORDS_MISSING')
    return records


def fresh_fleet_records():
    """在发布保护前重读 Fleet 分支，认证失败和缺失不得回退旧快照。"""
    credential = base64.b64encode(('x-access-token:' + os.environ['SOURCE_TOKEN']).encode()).decode()
    env = dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader', GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + credential)
    if DEPLOY_MANIFEST.exists():
        DEPLOY_MANIFEST.unlink()
    fetch_deploy_manifest(env)
    if not DEPLOY_MANIFEST.exists():
        raise ValueError('FLEET_DEPLOY_BRANCH_REQUIRED')
    return fleet_records(DEPLOY_MANIFEST.read_text())


def baseline_floors():
    """只读取输入源码提交中的受控迁移版本基线，不信任可变工作目录。"""
    sha = os.environ['SOURCE_SHA']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('BASELINE_SOURCE_SHA_INVALID')
    path = 'deploy/ci/registry-version-baseline.json'
    tracked = run(['git', 'ls-tree', '--name-only', sha, '--', path], ROOT).decode().splitlines()
    if not tracked:
        return {}
    if tracked != [path]:
        raise ValueError('BASELINE_PATH_INVALID')
    value = json.loads(run(['git', 'show', sha + ':' + path], ROOT))
    services = value.get('services')
    if value.get('schema') != 'cloudlinkpos.registry-baseline/v1' or not isinstance(services, dict):
        raise ValueError('BASELINE_SCHEMA_INVALID')
    for service, tag in services.items():
        if not re.fullmatch('[a-z][a-z0-9]*(?:-[a-z0-9]+)*', service) or not isinstance(tag, str) or not re.fullmatch(re.escape(service) + r'-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag):
            raise ValueError('BASELINE_VERSION_INVALID')
    return services


def stamp_admin_version(output, version):
    """将已验证的手动构建上下文标记为注册表分配版本，并重新计算上下文证据。"""
    if not re.fullmatch(r'admin-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('后台正式版本无效')
    path = output / 'release.json'
    metadata = json.loads(path.read_text())
    dockerfile = output / 'context/Dockerfile'
    content = dockerfile.read_text()
    old = 'LABEL io.cloudlinkpos.release-tag="' + metadata['release_tag'] + '"'
    if content.count(old) != 1 or metadata['release_source'] != 'manual' or metadata['git_tag']:
        raise ValueError('后台手动构建标记不符')
    dockerfile.write_text(content.replace(old, 'LABEL io.cloudlinkpos.release-tag="' + version + '"'))
    artifacts = output / 'context/artifacts'
    archive = run([sys.executable, str(ROOT / 'deploy/scripts/normalized-context-tar.py'), 'artifacts=' + str(artifacts), 'Dockerfile=' + str(dockerfile)])
    (output / 'context.tar').write_bytes(archive)
    metadata.update(release_tag=version, context_sha256=hashlib.sha256(archive).hexdigest(), tag=version, image=IMAGE + ':' + version, aliases=[IMAGE + ':' + version], tag_policy='version-and-latest-v1')
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')


def build(service):
    global STAGE
    publisher = module('ghcr_publish', Path(__file__).with_name('ghcr-publish.py'))
    client = publisher.Registry()
    STAGE = '验证私有仓库与正式版本'
    print(STAGE, flush=True)

    def resolve_version():
        client.check_private_existing()
        records = fresh_fleet_records()
        floors = [record['tag'] for record in records if record['tag'].startswith(service + '-v')]
        baseline = baseline_floors().get(service)
        if baseline:
            floors.append(baseline)
        tags = all_tags(client)
        if not floors and not any(tag.startswith(service + '-v') for tag in tags):
            raise ValueError('SERVICE_VERSION_FLOOR_REQUIRED')
        candidate = next_version(service, tags, floors)
        client.reject_existing_tags([candidate])
        return candidate
    # 只读核验与版本计算，网络超时可安全重试。
    version = retry(STAGE, resolve_version)
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        base_out = folder / 'base.out'
        profile = 'nginx' if service == 'admin' else 'alpine'
        STAGE = '拉取并核验固定官方基础镜像'
        print(STAGE, flush=True)
        base_helper = module('public_base_image', Path(__file__).with_name('pull-ci-base-image.py'))
        base_helper.prepare(ROOT, profile, base_out)
        base = dict(line.split('=', 1) for line in base_out.read_text().splitlines())['base_image']
        if service == 'admin':
            STAGE = '构建管理后台'
            print(STAGE, flush=True)
            # 托管 Runner 直接访问官方源，不运行自建主机的强制代理初始化。
            run(['pnpm', '--dir', 'apps/admin', 'install', '--frozen-lockfile', '--registry=https://registry.npmjs.org', '--fetch-timeout=120000', '--fetch-retries=2'], ROOT, private_admin=True)
            STAGE = '编译管理后台页面'
            print(STAGE, flush=True)
            run(['pnpm', '--dir', 'apps/admin', 'run', 'build:antd'], ROOT, private_admin=True)
            script, extra = 'prepare-admin-ci-context.py', []
        else:
            script, extra = 'prepare-service-ci-context.py', ['--service', service]
        STAGE = '准备经过校验的构建上下文'
        print(STAGE, flush=True)
        output = folder / 'build'
        run([sys.executable, 'deploy/scripts/' + script, '--repo-root', str(ROOT), '--output-dir', str(output), '--release-tag', '' if service == 'admin' else version, '--release-source', 'manual', '--source-sha', os.environ['SOURCE_SHA'], '--build-number', os.environ['GITHUB_RUN_NUMBER'], '--run-attempt', os.environ['GITHUB_RUN_ATTEMPT'], '--image-repository', IMAGE, '--base-image', base, *extra], ROOT)
        if service == 'admin':
            stamp_admin_version(output, version)
        metadata = json.loads((output / 'release.json').read_text())
        STAGE = '编译正式镜像'
        print(STAGE, flush=True)
        retry(STAGE, lambda: run(['docker', 'build', '--platform', 'linux/amd64', '-t', 'local-fullbuild:build', str(output / 'context')]))
        # 仅将版本号推送到注册表，不使用上下文 SHA 标签。
        client.reject_existing_tags([version])
        env = dict(os.environ, DOCKER_CONFIG=str(folder / 'docker'))
        run(['docker', 'login', REGISTRY, '-u', os.environ['GITHUB_ACTOR'], '--password-stdin'], env=env, data=os.environ['GITHUB_TOKEN'].encode())
        try:
            STAGE = '发布并回读固定版本'
            print(STAGE, flush=True)
            digest = push_image(client, version, folder, env)
            client.verify_image(version, digest)
            origin_evidence = client.prove_private(digest)
        finally:
            run(['docker', 'logout', REGISTRY], env=env)
        receipt = {'schema': 'cloudlinkpos.public-full-build/v1', 'sourceRepository': 'sdbcbt/CloudLinkPOS', 'sourceSHA': os.environ['SOURCE_SHA'], 'buildRepository': os.environ['GITHUB_REPOSITORY'], 'workflowSHA': os.environ['GITHUB_SHA'], 'runID': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'service': service, 'repository': IMAGE, 'tag': version, 'digest': digest, 'origin': {'repository': ORIGIN_IMAGE, 'digest': digest, 'privateEvidence': origin_evidence}, 'tagPolicy': 'version-and-latest-v1', 'metadataSHA256': hashlib.sha256((output / 'release.json').read_bytes()).hexdigest()}
        dest = Path('candidates')
        dest.mkdir(exist_ok=True)
        (dest / (service + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
        print(service + ' ' + version + ' ' + digest)


def main():
    global STAGE
    if sys.argv[1] == 'checkout':
        checkout()
    elif sys.argv[1] == 'plan':
        catalog = module('source_catalog', ROOT / 'deploy/scripts/prepare-service-ci-context.py')
        names = sorted(catalog.SERVICES) + ['admin']
        selected = os.environ.get('SOURCE_SERVICES', 'all')
        before = os.environ.get('SOURCE_BEFORE_SHA', '')
        if selected == 'changed':
            if not re.fullmatch('[0-9a-f]{40}', before) or set(before) == {'0'}:
                raise ValueError('自动发布需要完整推送基线')
            history = run(['git', 'rev-list', '--first-parent', os.environ['SOURCE_SHA']], ROOT).decode().splitlines()
            if before not in history:
                raise ValueError('自动发布基线不属于主线历史')
            names = catalog.detect_changed_services(ROOT, before, os.environ['SOURCE_SHA'])['services']
            paths = run(['git', 'diff', '--name-only', '--no-renames', before, os.environ['SOURCE_SHA']], ROOT).decode().splitlines()
            admin = module('admin_catalog', ROOT / 'deploy/scripts/prepare-admin-ci-context.py')
            if any(path == entry or path.startswith(entry + '/') for path in paths for entry in admin.ADMIN_SOURCE_PATHS):
                names.append('admin')
            names = sorted(set(names))
        if selected not in ('all', 'changed'):
            requested = selected.split(',')
            if not requested or any(name not in names for name in requested) or len(set(requested)) != len(requested):
                raise ValueError('服务选择无效')
            names = requested
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write('matrix=' + json.dumps({'include': [{'service': name} for name in names]}) + '\n')
            out.write('has_services=' + str(bool(names)).lower() + '\n')
        if os.environ.get('PUBLISH_RECEIPTS') == 'true':
            policy = release_policy((ROOT / MANIFEST).read_text(), deploy_manifest())
            # explicit：补建指定服务，只代表部分服务，不能作为该源码提交的整批 PR 收据。
            plan = {'schema': 'cloudlinkpos.public-source-plan/v1', 'sourceSHA': os.environ['SOURCE_SHA'], 'beforeSHA': before, 'expectedServices': names, 'publicationPolicy': policy, 'selection': 'explicit' if selected not in ('all', 'changed') else selected}
            Path('source-build-plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    else:
        build(os.environ['BUILD_SERVICE'])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # 只输出异常类别与本仓库/发布脚本定义的固定原因文本，便于区分网络、协议与核验失败。
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]{3,80}', str(error)) else 'BUILD_STAGE_FAILED'
        print('::error::全量构建停止于：' + STAGE + '（' + type(error).__name__ + '/' + category + '）', file=sys.stderr)
        sys.exit(1)
