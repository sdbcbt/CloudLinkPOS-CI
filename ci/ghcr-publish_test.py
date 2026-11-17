"""核对原始仓库私有性及不可变版本失败闭锁。"""
import importlib.util
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ghcr_publish', Path(__file__).with_name('ghcr-publish.py'))
GHCR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GHCR)


class Publication(unittest.TestCase):
    def test_public_package_and_unknown_privacy_fail_closed(self):
        for status, raw in ((200, b'{"visibility":"public"}'), (403, b'')):
            with patch.dict(GHCR.os.environ, {'GITHUB_TOKEN': 'test'}), patch.object(GHCR, 'request', return_value=(status, {}, raw)), self.assertRaises(ValueError):
                GHCR.Registry().check_private_existing()

    def test_existing_or_unverified_tag_is_never_overwritten(self):
        for status in (200, 401, 500):
            with patch.object(GHCR.Registry, 'authenticated', return_value=(status, {}, b'')), self.assertRaises(ValueError):
                GHCR.Registry().reject_existing_tags(['svc-v1.0.0'])

    def test_anonymous_proof_targets_this_digest_even_when_token_denied(self):
        digest = 'sha256:' + 'a' * 64
        with patch.object(GHCR.Registry, 'verify_image'), patch.object(GHCR, 'token', return_value=None), patch.object(GHCR, 'request', return_value=(401, {}, b'')) as request:
            self.assertEqual(GHCR.Registry().prove_private(digest)['image_digest'], digest)
            self.assertTrue(request.call_args.args[0].endswith('/manifests/' + digest))
        with patch.object(GHCR.Registry, 'verify_image'), patch.object(GHCR, 'token', return_value='test'), patch.object(GHCR, 'request', return_value=(200, {}, b'')), self.assertRaises(ValueError):
            GHCR.Registry().prove_private(digest)

    def test_wrong_manifest_digest_is_rejected(self):
        raw = json.dumps({'schemaVersion': 2, 'layers': []}).encode()
        with patch.object(GHCR.Registry, 'authenticated', return_value=(200, {}, raw)), self.assertRaises(ValueError):
            GHCR.Registry().verify_image('tag', 'sha256:' + 'a' * 64)


if __name__ == '__main__':
    unittest.main()
