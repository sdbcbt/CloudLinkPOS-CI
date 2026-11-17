"""同步正式 GHCR 镜像至国内 CF/R2，保留摘要、部署保护及访问证据。"""
import hashlib
import base64
import re
import sys
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

spec = importlib.util.spec_from_file_location('ghcr_publish', Path(__file__).with_name('ghcr-publish.py'))
PILOT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PILOT)
ORIGIN = 'https://registry.cloudlinkpos.com'
STAGE = 'RECEIPT'


def publication_token():
    """每次管理请求重新申请短期身份，避免上传期间令牌过期。"""
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.actions.githubusercontent.com') or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('OIDC_ENDPOINT_INVALID')
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if key != 'audience']
    query.append(('audience', 'cloudlinkpos-registry'))
    url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    headers = {'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'], 'User-Agent': 'cloudlinkpos-registry/1.0'}
    try:
        with urllib.request.build_opener(PILOT.NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            token = json.loads(response.read(32000))['value']
    except urllib.error.HTTPError as error:
        raise ValueError('OIDC_HTTP_' + str(error.code)) from None
    if not isinstance(token, str) or not 20 < len(token) < 16000:
        raise ValueError('OIDC_TOKEN_INVALID')
    # 本地仅预检固定声明；可信签名及 JWKS 验证由 Worker 完成。
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        expected = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'cloudlinkpos-registry', 'repository': 'sdbcbt/CloudLinkPOS-CI', 'repository_id': '1446734771', 'repository_owner_id': '286992896', 'ref': 'refs/heads/main', 'event_name': 'workflow_dispatch', 'runner_environment': 'github-hosted', 'actor': 'cloudlinkpos-ci-notifier[bot]'}
        workflow = claims.get('workflow_ref')
        callers = ['sdbcbt/CloudLinkPOS-CI/.github/workflows/' + name + '@refs/heads/main' for name in ('private-main.yml', 'full-version-build.yml')]
        if any(claims.get(key) != value for key, value in expected.items()) or workflow not in callers or claims.get('sub') not in ('repo:sdbcbt/CloudLinkPOS-CI:ref:refs/heads/main', 'repo:sdbcbt@286992896/CloudLinkPOS-CI@1446734771:ref:refs/heads/main'):
            raise ValueError('OIDC_CLAIMS_INVALID')
        if claims.get('job_workflow_ref', callers[1]) != callers[1]:
            raise ValueError('OIDC_WORKFLOW_INVALID')
    except (ValueError, IndexError, KeyError):
        raise ValueError('OIDC_CLAIMS_INVALID') from None
    return token


def cf(path, value, method='POST', tag=None):
    headers = {'Authorization': 'Bearer ' + publication_token(), 'User-Agent': 'cloudlinkpos-registry/1.0'}
    if value is None:
        data = None
    elif isinstance(value, dict):
        data = json.dumps(value).encode()
        headers['Content-Type'] = 'application/json'
    else:
        data = value.open('rb')
        headers['Content-Length'] = str(value.stat().st_size)
        headers['X-Registry-Tag'] = tag
        headers['Content-Type'] = 'application/octet-stream'
    try:
        request = urllib.request.Request(ORIGIN + '/_registry' + path, data=data, method=method, headers=headers)
        with urllib.request.build_opener(PILOT.NoRedirect()).open(request, timeout=120) as response:
            return json.loads(response.read(200_000))
    except urllib.error.HTTPError as error:
        reason = error.headers.get('x-registry-auth-reason', '')
        suffix = '_' + reason if reason in ('FORMAT', 'HEADER', 'TIME', 'CLAIMS', 'KEY_FETCH', 'KEY_ID', 'KEY_IMPORT', 'SIGNATURE', 'CREDENTIALS') else ''
        raise ValueError('CF_HTTP_' + str(error.code) + suffix) from None
    finally:
        if hasattr(data, 'close'):
            data.close()


def download_blob(descriptor, destination, bearer):
    url = 'https://ghcr.io/v2/' + PILOT.REPOSITORY + '/blobs/' + descriptor['digest']
    opener = urllib.request.build_opener(PILOT.NoRedirect())
    for _ in range(5):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('ghcr.io', 'pkg-containers.githubusercontent.com') or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError('UNTRUSTED_BLOB_REDIRECT')
        headers = {'Authorization': 'Bearer ' + bearer} if parsed.hostname == 'ghcr.io' else {}
        try:
            response = opener.open(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get('Location'):
                url = urllib.parse.urljoin(url, error.headers['Location'])
                continue
            raise ValueError('GHCR_BLOB_DOWNLOAD_FAILED') from None
        digest, size = hashlib.sha256(), 0
        with response, destination.open('wb') as stream:
            while block := response.read(128 * 1024):
                size += len(block)
                if size > descriptor['size']:
                    raise ValueError('GHCR_BLOB_SIZE_MISMATCH')
                digest.update(block)
                stream.write(block)
        if size != descriptor['size'] or 'sha256:' + digest.hexdigest() != descriptor['digest']:
            raise ValueError('GHCR_BLOB_DIGEST_MISMATCH')
        return
    raise ValueError('GHCR_BLOB_REDIRECT_LIMIT')


def mirror(service, tag, digest, raw, refresh_deployment):
    """先保护当前部署，再上传原始 GHCR 内容，所有证据验证通过后才返回收据。"""
    client = PILOT.Registry()
    if client.verify_image(tag, digest) != raw:
        raise ValueError('GHCR_MANIFEST_MISMATCH')
    # 网络读取失败必须终止，不能拿 checkout 时的旧清单替代保护范围。
    deployed = refresh_deployment()
    cf('/pins/add', {'repository': PILOT.REPOSITORY, 'digests': deployed, 'ttlSeconds': 172800, 'operationId': 'ci-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']})
    plan = cf('/prepare', {'service': service, 'tag': tag, 'manifest': raw.decode()})
    try:
        if plan.get('digest') != digest:
            raise ValueError('CF_DIGEST_MISMATCH')
        # 即使 commit 后至收据消费前发生 GC，也不得删除本次版本。
        cf('/pins/add', {'repository': PILOT.REPOSITORY, 'digests': [digest], 'ttlSeconds': 172800, 'operationId': 'ci-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']})
        transferred = 0
        with tempfile.TemporaryDirectory() as directory:
            for descriptor in plan['missing']:
                file = Path(directory) / 'blob'
                download_blob(descriptor, file, PILOT.token())
                cf('/blob/' + descriptor['digest'], file, method='PUT', tag=tag)
                transferred += descriptor['size']
                file.unlink()
        result = cf('/commit', {'tag': tag, 'digest': digest})
        if result.get('digest') != digest:
            raise ValueError('CF_COMMIT_MISMATCH')
        evidence = cf('/verify', {'tag': tag, 'digest': digest, 'latest': service + '-latest'})
        if evidence.get('verified') is not True or evidence.get('blobDigestsVerified') is not True or evidence.get('repository') != PILOT.REPOSITORY or evidence.get('tag') != tag or evidence.get('digest') != digest or evidence.get('latest') != service + '-latest':
            raise ValueError('CF_PRIVATE_EVIDENCE_INVALID')
        status, _, _ = PILOT.request(ORIGIN + '/v2/' + PILOT.REPOSITORY + '/manifests/' + digest, {'Accept': PILOT.ACCEPT})
        if status != 401:
            raise ValueError('CF_ANONYMOUS_READ_NOT_DENIED')
        return {'authenticated_read': True, 'anonymous_pull_denied': True, 'image_digest': digest}
    except Exception:
        # 尽力释放本次待发布预留；清理失败不得覆盖原始发布错误。
        try:
            cf('/abort', {'tag': tag})
        except Exception:
            pass
        raise


def preflight():
    """编译前只读核验正式分发入口身份和当前容量，不创建发布或保护状态。"""
    result = cf('/status', None, method='GET')
    budget, used, reserved = (result.get(key) for key in ('budgetBytes', 'usedBytes', 'reservedBytes'))
    if not all(type(value) is int for value in (budget, used, reserved)) or not 0 < budget <= 8000000000 or used < 0 or reserved < 0 or used + reserved >= budget:
        raise ValueError('CF_CAPACITY_PREFLIGHT_FAILED')
    print('CF 短期身份及容量预检通过；预算 ' + str(budget) + ' 字节，已用 ' + str(used) + ' 字节，预留 ' + str(reserved) + ' 字节。', flush=True)


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['--preflight']:
            raise ValueError('REGISTRY_ARGUMENTS_INVALID')
        preflight()
    except Exception as error:
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]{3,80}', str(error)) else 'PREFLIGHT_FAILED'
        print('::error::正式分发入口预检失败：' + category, flush=True)
        raise SystemExit(1)
