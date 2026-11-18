"""覆盖只读代理的摘要、私有性和镜像层验证。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
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
        version = 'admin-v0.3.128'
        manifest_reply = (200, HEADERS, MANIFEST)
        head = {'Docker-Content-Length': '123', 'Docker-Content-Digest': BLOB}
        replies = [manifest_reply, manifest_reply, (200, head, b''), (401, {}, b'')]
        request.side_effect = replies
        self.assertTrue(DELIVERY.verify(version, DIGEST, MANIFEST)['anonymous_pull_denied'])
        self.assertEqual(request.call_args_list, [
            call('/manifests/' + DIGEST, token='oidc'),
            call('/manifests/' + version, token='oidc'),
            call('/blobs/' + BLOB, method='HEAD', token='oidc'),
            call('/manifests/' + DIGEST),
        ])
        request.side_effect = replies[:3] + [(200, HEADERS, MANIFEST)]
        with self.assertRaisesRegex(ValueError, 'OCI_ANONYMOUS_ACCESS_UNVERIFIED'):
            DELIVERY.verify(version, DIGEST, MANIFEST)
        request.side_effect = replies[:2] + [(200, dict(head, **{'Docker-Content-Digest': DIGEST}), b'')]
        with self.assertRaisesRegex(ValueError, 'OCI_DELIVERY_BLOB_HEAD_FAILED'):
            DELIVERY.verify(version, DIGEST, MANIFEST)


    @patch.object(DELIVERY.SOURCE.GHCR, 'Registry')
    @patch.object(DELIVERY, 'verify_manifest')
    @patch.object(DELIVERY, 'latest_manifest')
    def test_ambiguous_alias_write_is_reconciled_or_restored(self, latest, verify, registry):
        prior = 'sha256:' + '9' * 64
        entry = ('admin', DIGEST, MANIFEST, prior)
        latest.return_value = (DIGEST, MANIFEST)
        registry.return_value.update_latest.side_effect = TimeoutError
        self.assertEqual(DELIVERY.promote(entry, 'oidc'), 'admin')
        registry.return_value.update_latest.reset_mock(side_effect=True)
        verify.side_effect = ValueError('OCI_DELIVERY_READBACK_FAILED')
        with self.assertRaisesRegex(ValueError, 'OCI_DELIVERY_READBACK_FAILED'):
            DELIVERY.promote(entry, 'oidc')
        self.assertEqual(registry.return_value.update_latest.call_args_list, [
            call('admin', DIGEST), call('admin', prior),
        ])

    def test_batch_requires_all_current_attempt_receipts_before_promotion(self):
        env = {'SOURCE_SHA': 's', 'GITHUB_SHA': 'w', 'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '2',
               'EXPECTED_MATRIX': json.dumps({'include': [{'service': 'admin'}, {'service': 'file-api'}]})}
        item = {'service': 'admin', 'tag': 'admin-v0.3.128', 'digest': DIGEST, 'sourceSHA': 's',
                'schema': 'cloudlinkpos.public-full-build/v1', 'runID': '1', 'attempt': '2', 'workflowSHA': 'w'}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'admin.json').write_text(json.dumps(item))
            with self.assertRaisesRegex(ValueError, 'OCI_DELIVERY_BATCH_INCOMPLETE'):
                DELIVERY.verify_batch(folder, env)
            file = dict(item, service='file-api', tag='file-api-v0.3.48')
            (folder / 'file-api.json').write_text(json.dumps(dict(file, attempt='1')))
            with self.assertRaisesRegex(ValueError, 'OCI_DELIVERY_BATCH_INVALID'):
                DELIVERY.verify_batch(folder, env)
            (folder / 'file-api.json').write_text(json.dumps(file))
            self.assertEqual(set(DELIVERY.verify_batch(folder, env)), {
                ('admin', 'admin-v0.3.128', DIGEST), ('file-api', 'file-api-v0.3.48', DIGEST),
            })


if __name__ == '__main__':
    unittest.main()
