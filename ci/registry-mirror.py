"""同步正式 GHCR 镜像至国内 CF/R2，保留摘要、部署保护及访问证据。"""
import hashlib
import base64
import re
import random
import ssl
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
PUBLICATION_WAIT_SECONDS = 45 * 60
# 与Worker保持一致：只有JWKS获取瞬态故障兼容旧401重试，不放宽身份或签名拒绝。
TRANSIENT_AUTH_REASONS = frozenset({
    'KEY_FETCH_TIMEOUTERROR', 'KEY_FETCH_ABORTERROR', 'KEY_FETCH_TYPEERROR', 'KEY_FETCH_ERROR',
    'JWKS_HTTP_429', 'JWKS_HTTP_500', 'JWKS_HTTP_502', 'JWKS_HTTP_503', 'JWKS_HTTP_504',
})
SAFE_AUTH_REASONS = TRANSIENT_AUTH_REASONS | frozenset({
    'FORMAT', 'HEADER', 'TIME', 'CLAIMS', 'KEY_FETCH', 'KEY_FETCH_SYNTAXERROR',
    'KEY_ID', 'KEY_IMPORT', 'SIGNATURE', 'CREDENTIALS',
}) | frozenset('JWKS_HTTP_' + str(status) for status in range(100, 600))



class PublicationBusy(ValueError):
    def __init__(self, retry_after=5):
        super().__init__('PUBLICATION_BUSY')
        self.retry_after = retry_after


class TransientHttpError(ValueError):
    def __init__(self, category, status):
        super().__init__(category)
        self.status = status


def certificate_failure(error):
    """证书真实性错误不重试，字符串原因仅在本地判定且不进入日志。"""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(reason, (ssl.SSLCertVerificationError, ssl.CertificateError)) or (isinstance(reason, str) and ('certificate_verify_failed' in reason.lower() or 'certificate verify failed' in reason.lower()))


def cf(path, value, method='POST', tag=None, deadline=None):
    """仅为明确幂等的操作重试短暂网络错误，每次重新签发身份并打开上传文件。"""
    retryable = (method == 'GET' and path == '/status') or (method == 'PUT' and path.startswith('/blob/')) or (method == 'POST' and path in ('/prepare', '/commit', '/verify', '/finalize', '/pins/add'))
    for attempt in range(4):
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError('CF_PUBLICATION_WAIT_TIMEOUT')
        try:
            return cf_once(path, value, method=method, tag=tag)
        except (urllib.error.URLError, TimeoutError, ConnectionError, TransientHttpError) as error:
            if not retryable or certificate_failure(error) or attempt == 3:
                raise
            time.sleep((1, 3, 6)[attempt])


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
        if error.code in (502, 503, 504):
            raise TransientHttpError('OIDC_HTTP_' + str(error.code), error.code) from None
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


def cf_once(path, value, method='POST', tag=None):
    """执行受限管理请求，只记录固定类别、耗时及状态，禁止输出凭据与原始响应。"""
    started = time.monotonic()
    data = None
    status = None
    exception = None
    reason_class = None
    reason_errno = None
    auth_reason = None
    category = 'blob' if path.startswith('/blob/') else 'pins' if path.startswith('/pins/') else path.removeprefix('/') if path in ('/prepare', '/commit', '/verify', '/finalize', '/status', '/abort') else 'other'
    try:
        headers = {'Authorization': 'Bearer ' + publication_token(), 'User-Agent': 'cloudlinkpos-registry/1.0'}
        if isinstance(value, dict):
            data = json.dumps(value).encode()
            headers['Content-Type'] = 'application/json'
        elif value is not None:
            data = value.open('rb')
            headers['Content-Length'] = str(value.stat().st_size)
            headers['X-Registry-Tag'] = tag
            headers['Content-Type'] = 'application/octet-stream'
        request = urllib.request.Request(ORIGIN + '/_registry' + path, data=data, method=method, headers=headers)
        with urllib.request.build_opener(PILOT.NoRedirect()).open(request, timeout=600) as response:
            status = response.status
            return json.loads(response.read(200_000))
    except urllib.error.HTTPError as error:
        status = error.code
        exception = 'HTTPError'
        if path == '/prepare' and error.code == 429 and error.headers.get('x-registry-publication-busy') == '1':
            retry_after = error.headers.get('Retry-After', '5')
            delay = int(retry_after) if re.fullmatch('[0-9]{1,2}', retry_after) else 5
            raise PublicationBusy(max(1, min(delay, 30))) from None
        reason = error.headers.get('x-registry-auth-reason', '')
        auth_reason = reason if reason in SAFE_AUTH_REASONS else None
        suffix = '_' + auth_reason if auth_reason is not None else ''
        if error.code in (502, 503, 504) or (error.code == 401 and auth_reason in TRANSIENT_AUTH_REASONS):
            raise TransientHttpError('CF_HTTP_' + str(error.code) + suffix, error.code) from None
        raise ValueError('CF_HTTP_' + str(error.code) + suffix) from None
    except Exception as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        reason_name = type(reason).__name__
        reason_class = reason_name if reason_name in ('gaierror', 'TimeoutError', 'ConnectionResetError', 'ConnectionRefusedError', 'ConnectionAbortedError', 'SSLCertVerificationError', 'SSLError', 'OSError', 'ConnectionError') else 'StringReason' if isinstance(reason, str) else 'OtherError'
        number = getattr(reason, 'errno', None)
        reason_errno = number if type(number) is int and -65535 <= number <= 65535 else None
        if isinstance(error, TransientHttpError):
            status = error.status
        name = type(error).__name__
        exception = 'HTTPError' if isinstance(error, TransientHttpError) else name if name in ('TimeoutError', 'URLError', 'ConnectionError', 'ConnectionResetError', 'OSError', 'ValueError', 'JSONDecodeError', 'TypeError') else 'OtherError'
        raise
    finally:
        if hasattr(data, 'close'):
            data.close()
        event = {'category': category, 'method': method if method in ('GET', 'POST', 'PUT') else 'OTHER', 'elapsedSeconds': round(max(0, time.monotonic() - started), 3), 'httpStatus': status if type(status) is int and 100 <= status <= 599 else None, 'exception': exception, 'reasonClass': reason_class, 'reasonErrno': reason_errno}
        if auth_reason is not None:
            event['authReason'] = auth_reason
        print('CF 请求状态：' + json.dumps(event, ensure_ascii=False), flush=True)


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


def prepare_publication(service, tag, raw):
    """仅在正式入口明确报告发布槽繁忙时等待，认证、容量和其他失败立即返回。"""
    started = time.monotonic()
    waiting = False
    while True:
        if waiting and time.monotonic() - started >= PUBLICATION_WAIT_SECONDS:
            raise ValueError('CF_PUBLICATION_WAIT_TIMEOUT')
        try:
            return cf('/prepare', {'service': service, 'tag': tag, 'manifest': raw.decode()}, deadline=started + PUBLICATION_WAIT_SECONDS if waiting else None)
        except PublicationBusy as error:
            remaining = PUBLICATION_WAIT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError('CF_PUBLICATION_WAIT_TIMEOUT') from None
            if not waiting:
                print('CF 发布槽已满，等待空闲槽；最多等待 45 分钟。', flush=True)
                waiting = True
            time.sleep(min(max(error.retry_after, random.uniform(5, 15)), remaining))


def mirror(service, tag, digest, raw, refresh_deployment):
    """先保护当前部署，再上传原始 GHCR 内容，所有证据验证通过后才返回收据。"""
    client = PILOT.Registry()
    if client.verify_image(tag, digest) != raw:
        raise ValueError('GHCR_MANIFEST_MISMATCH')
    # 网络读取失败必须终止，不能拿 checkout 时的旧清单替代保护范围。
    deployed = refresh_deployment()
    cf('/pins/add', {'repository': PILOT.REPOSITORY, 'digests': deployed, 'ttlSeconds': 172800, 'operationId': 'ci-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']})
    plan = prepare_publication(service, tag, raw)
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


def finalize(tag, digest):
    """双端发布核验成功后确认版本并安全回收，失败不得生成成功收据。"""
    result = cf('/finalize', {'tag': tag, 'digest': digest})
    if result.get('finalized') is not True or result.get('gcCompleted') is not True or result.get('repository') != PILOT.REPOSITORY or result.get('tag') != tag or result.get('digest') != digest or type(result.get('keepVersions')) is not int or result['keepVersions'] != 2:
        raise ValueError('CF_FINALIZE_VERIFICATION_FAILED')
    used, budget = (result.get(key) for key in ('usedBytes', 'budgetBytes'))
    if type(used) is not int or type(budget) is not int or used < 0 or not 0 < budget <= 9000000000 or used > budget:
        raise ValueError('CF_FINALIZE_CAPACITY_INVALID')


def preflight():
    """编译前只读核验正式分发入口身份和当前容量，不删除历史版本。"""
    result = cf('/status', None, method='GET')
    budget, used, reserved = (result.get(key) for key in ('budgetBytes', 'usedBytes', 'reservedBytes'))
    if not all(type(value) is int for value in (budget, used, reserved)) or not 0 < budget <= 9000000000 or used < 0 or reserved < 0:
        raise ValueError('CF_CAPACITY_PREFLIGHT_FAILED')
    # 构建完成后的 prepare 才清理过期且无引用的额外版本，再判断本次上传能否预留容量。
    print('CF 短期身份及只读容量检查通过；上传时安全清理后判定容量；预算 ' + str(budget) + ' 字节，已用 ' + str(used) + ' 字节，预留 ' + str(reserved) + ' 字节。', flush=True)


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['--preflight']:
            raise ValueError('REGISTRY_ARGUMENTS_INVALID')
        preflight()
    except Exception as error:
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]{3,80}', str(error)) else 'PREFLIGHT_FAILED'
        print('::error::正式分发入口预检失败：' + category, flush=True)
        raise SystemExit(1)
