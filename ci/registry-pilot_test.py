"""核对试点身份、私有镜像证明和正式发布隔离。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pilot', Path(__file__).with_name('registry-pilot.py'))
PILOT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PILOT)


class Pilot(unittest.TestCase):
    def test_identity_is_immutable_and_attempt_specific(self):
        env = {'SOURCE_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        self.assertEqual(PILOT.identity(env)[1], 'pilot-' + 'a' * 12 + '-123-a2')
        for key in env:
            with self.subTest(key=key), self.assertRaises(ValueError):
                PILOT.identity(dict(env, **{key: 'bad'}))

    def test_private_manifest_must_match_digest(self):
        body = json.dumps({'schemaVersion': 2, 'layers': []}).encode()
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        with patch.object(PILOT, 'token', side_effect=[None, 'private']), patch.object(PILOT, 'request', return_value=(200, {}, body)):
            self.assertEqual(PILOT.verify('tag', digest)['layers'], [])
        with patch.object(PILOT, 'token', side_effect=[None, 'private']), patch.object(PILOT, 'request', return_value=(200, {}, body)), self.assertRaises(ValueError):
            PILOT.verify('tag', 'sha256:' + '0' * 64)

    def test_public_package_is_rejected(self):
        with patch.object(PILOT, 'token', return_value='anonymous'), patch.object(PILOT, 'request', return_value=(200, {}, b'{}')), self.assertRaises(ValueError):
            PILOT.verify('tag', 'sha256:' + '0' * 64)

    def test_existing_public_package_is_rejected_before_push(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-only'}):
            with patch.object(PILOT, 'request', return_value=(200, {}, b'{"visibility":"public"}')), self.assertRaises(ValueError):
                PILOT.check_package_privacy()
            for status, body in ((200, b'{"visibility":"private"}'), (404, b'')):
                with patch.object(PILOT, 'request', return_value=(status, {}, body)):
                    PILOT.check_package_privacy()
            with patch.object(PILOT, 'request', return_value=(403, {}, b'')), self.assertRaises(ValueError):
                PILOT.check_package_privacy()

    def test_workflow_is_fixed_to_github_and_does_not_publish_release_receipts(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/registry-pilot.yml').read_text())
        self.assertEqual(workflow['jobs']['build']['runs-on'], 'ubuntu-24.04')
        self.assertEqual(workflow['jobs']['build']['permissions']['packages'], 'write')
        self.assertNotIn('publish-receipts.py', json.dumps(workflow))
        self.assertNotIn('ACR_PASSWORD', json.dumps(workflow))


if __name__ == '__main__':
    unittest.main()
