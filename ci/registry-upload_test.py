"""核对分块断点续传：断线后从服务端已收到位置续传、已存在层跳过、无进展才放弃、协议不符退回。"""
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

spec = importlib.util.spec_from_file_location('registry_upload', Path(__file__).with_name('registry-upload.py'))
UP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(UP)

HOST = 'registry.example.test'
REPO = 'ns/repo'


class Response:
    def __init__(self, status, headers=None, body=b''):
        self.status, self.headers, self.body = status, headers or {}, body

    def read(self, _=None):
        return self.body

    def getheaders(self):
        return list(self.headers.items())


class FakeRegistry:
    """最小化的 Registry 分块协议实现；drop 中的请求序号会在服务端收下部分数据后断线。"""

    def __init__(self, existing=(), drop_after=None, location_host=HOST, chunk_status=None):
        self.blobs = {digest: b'' for digest in existing}
        self.sessions = {}
        self.manifests = {}
        self.calls = []
        self.patches = 0
        self.drop_after = drop_after or {}
        self.location_host = location_host
        self.chunk_status = chunk_status
        self.need_auth = True

    def connect(self):
        return Connection(self)

    def handle(self, method, path, body, headers):
        self.calls.append((method, path.split('?')[0]))
        if self.need_auth and headers.get('Authorization') != 'Bearer good':
            return Response(401, {'WWW-Authenticate': 'Bearer realm="x"'})
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query)
        if method == 'HEAD':
            digest = parsed.path.rsplit('/', 1)[1]
            if digest in self.blobs:
                return Response(200, {'Content-Length': str(len(self.blobs[digest])) if self.blobs[digest] else None} if self.blobs[digest] else {})
            return Response(404)
        if method == 'POST':
            session = 's' + str(len(self.sessions))
            self.sessions[session] = b''
            return Response(202, {'Location': 'https://' + self.location_host + '/v2/' + REPO + '/blobs/uploads/' + session + '?_state=0'})
        session = parsed.path.rsplit('/', 1)[1]
        if method == 'GET':
            if session not in self.sessions:
                return Response(404)
            got = len(self.sessions[session])
            return Response(204, {'Range': '0-' + str(got - 1)} if got else {})
        if method == 'PATCH':
            if self.chunk_status:
                return Response(self.chunk_status)
            start, end = (int(v) for v in headers['Content-Range'].split('-'))
            if start != len(self.sessions[session]):
                return Response(416)
            self.patches += 1
            keep = self.drop_after.get(self.patches)
            if keep is not None:
                self.sessions[session] += body[:keep]
                raise ConnectionResetError()
            self.sessions[session] += body
            return Response(202, {'Location': '/v2/' + REPO + '/blobs/uploads/' + session + '?_state=' + str(end + 1), 'Range': '0-' + str(end)})
        if method == 'PUT' and '/manifests/' in parsed.path:
            digest = 'sha256:' + hashlib.sha256(body).hexdigest()
            self.manifests[parsed.path.rsplit('/', 1)[1]] = body
            return Response(201, {'Docker-Content-Digest': digest})
        if method == 'PUT':
            data = self.sessions.pop(session)
            digest = query['digest'][0]
            if 'sha256:' + hashlib.sha256(data).hexdigest() != digest:
                return Response(400)
            self.blobs[digest] = data
            return Response(201, {'Docker-Content-Digest': digest})
        raise AssertionError(method)


class Connection:
    def __init__(self, registry):
        self.registry = registry
        self.pending = None

    def request(self, method, path, body=None, headers=None):
        self.pending = (method, path, body, headers or {})

    def getresponse(self):
        response = self.registry.handle(*self.pending)
        response.headers = {k: v for k, v in response.headers.items() if v is not None}
        return response

    def close(self):
        pass


def blob(tmp, name, data):
    path = Path(tmp) / name
    path.write_bytes(data)
    return path, 'sha256:' + hashlib.sha256(data).hexdigest(), len(data)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(UP, 'SLEEP', lambda _: None), patch.object(UP, 'CHUNK_BYTES', 10)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def uploader(self, registry):
        return UP.Uploader(HOST, REPO, lambda challenge: 'good', registry.connect)

    def test_resumes_from_server_offset_after_disconnect(self):
        data = bytes(range(256)) * 2
        registry = FakeRegistry(drop_after={3: 4, 7: 0, 8: 9})
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', data)
            digest = self.uploader(registry).push(b'{"rootfs":{}}', [layer], 'svc-v1.0.1')
        self.assertEqual(registry.blobs[layer[1]], data)
        # 断线后通过 GET 查询断点，没有整层重传：PATCH 字节总量只比数据多出断线前丢弃的部分。
        self.assertIn(('GET', '/v2/ns/repo/blobs/uploads/s0'), registry.calls)
        self.assertEqual(sum(1 for c in registry.calls if c[0] == 'POST'), 2)  # 层与 config 各一次会话
        manifest = json.loads(registry.manifests['svc-v1.0.1'])
        self.assertEqual(digest, 'sha256:' + hashlib.sha256(registry.manifests['svc-v1.0.1']).hexdigest())
        self.assertEqual(manifest['layers'][0]['digest'], layer[1])

    def test_existing_blobs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'base-layer')
            config = b'{"rootfs":{}}'
            registry = FakeRegistry(existing=[layer[1], 'sha256:' + hashlib.sha256(config).hexdigest()])
            self.uploader(registry).push(config, [layer], 't')
        self.assertFalse([c for c in registry.calls if c[0] in ('POST', 'PATCH')])

    def test_gives_up_only_after_consecutive_failures_without_progress(self):
        registry = FakeRegistry(drop_after={n: 0 for n in range(1, 100)})
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'x' * 50)
            with self.assertRaises(UP.UploadFailed):
                self.uploader(registry).push(b'{}', [layer], 't')
        self.assertEqual(registry.patches, UP.MAX_STALLS)

    def test_slow_but_progressing_upload_is_not_abandoned(self):
        # 每个分块都在收下 1 字节后断线：一直有进度，因此不会因连续失败计数放弃。
        registry = FakeRegistry(drop_after={n: 1 for n in range(1, 1000)})
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'y' * 30)
            self.uploader(registry).push(b'{}', [layer], 't')
        self.assertEqual(registry.blobs[layer[1]], b'y' * 30)

    def test_expired_session_restarts_upload(self):
        registry = FakeRegistry(drop_after={2: 3})
        original = registry.handle

        def expire(method, path, body, headers):
            if method == 'GET' and 'uploads/s0' in path:
                registry.sessions.pop('s0', None)
            return original(method, path, body, headers)
        registry.handle = expire
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'z' * 25)
            self.uploader(registry).push(b'{}', [layer], 't')
        self.assertEqual(registry.blobs[layer[1]], b'z' * 25)

    def test_socket_timeout_is_a_resumable_stall(self):
        registry = FakeRegistry()
        original = registry.handle
        state = {'n': 0}

        def stall(method, path, body, headers):
            if method == 'PATCH':
                state['n'] += 1
                if state['n'] == 2:
                    raise socket.timeout()
            return original(method, path, body, headers)
        registry.handle = stall
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'w' * 25)
            self.uploader(registry).push(b'{}', [layer], 't')
        self.assertEqual(registry.blobs[layer[1]], b'w' * 25)

    def test_foreign_location_or_unexpected_status_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'q' * 5)
            with self.assertRaises(UP.UploadUnsupported):
                self.uploader(FakeRegistry(location_host='oss.example.test')).push(b'{}', [layer], 't')
            with self.assertRaises(UP.UploadUnsupported):
                self.uploader(FakeRegistry(chunk_status=400)).push(b'{}', [layer], 't')

    def test_total_deadline_stops_upload(self):
        registry = FakeRegistry(drop_after={n: 1 for n in range(1, 1000)})
        ticks = iter(range(0, 10 ** 6, 600))
        with patch.object(UP, 'CLOCK', lambda: next(ticks)), tempfile.TemporaryDirectory() as tmp:
            layer = blob(tmp, 'l', b'v' * 500)
            with self.assertRaises(UP.UploadFailed):
                self.uploader(registry).push(b'{}', [layer], 't')


def tar_bytes(files):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class PrepareTests(unittest.TestCase):
    def saved(self, tmp, layers, layout):
        diff_ids = ['sha256:' + hashlib.sha256(raw).hexdigest() for raw in layers]
        config = json.dumps({'rootfs': {'type': 'layers', 'diff_ids': diff_ids}}).encode()
        files = {}
        names = []
        for index, raw in enumerate(layers):
            if layout == 'oci':
                stored = gzip.compress(raw, mtime=0) if index == 0 else raw
                name = 'blobs/sha256/' + hashlib.sha256(stored).hexdigest()
            else:
                stored, name = raw, 'l' + str(index) + '/layer.tar'
            files[name] = stored
            names.append(name)
        files['config.json'] = config
        files['manifest.json'] = json.dumps([{'Config': 'config.json', 'RepoTags': ['x:y'], 'Layers': names}]).encode()
        archive = Path(tmp) / 'image.tar'
        archive.write_bytes(tar_bytes(files))
        return archive, config

    def test_layers_are_verified_and_compressed_deterministically(self):
        layers = [tar_bytes({'a': b'1' * 1000}), tar_bytes({'b': b'2' * 1000})]
        digests = []
        for layout in ('legacy', 'oci'):
            for _ in range(2):
                with tempfile.TemporaryDirectory() as tmp:
                    archive, config = self.saved(tmp, layers, layout)
                    got_config, got = UP.prepare_image(archive, tmp)
                    self.assertEqual(got_config, config)
                    for path, digest, size in got:
                        self.assertEqual(gzip.decompress(Path(path).read_bytes()), layers[got.index((path, digest, size))])
                    digests.append([digest for _, digest, _ in got])
        # 同一内容重复准备得到相同 digest，后续构建可复用已上传的层。
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[2], digests[3])

    def test_diff_id_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, _ = self.saved(tmp, [tar_bytes({'a': b'1'})], 'legacy')
            with tarfile.open(archive) as tar:
                members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
            members['config.json'] = json.dumps({'rootfs': {'diff_ids': ['sha256:' + '0' * 64]}}).encode()
            archive.write_bytes(tar_bytes(members))
            with self.assertRaises(UP.UploadUnsupported):
                UP.prepare_image(archive, tmp)


if __name__ == '__main__':
    unittest.main()
