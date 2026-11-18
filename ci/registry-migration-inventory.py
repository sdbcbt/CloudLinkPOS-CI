"""只读核算生产镜像迁移所需的最小存储空间。"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import importlib.util
import re
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'sdbcbt/cloudlinkpos'
ORIGIN = 'https://ghcr.io'
V2 = '/v2/' + REPOSITORY
BUDGET = 9_000_000_000
DISTRIBUTION = 'registry.cloudlinkpos.com/' + REPOSITORY
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
ACCEPT = ', '.join((
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

class Registry:
    def __init__(self, username, password):
        self.opener = urllib.request.build_opener(NoRedirect())
        credentials = base64.b64encode(f'{username}:{password}'.encode()).decode()
        query = urllib.parse.urlencode({'scope': f'repository:{REPOSITORY}:pull', 'service': 'ghcr.io'})
        _, body = self.get('/token?' + query, {'Authorization': 'Basic ' + credentials})
        result = json.loads(body)
        self.token = result.get('token') or result['access_token']

    def get(self, path, headers):
        url = urllib.parse.urlsplit(ORIGIN + path)
        if not path.startswith('/') or url.netloc != 'ghcr.io':
            raise ValueError('GHCR 请求地址无效')
        request = urllib.request.Request(url.geturl(), headers=headers)
        with self.opener.open(request, timeout=20) as response:
            return response.headers, response.read(4 * 1024 * 1024)

    def manifest(self, reference):
        return self.get(V2 + '/manifests/' + reference, {'Accept': ACCEPT, 'Authorization': 'Bearer ' + self.token})[1]

    def tags(self):
        path = V2 + '/tags/list?n=100'
        seen = set()
        while path:
            if path in seen:
                raise ValueError('GHCR 标签分页重复')
            seen.add(path)
            headers, body = self.get(path, {'Authorization': 'Bearer ' + self.token})
            yield from json.loads(body).get('tags') or []
            link = headers.get('Link')
            if not link:
                return
            match = re.fullmatch(r'<([^>]+)>;\s*rel="next"', link)
            if not match:
                raise ValueError('GHCR 标签分页无效')
            url = urllib.parse.urlsplit(urllib.parse.urljoin(ORIGIN, match[1]))
            if url.scheme != 'https' or url.netloc != 'ghcr.io' or url.path != V2 + '/tags/list':
                raise ValueError('GHCR 标签分页地址无效')
            path = url.path + '?' + url.query

def select_tags(tags, services):
    """镜像历史按语义版本数值排序，从每个服务选择最新两个版本标签。"""
    selected = {service: [] for service in services}
    for tag in tags:
        match = re.fullmatch(r'([a-z0-9-]+)-v([0-9]+)\.([0-9]+)\.([0-9]+)', tag)
        if match and match[1] in selected:
            selected[match[1]].append((tuple(map(int, match.groups()[1:])), tag))
    if any(not versions for versions in selected.values()):
        raise ValueError('存在没有 GHCR 版本标签的服务')
    return {tag for versions in selected.values() for _, tag in sorted(versions, reverse=True)[:2]}

def inventory(source, services, protected):
    """按唯一摘要累计内容，按 R2 物理对象另计版本标签清单副本。"""
    manifests = {}
    blobs = {}
    references = select_tags(source.tags(), services)
    tag_bytes = 0

    def read_manifest(reference, expected_size=None):
        cached = manifests.get(reference)
        if cached is not None:
            if expected_size is not None and cached != expected_size:
                raise ValueError('GHCR 子清单大小冲突')
            return cached
        raw = source.manifest(reference)
        sha = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if DIGEST.fullmatch(reference) and sha != reference:
            raise ValueError('GHCR 清单摘要不匹配')
        size = len(raw)
        if expected_size is not None and size != expected_size:
            raise ValueError('GHCR 子清单大小不匹配')
        manifest = json.loads(raw)
        if manifest.get('schemaVersion') != 2:
            raise ValueError('GHCR 清单版本无效')
        manifests[sha] = size
        if 'manifests' in manifest:
            for descriptor in manifest['manifests']:
                digest, child_size = descriptor_info(descriptor)
                read_manifest(digest, child_size)
        else:
            for descriptor in [manifest['config'], *manifest['layers']]:
                digest, blob_size = descriptor_info(descriptor)
                if blobs.setdefault(digest, blob_size) != blob_size:
                    raise ValueError('GHCR 层大小冲突')
        return size

    for sha in sorted(protected):
        read_manifest(sha)
    for tag in sorted(references):
        tag_bytes += read_manifest(tag)
    return {
        'protectedDigestCount': len(protected),
        'versionTagCount': len(references),
        'uniqueManifestCount': len(manifests),
        'uniqueBlobCount': len(blobs),
        'blobBytes': sum(blobs.values()),
        'manifestAndVersionTagBytes': sum(manifests.values()) + tag_bytes,
    }

def descriptor_info(descriptor):
    digest, size = descriptor['digest'], descriptor['size']
    if not DIGEST.fullmatch(digest) or type(size) is not int or size < 0:
        raise ValueError('GHCR 内容描述无效')
    return digest, size

def fleet_protection():
    """复用正式发布的只读 Fleet 读取逻辑，仅统计正式分发仓库的引用。"""
    spec = importlib.util.spec_from_file_location('full_build', Path(__file__).with_name('full-build.py'))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    records = [item for item in build.fresh_fleet_records() if item['repository'] == DISTRIBUTION]
    services = set()
    protected = set()
    for item in records:
        tag = re.fullmatch(r'([a-z0-9-]+)-v[0-9]+\.[0-9]+\.[0-9]+', item['tag'])
        if not tag:
            raise ValueError('Fleet 正式镜像标签无效')
        services.add(tag[1])
        protected.add(item['digest'])
    return services, protected

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--migration-input', type=Path)
    source.add_argument('--fleet', action='store_true')
    parser.add_argument('--existing-bytes', required=True, type=int)
    args = parser.parse_args()
    source_user = os.environ.get('CLOUDLINKPOS_GHCR_USERNAME')
    source_token = os.environ.get('CLOUDLINKPOS_GHCR_READ_TOKEN')
    if not source_user or not source_token:
        parser.exit(2, '缺少私有 GHCR 的只读认证。\n')
    if args.existing_bytes < 0:
        parser.error('已有占用必须为非负整数')
    try:
        if args.fleet:
            services, protected = fleet_protection()
        else:
            inputs = json.loads(args.migration_input.read_text())
            protected = set(inputs['protectedDigests'])
            services = set(inputs['services'])
    except (ValueError, KeyError, OSError, RuntimeError):
        parser.exit(1, 'Fleet 镜像保护清单读取失败。\n')
    if not services or not protected or not all(DIGEST.fullmatch(digest) for digest in protected):
        parser.error('镜像保护清单无效')
    try:
        result = inventory(Registry(source_user, source_token), services, protected)
    except urllib.error.HTTPError as error:
        parser.exit(1, f'GHCR 只读核验失败，HTTP {error.code}。\n')
    except (ValueError, KeyError, urllib.error.URLError):
        parser.exit(1, 'GHCR 清单读取或摘要核验失败。\n')
    size = result['blobBytes'] + result['manifestAndVersionTagBytes']
    result.update({
        'minimumNewRegistryBytes': size,
        'minimumCombinedBytes': size + args.existing_bytes,
        'existingBytes': args.existing_bytes,
        'budgetBytes': BUDGET,
        'minimumCombinedFitsBudget': size + args.existing_bytes <= BUDGET,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
