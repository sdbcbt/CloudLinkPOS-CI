"""覆盖只读代理的摘要、私有性和镜像层验证。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import call, patch

SPEC = importlib.util.spec_from_file_location('delivery', Path(__file__).with_name('ghcr-delivery.py'))
DELIVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DELIVERY)
BLOB = 'sha256:' + 'a' * 64
DESCRIPTOR = {'size': 123, 'digest': BLOB}
MANIFEST = json.dumps({'config': DESCRIPTOR, 'layers': [DESCRIPTOR]}).encode()
DIGEST = 'sha256:' + hashlib.sha256(MANIFEST).hexdigest()
HEADERS = {'Docker-Content-Digest': DIGEST}


class DeliveryVerification(unittest.TestCase):
    @patch.object(DELIVERY.SOURCE, 'pull_token', return_value='oidc')
    @patch.object(DELIVERY, 'request')
    def test_preflight_checks_exact_manifest(self, request, _token):
        request.return_value = (200, HEADERS, MANIFEST + b' ')
        with self.assertRaisesRegex(ValueError, 'OCI_DELIVERY_ORIGIN_UNAVAILABLE'):
            DELIVERY.preflight()
        request.return_value = (200, HEADERS, MANIFEST)
        DELIVERY.preflight()

    @patch.object(DELIVERY.SOURCE, 'pull_token', return_value='oidc')
    @patch.object(DELIVERY, 'request')
    def test_read_only_proof_requires_digest_layers_and_anonymous_denial(self, request, _token):
        replies = [(200, HEADERS, MANIFEST), (200, {'Content-Length': '123'}, b''), (401, {}, b'')]
        request.side_effect = replies
        self.assertTrue(DELIVERY.verify(DIGEST, MANIFEST)['anonymous_pull_denied'])
        self.assertEqual(request.call_args_list, [
            call('/manifests/' + DIGEST, token='oidc'),
            call('/blobs/' + BLOB, method='HEAD', token='oidc'),
            call('/manifests/' + DIGEST),
        ])
        request.side_effect = replies[:2] + [(200, HEADERS, MANIFEST)]
        with self.assertRaisesRegex(ValueError, 'OCI_ANONYMOUS_ACCESS_UNVERIFIED'):
            DELIVERY.verify(DIGEST, MANIFEST)
        request.side_effect = replies[:1] + [(206, {'Content-Length': '1'}, b'')]
        with self.assertRaisesRegex(ValueError, 'OCI_DELIVERY_BLOB_HEAD_FAILED'):
            DELIVERY.verify(DIGEST, MANIFEST)


if __name__ == '__main__':
    unittest.main()
