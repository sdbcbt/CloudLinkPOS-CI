"""核对标准 OCI 推送边界、预检顺序和容量约束。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('official_registry', Path(__file__).with_name('official-registry.py'))
OCI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OCI)


class OfficialRegistry(unittest.TestCase):
    def test_production_preflight_reads_a_fixed_digest(self):
        digest = 'sha256:' + 'd' * 64
        with patch.object(OCI, 'manifest_head', return_value=digest), patch.object(OCI, 'request', return_value=({'Docker-Content-Digest': digest}, b'')) as request, patch.object(OCI, 'storage', return_value={'usedBytes': 1}):
            OCI.preflight()
        self.assertEqual(request.call_args_list[0].args, ('GET', '/v2/', {200}))
        self.assertEqual(request.call_args_list[1].args, ('HEAD', '/delivery/' + digest, {200}))

    def test_plan_read_only_preflight_precedes_build_and_source_checkout(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/full-version-build.yml').read_text())
        plan = workflow['jobs']['plan']
        self.assertEqual(plan['permissions'], {'contents': 'read', 'id-token': 'write'})
        runs = [step.get('run', '') for step in plan['steps']]
        self.assertLess(runs.index('python3 automation/ci/official-registry.py'), runs.index('python3 automation/ci/full-build.py checkout'))
        self.assertEqual(workflow['jobs']['build']['needs'], 'plan')
        self.assertEqual(workflow['jobs']['distribute']['concurrency']['group'], 'official-oci-publication')

    def test_official_publish_and_gc_precede_receipt(self):
        source = Path(__file__).with_name('publish-official-images.py').read_text()
        origin = source.index('raw = origin.verify_image(')
        publish = source.index("item['privateEvidence'] = registry.publish(")
        latest = source.index('registry.activate_latest(')
        collect = source.rindex('registry.collect(protected, fleet_digests)')
        receipt = source.index("work = Path('receipts')")
        stages = [origin, publish, collect, latest, receipt]
        self.assertEqual(sorted(stages), stages)

    def test_credentials_are_never_forwarded_to_untrusted_download_redirect(self):
        error = urllib.error.HTTPError('https://ghcr.io', 307, '', {'Location': 'https://evil.example/blob'}, None)
        with tempfile.TemporaryDirectory() as directory, patch.object(OCI.SOURCE.urllib.request.OpenerDirector, 'open', side_effect=error), self.assertRaisesRegex(ValueError, 'UNTRUSTED_BLOB_REDIRECT'):
            OCI.SOURCE.download_blob({'digest': 'sha256:' + 'd' * 64, 'size': 10}, Path(directory) / 'blob', 'private-token')

    def test_capacity_rejects_new_layers_before_writing(self):
        config = {'size': 100000, 'digest': 'sha256:' + 'd' * 64}
        manifest = {'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'schemaVersion': 2, 'config': config, 'layers': []}
        raw = json.dumps(manifest).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        with patch.object(OCI, 'manifest_head', return_value=None), patch.object(OCI, 'storage', return_value={'usedBytes': OCI.BUDGET - 10000, 'tagTimes': {}}), patch.object(OCI, 'blob_exists', return_value=False), patch.object(OCI, 'upload') as upload, self.assertRaisesRegex(ValueError, 'OCI_CAPACITY_EXCEEDED'):
            OCI.publish('admin-v1.2.3', digest, raw)
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
