"""把本地 docker 镜像以分块、断点续传方式推送到镜像仓库。

为什么不用 docker push：docker push 每个层一次性上传，连接一断就从 0 重传；跨境链路慢时
最大层永远传不完，加多少次重试都没用。这里按 Registry 分块协议上传：
  1. HEAD 查 blob 是否已存在，存在就跳过（基础层、未变化的层只传一次）；
  2. POST 开上传会话，按固定大小 PATCH 分块（Content-Range）；
  3. 断线或超时后 GET 会话状态读取服务端已收到的 Range，从断点续传，进度只增不减；
  4. 全部收齐后 PUT 带 digest 完成，最后 PUT manifest。
“卡住”由套接字超时判定（STALL_SECONDS 内没有任何字节收发），不再用整次推送的总时长。
连续 MAX_STALLS 次失败且没有新进度才放弃；有进度就重新计数。

协议不支持（跨域 Location、非预期 4xx 等）时抛 UploadUnsupported，由调用方退回 docker push。
诊断输出只含阶段、层序号、大小、耗时、错误类别和 HTTP 状态，不输出地址、令牌或响应正文。
"""
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import tarfile
import time
import urllib.parse

CHUNK_BYTES = 4 * 1024 * 1024
STALL_SECONDS = 60  # 单个请求 60 秒内没有任何字节收发即视为卡住
MAX_STALLS = 10  # 连续 10 次失败且无新进度才放弃；有进度就清零
TOTAL_SECONDS = 75 * 60  # 整个镜像上传的硬上限，留出 job 的构建与核验时间
BACKOFF = (5, 10, 20, 30, 60)
GZIP_LEVEL = 6
MANIFEST_TYPE = 'application/vnd.docker.distribution.manifest.v2+json'
CONFIG_TYPE = 'application/vnd.docker.container.image.v1+json'
LAYER_TYPE = 'application/vnd.docker.image.rootfs.diff.tar.gzip'
SLEEP = time.sleep
CLOCK = time.monotonic


class UploadUnsupported(Exception):
    """仓库不接受分块协议，调用方应退回 docker push。"""


class UploadFailed(Exception):
    """网络持续无进展或超出总时限。"""


class Transient(Exception):
    """可续传的网络或服务端临时错误，category 只含固定类别。"""

    def __init__(self, category, status=None):
        super().__init__(category)
        self.category = category
        self.status = status


def log(event, **fields):
    print('镜像分块上传：' + json.dumps({'event': event, **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return 'sha256:' + digest.hexdigest()


def prepare_image(archive, workdir):
    """解析 docker save 的输出，返回 (config 字节, [(gzip 层路径, digest, size)])。

    兼容旧格式（<id>/layer.tar）和新格式（blobs/sha256/<hex>）；层已是 gzip 的直接用，
    未压缩的层以固定参数（mtime=0、无文件名）压缩，同一内容总得到同一 digest，可被后续构建复用。
    每层都核对解压后的 sha256 等于 config 里的 diff_id。
    """
    workdir = Path(workdir)
    extracted = workdir / 'saved'
    extracted.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(extracted, filter='data')
    manifest = json.loads((extracted / 'manifest.json').read_text())
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise UploadUnsupported('镜像归档应只含一个镜像')
    entry = manifest[0]
    config = (extracted / entry['Config']).read_bytes()
    diff_ids = json.loads(config)['rootfs']['diff_ids']
    if len(diff_ids) != len(entry['Layers']):
        raise UploadUnsupported('层数量与 diff_id 不符')
    layers = []
    for index, (name, diff_id) in enumerate(zip(entry['Layers'], diff_ids)):
        source = extracted / name
        with open(source, 'rb') as handle:
            magic = handle.read(4)
        if magic[:2] == b'\x1f\x8b':
            # 移出解包目录，解包目录在函数末尾删除。
            compressed = workdir / ('layer-' + str(index) + '.tar.gz')
            shutil.move(source, compressed)
            source = compressed
            raw = hashlib.sha256()
            with gzip.open(source, 'rb') as handle:
                for block in iter(lambda: handle.read(1 << 20), b''):
                    raw.update(block)
            if 'sha256:' + raw.hexdigest() != diff_id:
                raise UploadUnsupported('压缩层与 diff_id 不符')
        elif magic == b'\x28\xb5\x2f\xfd':
            raise UploadUnsupported('不支持 zstd 层')
        else:
            if sha256_file(source) != diff_id:
                raise UploadUnsupported('层内容与 diff_id 不符')
            compressed = workdir / ('layer-' + str(index) + '.tar.gz')
            with open(source, 'rb') as reader, open(compressed, 'wb') as raw_out:
                with gzip.GzipFile(filename='', mode='wb', compresslevel=GZIP_LEVEL, fileobj=raw_out, mtime=0) as writer:
                    shutil.copyfileobj(reader, writer, 1 << 20)
        layers.append((compressed, sha256_file(compressed), compressed.stat().st_size))
    shutil.rmtree(extracted)
    return config, layers


def build_manifest(config, layers):
    body = {
        'schemaVersion': 2,
        'mediaType': MANIFEST_TYPE,
        'config': {'mediaType': CONFIG_TYPE, 'size': len(config), 'digest': 'sha256:' + hashlib.sha256(config).hexdigest()},
        'layers': [{'mediaType': LAYER_TYPE, 'size': size, 'digest': digest} for _, digest, size in layers],
    }
    return json.dumps(body, separators=(',', ':')).encode()


class Uploader:
    def __init__(self, host, repository, token_for, connect=None):
        """token_for(challenge) 用 401 的 WWW-Authenticate 换取 push 令牌；connect() 返回 HTTPSConnection。"""
        self.host = host
        self.repository = repository
        self.token_for = token_for
        self.token = ''
        self.connect = connect or (lambda: http.client.HTTPSConnection(host, timeout=STALL_SECONDS, context=ssl.create_default_context()))
        self.connection = None
        self.deadline = None

    def path(self, location):
        """只接受同一仓库主机的 https 地址，返回带查询串的路径。"""
        url = urllib.parse.urljoin('https://' + self.host + '/', location)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != self.host or parsed.username or parsed.password or parsed.fragment:
            raise UploadUnsupported('上传地址不在仓库主机')
        return parsed.path + ('?' + parsed.query if parsed.query else '')

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None

    def request(self, method, path, body=None, headers=None):
        """发送一次请求；401 时换令牌重发一次；网络错误和 5xx/429 转为 Transient。"""
        for _ in range(2):
            values = dict(headers or {})
            if self.token:
                values['Authorization'] = 'Bearer ' + self.token
            if body is not None or method in ('PUT', 'PATCH', 'POST'):
                values['Content-Length'] = str(len(body or b''))
            try:
                if self.connection is None:
                    self.connection = self.connect()
                self.connection.request(method, path, body=body, headers=values)
                response = self.connection.getresponse()
                data = response.read(1 << 20)
                returned = {key.lower(): value for key, value in response.getheaders()}
                status = response.status
            except (socket.timeout, TimeoutError):
                self.close()
                raise Transient('stall_timeout') from None
            except (ConnectionError, http.client.HTTPException, ssl.SSLError, OSError) as error:
                self.close()
                raise Transient(type(error).__name__) from None
            if returned.get('connection', '').lower() == 'close':
                self.close()
            if status == 401 and 'www-authenticate' in returned:
                self.token = self.token_for(returned['www-authenticate'])
                continue
            if status == 429 or status >= 500:
                raise Transient('http_status', status)
            return status, returned, data
        raise UploadUnsupported('令牌刷新后仍未授权')

    def guarded(self, action):
        """在无进展失败计数和总时限内重复执行 action(progress) 直到成功。"""
        stalls = 0
        while True:
            if CLOCK() > self.deadline:
                raise UploadFailed('超出上传总时限')
            try:
                return action()
            except Transient as error:
                progressed = getattr(error, 'progressed', False)
                stalls = 0 if progressed else stalls + 1
                log('retry', category=error.category, status=error.status, stalls=stalls)
                if stalls >= MAX_STALLS:
                    raise UploadFailed('连续失败且无进展') from None
                SLEEP(BACKOFF[min(stalls, len(BACKOFF) - 1)])

    def blob_exists(self, digest, size):
        status, headers, _ = self.request('HEAD', '/v2/' + self.repository + '/blobs/' + digest)
        if status in (200, 302, 307):
            if status == 200 and headers.get('content-length') not in (None, str(size)):
                raise UploadUnsupported('已存在层大小不符')
            return True
        if status == 404:
            return False
        raise UploadUnsupported('层存在性查询返回 ' + str(status))

    def start(self):
        status, headers, _ = self.request('POST', '/v2/' + self.repository + '/blobs/uploads/')
        if status != 202 or not headers.get('location'):
            raise UploadUnsupported('开启上传会话返回 ' + str(status))
        return self.path(headers['location'])

    def received(self, location):
        """查询会话已收到的字节数；会话失效返回 None。"""
        status, headers, _ = self.request('GET', location)
        if status == 404:
            return None
        if status not in (202, 204):
            raise UploadUnsupported('上传状态查询返回 ' + str(status))
        value = headers.get('range', '')
        if not value:
            return 0
        end = value.split('=', 1)[-1].split('-', 1)[-1]
        return int(end) + 1

    def upload_blob(self, path, digest, size, label):
        state = {'location': None, 'offset': 0, 'resume': False}
        started = CLOCK()

        def step():
            before = state['offset']
            try:
                if state['location'] is None:
                    state['location'], state['offset'], state['resume'] = self.start(), 0, False
                elif state['resume']:
                    got = self.received(state['location'])
                    if got is None:
                        state['location'], state['offset'] = self.start(), 0
                    else:
                        state['offset'] = got
                    state['resume'] = False
                with open(path, 'rb') as handle:
                    while state['offset'] < size:
                        handle.seek(state['offset'])
                        chunk = handle.read(CHUNK_BYTES)
                        end = state['offset'] + len(chunk) - 1
                        status, headers, _ = self.request('PATCH', state['location'], chunk, {
                            'Content-Type': 'application/octet-stream', 'Content-Range': str(state['offset']) + '-' + str(end)})
                        if status == 416:
                            state['resume'] = True
                            raise Transient('range_mismatch', status)
                        if status != 202 or not headers.get('location'):
                            raise UploadUnsupported('分块上传返回 ' + str(status))
                        state['location'] = self.path(headers['location'])
                        state['offset'] = end + 1
                separator = '&' if '?' in state['location'] else '?'
                status, headers, _ = self.request('PUT', state['location'] + separator + 'digest=' + urllib.parse.quote(digest), b'')
                if status == 416:
                    state['resume'] = True
                    raise Transient('range_mismatch', status)
                if status != 201:
                    raise UploadUnsupported('完成上传返回 ' + str(status))
                if headers.get('docker-content-digest', digest) != digest:
                    raise UploadUnsupported('仓库返回的层摘要不符')
            except Transient as error:
                state['resume'] = state['location'] is not None
                error.progressed = state['offset'] > before
                log('progress', layer=label, sent=state['offset'], size=size)
                raise
        self.guarded(step)
        elapsed = max(CLOCK() - started, 0.001)
        log('blob_done', layer=label, size=size, seconds=round(elapsed, 1), kbps=round(size / 1024 / elapsed, 1))

    def push(self, config, layers, tag):
        """上传全部层与 config，再推送 manifest；返回仓库确认的 manifest digest。"""
        self.deadline = CLOCK() + TOTAL_SECONDS
        config_path = None
        try:
            blobs = list(layers)
            config_digest = 'sha256:' + hashlib.sha256(config).hexdigest()
            for index, (path, digest, size) in enumerate(blobs):
                label = 'layer-' + str(index)
                if self.guarded(lambda: self.blob_exists(digest, size)):
                    log('blob_exists', layer=label, size=size)
                else:
                    self.upload_blob(path, digest, size, label)
            if not self.guarded(lambda: self.blob_exists(config_digest, len(config))):
                config_path = Path(blobs[0][0]).parent / 'config.json' if blobs else Path('config.json')
                config_path.write_bytes(config)
                self.upload_blob(config_path, config_digest, len(config), 'config')
            manifest = build_manifest(config, blobs)
            expected = 'sha256:' + hashlib.sha256(manifest).hexdigest()

            def put_manifest():
                status, headers, _ = self.request('PUT', '/v2/' + self.repository + '/manifests/' + tag, manifest, {'Content-Type': MANIFEST_TYPE})
                if status != 201:
                    raise UploadUnsupported('推送 manifest 返回 ' + str(status))
                if headers.get('docker-content-digest', expected) != expected:
                    # 标签已写入但内容不符，不能再退回 docker push 覆盖同一标签，直接失败。
                    raise UploadFailed('仓库返回的 manifest 摘要不符')
            self.guarded(put_manifest)
            log('manifest_done', layers=len(blobs), bytes=sum(size for _, _, size in blobs))
            return expected
        finally:
            self.close()
            if config_path is not None and config_path.exists():
                os.unlink(config_path)
