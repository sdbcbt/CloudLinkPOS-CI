"""核验生产 Cloudflare 拉取代理转发了私有 GHCR 的原始镜像。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

ORIGIN = 'https://registry.cloudlinkpos.com'
REPOSITORY = 'sdbcbt/cloudlinkpos'
BASE = '/v2/' + REPOSITORY
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
spec = importlib.util.spec_from_file_location('registry_source', Path(__file__).with_name('ghcr-delivery-auth.py'))
SOURCE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SOURCE)


def request(path, *, method='GET', token=None):
    headers = {
        'User-Agent': 'cloudlinkpos-registry/1.0',
        'Accept': SOURCE.GHCR.ACCEPT,
    }
    if token:
        headers['Authorization'] = 'Bearer ' + token
    opener = urllib.request.build_opener(SOURCE.GHCR.NoRedirect())
    for attempt in range(3):
        req = urllib.request.Request(ORIGIN + BASE + path, headers=headers, method=method)
        try:
            with opener.open(req, timeout=35) as response:
                raw = response.read(65537) if method == 'GET' else b''
                if len(raw) > 65536:
                    raise ValueError('OCI_DELIVERY_MANIFEST_TOO_LARGE')
                return response.status, response.headers, raw
        except urllib.error.HTTPError as error:
            if error.code not in (429, 502, 503, 504) or attempt == 2:
                return error.code, error.headers, b''
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise ValueError('OCI_DELIVERY_NETWORK_FAILED') from None
        time.sleep((1, 3)[attempt])


def preflight():
    status, reply, raw = request('/manifests/admin-latest', token=SOURCE.pull_token())
    digest = reply.get('Docker-Content-Digest', '')
    if status != 200 or not DIGEST.fullmatch(digest) or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('OCI_DELIVERY_ORIGIN_UNAVAILABLE')
    print('Cloudflare 只读入口已按摘要回源 GHCR', flush=True)


def verify(digest, raw):
    if not DIGEST.fullmatch(digest) or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('OCI_ORIGIN_MANIFEST_DIGEST_MISMATCH')
    token = SOURCE.pull_token()
    path = '/manifests/' + digest
    status, headers, received = request(path, token=token)
    if status != 200 or headers.get('Docker-Content-Digest') != digest or received != raw:
        raise ValueError('OCI_DELIVERY_READBACK_FAILED')
    manifest = json.loads(raw)
    unique = {item['digest']: item for item in [manifest['config'], *manifest['layers']]}
    for layer_digest, item in unique.items():
        if not DIGEST.fullmatch(layer_digest) or type(item['size']) is not int or item['size'] < 0:
            raise ValueError('OCI_BLOB_DESCRIPTOR_INVALID')
        status, headers, _ = request('/blobs/' + layer_digest, method='HEAD', token=token)
        if status != 200 or headers.get('Docker-Content-Length', headers.get('Content-Length')) != str(item['size']):
            raise ValueError('OCI_DELIVERY_BLOB_HEAD_FAILED')
    status, _, _ = request(path)
    if status != 401:
        raise ValueError('OCI_ANONYMOUS_ACCESS_UNVERIFIED')
    return {'authenticated_read': True, 'anonymous_pull_denied': True, 'image_digest': digest}


if __name__ == '__main__':
    try:
        preflight()
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]+', str(error)) else 'OCI_DELIVERY_PREFLIGHT_FAILED'
        print('::error::正式拉取代理预检失败 ' + code)
        raise SystemExit(1)
