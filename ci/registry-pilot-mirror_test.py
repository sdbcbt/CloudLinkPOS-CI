"""核对镜像层重定向不泄漏凭据，并验证下载大小和摘要。"""
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('pilot_mirror', Path(__file__).with_name('registry-pilot-mirror.py'))
MIRROR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MIRROR)


class Response(io.BytesIO):
    pass


class Mirror(unittest.TestCase):
    def run_download(self, location, payload=b'layer', size=5, checksum=None):
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(request.full_url, 307, 'redirect', {'Location': location}, None)
                return Response(payload)
        descriptor = {'size': size, 'digest': checksum or 'sha256:' + MIRROR.hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as directory, patch.object(MIRROR.urllib.request, 'build_opener', return_value=Opener()):
            destination = Path(directory) / 'layer'
            MIRROR.download_blob(descriptor, destination, 'fixture-private-token')
            self.assertEqual(destination.read_bytes(), payload)
        return calls

    def test_cdn_redirect_does_not_receive_ghcr_bearer(self):
        calls = self.run_download('https://pkg-containers.githubusercontent.com/blob?fixture=signed')
        self.assertEqual(calls[0].get_header('Authorization'), 'Bearer fixture-private-token')
        self.assertIsNone(calls[1].get_header('Authorization'))

    def test_untrusted_redirect_and_corrupt_blob_are_rejected(self):
        for url in ('https://attacker.test/blob', 'http://ghcr.io/blob', 'https://user:password@ghcr.io/blob'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.run_download(url)
        with self.assertRaises(ValueError):
            self.run_download('https://pkg-containers.githubusercontent.com/blob', size=4)
        with self.assertRaises(ValueError):
            self.run_download('https://pkg-containers.githubusercontent.com/blob', checksum='sha256:' + '0' * 64)


if __name__ == '__main__':
    unittest.main()
