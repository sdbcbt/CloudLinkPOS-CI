"""核验生产 Cloudflare 拉取代理转发了私有 GHCR 的原始镜像。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
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


def verify_manifest(reference, digest, raw, token):
    path = '/manifests/' + reference
    status, headers, received = request(path, token=token)
    if status != 200 or headers.get('Docker-Content-Digest') != digest or received != raw:
        raise ValueError('OCI_DELIVERY_READBACK_FAILED')


def verify(version, digest, raw):
    if not DIGEST.fullmatch(digest) or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('OCI_ORIGIN_MANIFEST_DIGEST_MISMATCH')
    token = SOURCE.pull_token()
    path = '/manifests/' + digest
    verify_manifest(digest, digest, raw, token)
    verify_manifest(version, digest, raw, token)
    manifest = json.loads(raw)
    unique = {item['digest']: item for item in [manifest['config'], *manifest['layers']]}
    for layer_digest, item in unique.items():
        if not DIGEST.fullmatch(layer_digest) or type(item['size']) is not int or item['size'] < 0:
            raise ValueError('OCI_BLOB_DESCRIPTOR_INVALID')
        status, headers, _ = request('/blobs/' + layer_digest, method='HEAD', token=token)
        if (status != 200 or headers.get('Docker-Content-Digest') != layer_digest
                or headers.get('Docker-Content-Length', headers.get('Content-Length')) != str(item['size'])):
            raise ValueError('OCI_DELIVERY_BLOB_HEAD_FAILED')
    status, _, _ = request(path)
    if status != 401:
        raise ValueError('OCI_ANONYMOUS_ACCESS_UNVERIFIED')
    return {'authenticated_read': True, 'anonymous_pull_denied': True, 'image_digest': digest}


def verify_batch(folder, env):
    names = {item['service'] for item in json.loads(env['EXPECTED_MATRIX'])['include']}
    receipts = {}
    for path in folder.glob('*.json'):
        if path.name == 'source-build-plan.json':
            continue
        item = json.loads(path.read_text())
        name, tag, digest = item['service'], item['tag'], item['digest']
        if (name not in names or name in receipts or item['schema'] != 'cloudlinkpos.public-full-build/v1'
                or item['sourceSHA'] != env['SOURCE_SHA'] or item['workflowSHA'] != env['GITHUB_SHA']
                or item['runID'] != env['GITHUB_RUN_ID'] or item['attempt'] != env['GITHUB_RUN_ATTEMPT']
                or not re.fullmatch(re.escape(name) + r'-v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}', tag)
                or not DIGEST.fullmatch(digest)):
            raise ValueError('OCI_DELIVERY_BATCH_INVALID')
        receipts[name] = (name, tag, digest)
    if not names or names != receipts.keys():
        raise ValueError('OCI_DELIVERY_BATCH_INCOMPLETE')
    return receipts.values()


def promote(entry):
    service, version, digest = entry
    client = SOURCE.GHCR.Registry()
    raw = client.verify_image(version, digest)
    client.update_latest(service, digest)
    verify_manifest(service + '-latest', digest, raw, SOURCE.pull_token())


def finalize():
    receipts = verify_batch(Path('input'), os.environ)
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(promote, receipts))
    print('整批 GHCR latest 已更新并经生产入口核验', flush=True)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--finalize']:
            finalize()
        else:
            preflight()
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]+', str(error)) else 'OCI_DELIVERY_PREFLIGHT_FAILED'
        print('::error::正式镜像分发核验失败 ' + code)
        raise SystemExit(1)
