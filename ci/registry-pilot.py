"""在 GitHub 托管执行器构建单个试点服务并验证私有 GHCR 镜像。"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

spec = importlib.util.spec_from_file_location('full_build', Path(__file__).with_name('full-build.py'))
BUILD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUILD)
SERVICE = 'employee-api'
REPOSITORY = 'sdbcbt/cloudlinkpos-registry-pilot'
IMAGE = 'ghcr.io/' + REPOSITORY
ACCEPT = 'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(url, headers):
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return response.status, dict(response.headers), response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, {}, b''


def token(anonymous=False):
    headers = {}
    if not anonymous:
        credential = os.environ['GITHUB_ACTOR'] + ':' + os.environ['GITHUB_TOKEN']
        headers['Authorization'] = 'Basic ' + base64.b64encode(credential.encode()).decode()
    query = urllib.parse.urlencode({'scope': 'repository:' + REPOSITORY + ':pull', 'service': 'ghcr.io'})
    status, _, data = request('https://ghcr.io/token?' + query, headers)
    if anonymous and status in (401, 403):
        return None
    if status != 200:
        raise ValueError('GHCR_TOKEN_FAILED')
    value = json.loads(data)
    return value.get('token') or value['access_token']


def verify(tag, expected):
    anonymous = token(True)
    if anonymous:
        status, _, _ = request('https://ghcr.io/v2/' + REPOSITORY + '/manifests/' + tag, {'Authorization': 'Bearer ' + anonymous, 'Accept': ACCEPT})
        if status not in (401, 403):
            raise ValueError('GHCR_ANONYMOUS_READ_NOT_DENIED')
    status, _, body = request('https://ghcr.io/v2/' + REPOSITORY + '/manifests/' + tag, {'Authorization': 'Bearer ' + token(), 'Accept': ACCEPT})
    digest = 'sha256:' + hashlib.sha256(body).hexdigest()
    if status != 200 or digest != expected:
        raise ValueError('GHCR_DIGEST_MISMATCH')
    manifest = json.loads(body)
    if manifest.get('schemaVersion') != 2 or not isinstance(manifest.get('layers'), list):
        raise ValueError('GHCR_MANIFEST_INVALID')
    return manifest


def identity(env):
    sha = env['SOURCE_SHA']
    run_id, attempt = env['GITHUB_RUN_ID'], env['GITHUB_RUN_ATTEMPT']
    if not re.fullmatch('[0-9a-f]{40}', sha) or not all(re.fullmatch('[1-9][0-9]*', value) for value in (run_id, attempt)):
        raise ValueError('PILOT_IDENTITY_INVALID')
    return sha, 'pilot-' + sha[:12] + '-' + run_id + '-a' + attempt


def check_package_privacy():
    """已有包必须私有，避免推送后才发现包被公开。新包沿用 GHCR 默认私有可见性。"""
    status, _, body = request('https://api.github.com/orgs/sdbcbt/packages/container/cloudlinkpos-registry-pilot', {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'], 'Accept': 'application/vnd.github+json'})
    if status == 200:
        if json.loads(body).get('visibility') != 'private':
            raise ValueError('GHCR_EXISTING_PACKAGE_NOT_PRIVATE')
    elif status != 404:
        raise ValueError('GHCR_PACKAGE_PREFLIGHT_FAILED')


def push_image(reference, environment):
    """只输出固定失败类别，避免公开任意命令输出。"""
    try:
        result = subprocess.run(['docker', 'push', reference], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError('GHCR_PUSH_TIMEOUT') from None
    if result.returncode:
        output = (result.stdout + result.stderr).decode(errors='replace').lower()
        if 'permission_denied' in output or 'permission denied' in output:
            raise ValueError('GHCR_WRITE_PERMISSION_DENIED')
        if 'unauthorized' in output:
            raise ValueError('GHCR_PUSH_UNAUTHORIZED')
        raise ValueError('GHCR_PUSH_FAILED')


def build():
    sha, tag = identity(os.environ)
    check_package_privacy()
    timings = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        base_output = folder / 'base.out'
        BUILD.run(['python3', 'deploy/scripts/pull-ci-base-image.py', '--profile', 'alpine', '--github-output', str(base_output)], BUILD.ROOT)
        base = dict(line.split('=', 1) for line in base_output.read_text().splitlines())['base_image']
        output = folder / 'context'
        command = ['python3', 'deploy/scripts/prepare-service-ci-context.py', '--repo-root', str(BUILD.ROOT), '--output-dir', str(output), '--service', SERVICE, '--release-tag', SERVICE + '-v0.0.' + os.environ['GITHUB_RUN_NUMBER'], '--release-source', 'manual', '--source-sha', sha, '--build-number', os.environ['GITHUB_RUN_NUMBER'], '--run-attempt', os.environ['GITHUB_RUN_ATTEMPT'], '--image-repository', IMAGE, '--base-image', base]
        BUILD.run(command, BUILD.ROOT)
        BUILD.run(['docker', 'build', '--platform', 'linux/amd64', '-t', IMAGE + ':' + tag, str(output / 'context')])
        timings['build_seconds'] = round(time.monotonic() - started, 3)
        print('试点镜像构建完成：' + str(timings['build_seconds']) + ' 秒', flush=True)
        environment = dict(os.environ, DOCKER_CONFIG=str(folder / 'docker-auth'))
        BUILD.run(['docker', 'login', 'ghcr.io', '-u', os.environ['GITHUB_ACTOR'], '--password-stdin'], env=environment, data=os.environ['GITHUB_TOKEN'].encode())
        try:
            started = time.monotonic()
            push_image(IMAGE + ':' + tag, environment)
            timings['push_seconds'] = round(time.monotonic() - started, 3)
            inspect = BUILD.run(['docker', 'image', 'inspect', IMAGE + ':' + tag, '--format', '{{json .RepoDigests}}'])
            digests = [value.split('@', 1)[1] for value in json.loads(inspect) if value.startswith(IMAGE + '@')]
            if len(digests) != 1 or not re.fullmatch('sha256:[0-9a-f]{64}', digests[0]):
                raise ValueError('PILOT_DIGEST_INVALID')
            manifest = verify(tag, digests[0])
        finally:
            BUILD.run(['docker', 'logout', 'ghcr.io'], env=environment)
        receipt = {
            'schema': 'cloudlinkpos.registry-pilot/v1', 'service': SERVICE, 'sourceSHA': sha,
            'workflowSHA': os.environ['GITHUB_SHA'], 'runID': os.environ['GITHUB_RUN_ID'],
            'attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'image': IMAGE, 'tag': tag, 'digest': digests[0],
            'compressed_layer_bytes': sum(layer['size'] for layer in manifest['layers']),
            'authenticated_read': True, 'anonymous_pull_denied': True, 'timings': timings,
        }
        Path('registry-pilot-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        build()
    except Exception as error:
        # 私库源码、编译输出、仓库响应和凭据不进入公开日志。
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]{3,80}', str(error)) else 'PILOT_STAGE_FAILED'
        print('::error::镜像试点失败：' + category + '；未写入正式发布收据，也未修改部署。', flush=True)
        raise SystemExit(1)
