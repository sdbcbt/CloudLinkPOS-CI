"""将 GHCR 原始镜像同步到 Cloudflare 官方 OCI 仓库。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = 'https://registry.cloudlinkpos.com'
PUBLISHER = 'https://cloudlinkpos-oci-publisher.bigrocs1986.workers.dev'
REPOSITORY = 'sdbcbt/cloudlinkpos'
V2 = '/v2/' + REPOSITORY
BUDGET = 9_000_000_000
MAX_BLOB = 99_000_000
TAGS = re.compile(r'([a-z][a-z0-9-]*)-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
SHA = re.compile(r'sha256:[0-9a-f]{64}')
CACHE = None
CACHE_UNTIL = 0
spec = importlib.util.spec_from_file_location('registry_source', Path(__file__).with_name('registry-mirror.py'))
SOURCE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SOURCE)


def oidc():
    """复用现有固定 CI 身份，短暂缓存在当前任务进程内。"""
    global CACHE, CACHE_UNTIL
    if time.monotonic() >= CACHE_UNTIL:
        CACHE = SOURCE.publication_token()
        CACHE_UNTIL = time.monotonic() + 180
    return CACHE


def request(method, path, expected, data=None, headers=None):
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('OCI_REQUEST_PATH_INVALID')
    url = PUBLISHER + path
    for attempt in range(4):
        extra = {'Authorization': 'Bearer ' + oidc(), 'User-Agent': 'cloudlinkpos-registry/1.0', **(headers or {})}
        if isinstance(data, Path):
            with data.open('rb') as stream:
                rq = urllib.request.Request(url, data=stream, method=method, headers={**extra, 'Content-Length': str(data.stat().st_size)})
                status, reply, body = send(rq)
        else:
            status, reply, body = send(urllib.request.Request(url, data=data, method=method, headers=extra))
        if status in expected:
            return reply, body
        if status not in (502, 503, 504) or attempt == 3 or method == 'POST':
            raise ValueError('OCI_HTTP_' + str(status))
        time.sleep((1, 3, 6)[attempt])


def send(rq):
    opener = urllib.request.build_opener(SOURCE.PILOT.NoRedirect())
    try:
        with opener.open(rq, timeout=180) as response:
            return response.status, response.headers, response.read(2_000_000)
    except urllib.error.HTTPError as error:
        return error.code, error.headers, b''
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 503, {}, b''


def storage():
    _, raw = request('GET', '/storage', {200})
    data = json.loads(raw)
    if type(data['usedBytes']) is not int or not 0 <= data['usedBytes'] <= BUDGET or not isinstance(data['tagTimes'], dict):
        raise ValueError('OCI_CAPACITY_UNVERIFIED')
    return data


def preflight():
    request('GET', '/v2/', {200})
    expected = manifest_head('admin-latest')
    headers, _ = request('HEAD', '/delivery/admin-latest', {200})
    if not expected or headers.get('Docker-Content-Digest') != expected:
        raise ValueError('OCI_DELIVERY_ORIGIN_MISMATCH')
    print('官方 OCI 短期身份和 R2 容量检查通过，已用字节 ' + str(storage()['usedBytes']), flush=True)


def manifest_ref(reference):
    return V2 + '/manifests/' + reference


def manifest_head(reference):
    headers, _ = request('HEAD', manifest_ref(reference), {200, 404})
    return headers.get('Docker-Content-Digest')


def descriptor_list(raw):
    manifest = json.loads(raw)
    blobs = {}
    for item in [manifest['config'], *manifest['layers']]:
        digest, size = item['digest'], item['size']
        if not SHA.fullmatch(digest) or type(size) is not int or not 0 <= size <= MAX_BLOB:
            raise ValueError('OCI_BLOB_DESCRIPTOR_INVALID')
        if digest in blobs and blobs[digest]['size'] != size:
            raise ValueError('OCI_BLOB_DESCRIPTOR_CONFLICT')
        blobs[digest] = item
    return manifest['mediaType'], list(blobs.values())


def blob_exists(item):
    headers, _ = request('HEAD', V2 + '/blobs/' + item['digest'], {200, 404})
    digest = headers.get('Docker-Content-Digest')
    if digest is None:
        return False
    if digest != item['digest'] or headers.get('Content-Length') != str(item['size']):
        raise ValueError('OCI_BLOB_IDENTITY_MISMATCH')
    return True


def upload(item, directory):
    target = directory / 'blob'
    SOURCE.download_blob(item, target, SOURCE.PILOT.token())
    headers, _ = request('POST', V2 + '/blobs/uploads/', {202}, data=b'')
    url = urllib.parse.urlsplit(urllib.parse.urljoin(PUBLISHER, headers['Location']))
    if url.scheme != 'https' or url.netloc != urllib.parse.urlsplit(PUBLISHER).netloc or not url.path.startswith(V2 + '/blobs/uploads/'):
        raise ValueError('OCI_UPLOAD_LOCATION_INVALID')
    path = url.path + '?' + url.query
    query = urllib.parse.parse_qsl(url.query)
    query.append(('digest', item['digest']))
    try:
        headers, _ = request('PUT', url.path + '?' + urllib.parse.urlencode(query), {201}, data=target, headers={'Content-Type': 'application/octet-stream'})
        if headers.get('Docker-Content-Digest') != item['digest']:
            raise ValueError('OCI_UPLOAD_DIGEST_MISMATCH')
    except Exception:
        try:
            request('DELETE', path, {204, 404})
        except Exception:
            pass
        raise
    finally:
        target.unlink(missing_ok=True)


def publish(tag, digest, raw):
    tag_parts = TAGS.fullmatch(tag)
    if not tag_parts or not SHA.fullmatch(digest) or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('OCI_PUBLISH_IDENTITY_INVALID')
    svc = tag_parts[1]
    existing = manifest_head(tag)
    if existing and existing != digest:
        raise ValueError('OCI_TAG_IMMUTABLE')
    used = storage()
    versions = [match for name in used['tagTimes'] if (match := TAGS.fullmatch(name)) and match[1] == svc]
    if any(tuple(map(int, match.groups()[1:])) > tuple(map(int, tag_parts.groups()[1:])) for match in versions):
        raise ValueError('OCI_STALE_VERSION')
    media, blobs = descriptor_list(raw)
    missing = [item for item in blobs if not blob_exists(item)]
    # 单路提交，附加层在原生 PUT 直接入桶，预算还包含清单、状态对象和别名替换。
    if used['usedBytes'] + sum(item['size'] for item in missing) + 3 * len(raw) + 65536 > BUDGET:
        raise ValueError('OCI_CAPACITY_EXCEEDED')
    with tempfile.TemporaryDirectory() as directory:
        for item in missing:
            upload(item, Path(directory))
    put_manifest(tag, digest, raw, media)
    for ref in (tag, digest):
        headers, received = request('GET', manifest_ref(ref), {200})
        if headers.get('Docker-Content-Digest') != digest or received != raw:
            raise ValueError('OCI_MANIFEST_READBACK_FAILED')
    if not all(blob_exists(item) for item in blobs):
        raise ValueError('OCI_BLOB_READBACK_FAILED')
    headers, delivered = request('GET', '/delivery/' + digest, {200})
    if headers.get('Docker-Content-Digest') != digest or delivered != raw:
        raise ValueError('OCI_DELIVERY_READBACK_FAILED')
    status, _, _ = send(urllib.request.Request(ORIGIN + manifest_ref(digest), headers={'User-Agent':'cloudlinkpos-registry/1.0'}))
    if status != 401:
        raise ValueError('OCI_ANONYMOUS_ACCESS_UNVERIFIED')
    return {'authenticated_read': True, 'anonymous_pull_denied': True, 'image_digest': digest}


def put_manifest(reference, digest, raw, media):
    headers, _ = request('PUT', manifest_ref(reference), {201}, data=raw, headers={'Content-Type': media})
    if headers.get('Docker-Content-Digest') != digest:
        raise ValueError('OCI_MANIFEST_DIGEST_MISMATCH')


def activate_latest(service, digest, raw, origin):
    reference = service + '-latest'
    previous = manifest_head(reference)
    media = json.loads(raw)['mediaType']
    try:
        put_manifest(reference, digest, raw, media)
        headers, _ = request('HEAD', '/delivery/' + reference, {200})
        if headers.get('Docker-Content-Digest') != digest:
            raise ValueError('OCI_LATEST_READBACK_FAILED')
        origin.update_latest(service, digest)
    except Exception:
        # 请求超时可能发生在写入成功后，以仓库的实际摘要为准。
        status, _, origin_raw = origin.authenticated('/v2/' + REPOSITORY + '/manifests/' + reference)
        if status not in (200, 404):
            raise ValueError('OCI_LATEST_RETRY_REQUIRED') from None
        if status == 200 and 'sha256:' + hashlib.sha256(origin_raw).hexdigest() == digest:
            if manifest_head(reference) == digest:
                return
            raise ValueError('OCI_LATEST_RETRY_REQUIRED') from None
        if manifest_head(reference) == digest and previous != digest:
            if previous:
                _, prior_raw = request('GET', manifest_ref(previous), {200})
                put_manifest(reference, previous, prior_raw, json.loads(prior_raw)['mediaType'])
            else:
                request('DELETE', manifest_ref(reference), {202, 404})
        raise


def collect(protected, force_gc=False):
    usage = storage()
    expiry = int(time.time() * 1000) - 48 * 3600 * 1000
    groups = {}
    available = {}
    for tag, uploaded in usage['tagTimes'].items():
        match = TAGS.fullmatch(tag)
        if not match or type(uploaded) is not int:
            raise ValueError('OCI_RETENTION_CATALOG_INVALID')
        groups.setdefault(match[1], []).append((tuple(map(int, match.groups()[1:])), tag, uploaded))
        available[tag] = manifest_head(tag)
    if any(digest is None for digest in available.values()):
        raise ValueError('OCI_RETENTION_CATALOG_INVALID')
    missing_tags = protected - set(available.values())
    if missing_tags:
        if any(manifest_head(digest) != digest for digest in missing_tags):
            raise ValueError('OCI_RETENTION_PROTECTION_UNVERIFIED')
        print('存在已核验但暂无固定标签的保护摘要，暂缓仓库回收。', flush=True)
        return
    removed = False
    for versions in groups.values():
        for _, tag, uploaded in sorted(versions, reverse=True)[2:]:
            if uploaded <= expiry and available[tag] not in protected:
                request('DELETE', manifest_ref(tag), {202, 404})
                removed = True
    if not removed and not force_gc:
        return
    _, raw = request('POST', V2 + '/gc?mode=untagged', {200}, data=b'')
    if json.loads(raw).get('success') is not True:
        raise ValueError('OCI_RETENTION_GC_FAILED')
    if any(manifest_head(digest) != digest for digest in protected):
        raise ValueError('OCI_RETENTION_REFERENCE_MISSING')
    storage()


if __name__ == '__main__':
    try:
        preflight()
    except Exception as error:
        category = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_0-9]{3,80}', str(error)) else 'OCI_PREFLIGHT_FAILED'
        print('::error::官方仓库预检失败 ' + category)
        raise SystemExit(1)
