"""把已核验的私有 GHCR 试点镜像同步至有容量上限的 CF/R2，不改变镜像摘要。"""
import hashlib
import base64
import re
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

spec = importlib.util.spec_from_file_location('registry_pilot', Path(__file__).with_name('registry-pilot.py'))
PILOT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PILOT)
ORIGIN = 'https://registry-pilot.cloudlinkpos.com'
STAGE = 'RECEIPT'


def publication_token():
    """每次管理请求重新申请短期身份，避免上传期间令牌过期。"""
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.actions.githubusercontent.com') or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('OIDC_ENDPOINT_INVALID')
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if key != 'audience']
    query.append(('audience', 'cloudlinkpos-registry-pilot'))
    url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    headers = {'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'], 'User-Agent': 'cloudlinkpos-registry-pilot/1.0'}
    try:
        with urllib.request.build_opener(PILOT.NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            token = json.loads(response.read(32000))['value']
    except urllib.error.HTTPError as error:
        raise ValueError('OIDC_HTTP_' + str(error.code)) from None
    if not isinstance(token, str) or not 20 < len(token) < 16000:
        raise ValueError('OIDC_TOKEN_INVALID')
    # 仅输出预期公开 CI 声明是否匹配，不输出令牌或原始声明值。
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        expected = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'cloudlinkpos-registry-pilot', 'repository': 'sdbcbt/CloudLinkPOS-CI', 'repository_id': '1446734771', 'repository_owner_id': '286992896', 'ref': 'refs/heads/main', 'event_name': 'workflow_dispatch', 'runner_environment': 'github-hosted', 'actor': 'cloudlinkpos-ci-notifier[bot]', 'workflow_ref': 'sdbcbt/CloudLinkPOS-CI/.github/workflows/registry-pilot.yml@refs/heads/main'}
        matches = {key: claims.get(key) == value for key, value in expected.items()}
        matches['sub'] = claims.get('sub') in ('repo:sdbcbt/CloudLinkPOS-CI:ref:refs/heads/main', 'repo:sdbcbt@286992896/CloudLinkPOS-CI@1446734771:ref:refs/heads/main')
        print('OIDC 声明匹配：' + json.dumps(matches), flush=True)
    except (ValueError, IndexError):
        pass
    return token


def cf(path, value, method='POST', tag=None):
    headers = {'Authorization': 'Bearer ' + publication_token(), 'User-Agent': 'cloudlinkpos-registry-pilot/1.0'}
    if isinstance(value, dict):
        data = json.dumps(value).encode()
        headers['Content-Type'] = 'application/json'
    else:
        data = value.open('rb')
        headers['Content-Length'] = str(value.stat().st_size)
        headers['X-Pilot-Tag'] = tag
        headers['Content-Type'] = 'application/octet-stream'
    try:
        request = urllib.request.Request(ORIGIN + '/_pilot' + path, data=data, method=method, headers=headers)
        with urllib.request.build_opener(PILOT.NoRedirect()).open(request, timeout=120) as response:
            return json.loads(response.read(200_000))
    except urllib.error.HTTPError as error:
        raise ValueError('CF_HTTP_' + str(error.code)) from None
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


def mirror():
    global STAGE
    receipt_path = Path('registry-pilot-receipt.json')
    receipt = json.loads(receipt_path.read_text())
    sha, tag = PILOT.identity(os.environ)
    if receipt['sourceSHA'] != sha or receipt['tag'] != tag or receipt['image'] != PILOT.IMAGE:
        raise ValueError('PILOT_RECEIPT_MISMATCH')
    STAGE = 'GHCR_MANIFEST'
    bearer = PILOT.token()
    status, _, raw = PILOT.request('https://ghcr.io/v2/' + PILOT.REPOSITORY + '/manifests/' + receipt['digest'], {'Authorization': 'Bearer ' + bearer, 'Accept': PILOT.ACCEPT})
    if status != 200 or 'sha256:' + hashlib.sha256(raw).hexdigest() != receipt['digest']:
        raise ValueError('PILOT_MANIFEST_MISMATCH')
    started = time.monotonic()
    planned = False
    try:
        STAGE = 'CF_PREPARE'
        plan = cf('/prepare', {'tag': tag, 'manifest': raw.decode()})
        planned = True
        if plan['digest'] != receipt['digest']:
            raise ValueError('CF_DIGEST_MISMATCH')
        transferred = 0
        with tempfile.TemporaryDirectory() as directory:
            for index, descriptor in enumerate(plan['missing']):
                file = Path(directory) / 'blob'
                STAGE = 'GHCR_BLOB'
                download_blob(descriptor, file, bearer)
                STAGE = 'CF_BLOB'
                cf('/blob/' + descriptor['digest'], file, method='PUT', tag=tag)
                transferred += descriptor['size']
                file.unlink()
                print('CF 已核验镜像层 ' + str(index + 1) + '，累计 ' + str(transferred) + ' 字节', flush=True)
        STAGE = 'CF_COMMIT'
        result = cf('/commit', {'tag': tag, 'digest': receipt['digest']})
        if result['digest'] != receipt['digest']:
            raise ValueError('CF_COMMIT_MISMATCH')
        receipt['cf'] = {'origin': ORIGIN, 'digest': result['digest'], 'stored_bytes': result['usedBytes'], 'transferred_bytes': transferred, 'mirror_seconds': round(time.monotonic() - started, 3)}
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt['cf'], ensure_ascii=False), flush=True)
    except Exception:
        if planned:
            try:
                cf('/abort', {'tag': tag})
            except Exception:
                pass
        raise


if __name__ == '__main__':
    try:
        mirror()
    except Exception as error:
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]{3,80}', str(error)) else 'SYNC_FAILED'
        print('::error::CF 镜像缓存同步失败：' + STAGE + '/' + type(error).__name__ + '/' + category + '；未修改正式发布和部署。', flush=True)
        raise SystemExit(1)
