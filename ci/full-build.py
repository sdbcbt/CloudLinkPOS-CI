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
import tempfile
import urllib.parse

ROOT = Path('source').resolve()
REGISTRY = 'registry.cn-zhangjiakou.aliyuncs.com'
IMAGE = REGISTRY + '/sdbcbt/cloudlinkpos'
STAGE = '初始化'


def run(args, cwd=None, env=None, data=None):
    result = subprocess.run(args, cwd=cwd, env=env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('阶段失败')
    return result.stdout


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def checkout():
    sha = os.environ['SOURCE_SHA']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('提交格式无效')
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


def next_version(service, tags):
    pattern = re.compile(re.escape(service) + r'-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
    versions = [tuple(int(v) for v in match.groups()) for tag in tags if (match := pattern.fullmatch(tag))]
    major, minor, patch = max(versions, default=(0, 1, 0))
    return f'{service}-v{major}.{minor}.{patch + 1}'


def all_tags(client):
    result, last = set(), ''
    for _ in range(1000):
        query = urllib.parse.urlencode({'n': 1000, **({'last': last} if last else {})})
        code, _, body = client.authenticated(f'/v2/{client.repository}/tags/list?{query}')
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


def build(service):
    global STAGE
    publisher = module('acr_publish', ROOT / 'deploy/scripts/publish-acr-image.py')
    client = publisher.Registry(REGISTRY, 'sdbcbt', 'cloudlinkpos', os.environ['ACR_USERNAME'], os.environ['ACR_PASSWORD'])
    STAGE = '验证私有仓库与正式版本'
    print(STAGE, flush=True)
    client.check_private_existing()
    version = next_version(service, all_tags(client))
    client.reject_existing_tags([version])
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        base_out = folder / 'base.out'
        profile = 'nginx' if service == 'admin' else 'alpine'
        run([sys.executable, 'deploy/scripts/pull-ci-base-image.py', '--profile', profile, '--github-output', str(base_out)], ROOT)
        base = dict(line.split('=', 1) for line in base_out.read_text().splitlines())['base_image']
        if service == 'admin':
            STAGE = '构建管理后台'
            print(STAGE, flush=True)
            run([sys.executable, 'deploy/scripts/prepare-admin-ci-environment.py'], ROOT)
            run(['pnpm', '--dir', 'apps/admin', 'install', '--frozen-lockfile', '--registry=https://registry.npmjs.org', '--fetch-timeout=120000', '--fetch-retries=2'], ROOT)
            run(['pnpm', '--dir', 'apps/admin', 'run', 'build:antd'], ROOT)
            script, extra = 'prepare-admin-ci-context.py', []
        else:
            script, extra = 'prepare-service-ci-context.py', ['--service', service]
        STAGE = '准备经过校验的构建上下文'
        print(STAGE, flush=True)
        output = folder / 'build'
        run([sys.executable, 'deploy/scripts/' + script, '--repo-root', str(ROOT), '--output-dir', str(output), '--release-tag', version, '--release-source', 'manual', '--source-sha', os.environ['SOURCE_SHA'], '--build-number', os.environ['GITHUB_RUN_NUMBER'], '--run-attempt', os.environ['GITHUB_RUN_ATTEMPT'], '--image-repository', IMAGE, '--base-image', base, *extra], ROOT)
        metadata = json.loads((output / 'release.json').read_text())
        STAGE = '编译正式镜像'
        print(STAGE, flush=True)
        run(['docker', 'build', '--platform', 'linux/amd64', '-t', 'local-fullbuild:build', str(output / 'context')])
        # 仅将版本号推送到注册表，不使用上下文 SHA 标签。
        client.reject_existing_tags([version])
        env = dict(os.environ, DOCKER_CONFIG=str(folder / 'docker'))
        run(['docker', 'login', REGISTRY, '-u', os.environ['ACR_USERNAME'], '--password-stdin'], env=env, data=os.environ['ACR_PASSWORD'].encode())
        try:
            STAGE = '发布并回读固定版本'
            print(STAGE, flush=True)
            run(['docker', 'tag', 'local-fullbuild:build', IMAGE + ':' + version], env=env)
            log = run(['docker', 'push', IMAGE + ':' + version], env=env).decode()
            found = re.findall(r'digest: (sha256:[0-9a-f]{64})', log)
            if not found:
                raise ValueError('缺少推送摘要')
            digest = found[-1]
            client.verify_image(version, digest)
            evidence = client.prove_private(digest)
            STAGE = '更新并核验 latest'
            print(STAGE, flush=True)
            run(['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false', '-t', IMAGE + ':' + service + '-latest', IMAGE + '@' + digest], env=env)
            client.verify_image(service + '-latest', digest)
        finally:
            run(['docker', 'logout', REGISTRY], env=env)
        receipt = {'schema': 'cloudlinkpos.public-full-build/v1', 'sourceRepository': 'sdbcbt/CloudLinkPOS', 'sourceSHA': os.environ['SOURCE_SHA'], 'buildRepository': os.environ['GITHUB_REPOSITORY'], 'workflowSHA': os.environ['GITHUB_SHA'], 'runID': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'service': service, 'repository': IMAGE, 'tag': version, 'digest': digest, 'privateEvidence': evidence, 'tagPolicy': 'version-and-latest-v1', 'metadataSHA256': hashlib.sha256((output / 'release.json').read_bytes()).hexdigest()}
        dest = Path('receipts')
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
        if selected != 'all':
            requested = selected.split(',')
            if not requested or any(name not in names for name in requested) or len(set(requested)) != len(requested):
                raise ValueError('服务选择无效')
            names = requested
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write('matrix=' + json.dumps({'include': [{'service': name} for name in names]}) + '\n')
    else:
        build(os.environ['BUILD_SERVICE'])


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('::error::全量构建停止于：' + STAGE, file=sys.stderr)
        sys.exit(1)
