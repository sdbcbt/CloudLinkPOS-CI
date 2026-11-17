"""核对正式同步的保护顺序、访问证据和不可信重定向边界。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('registry_mirror', Path(__file__).with_name('registry-mirror.py'))
MIRROR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MIRROR)


class Mirror(unittest.TestCase):
    def execute(self, anonymous=401, verified=None):
        digest = 'sha256:' + 'd' * 64
        tag = 'employee-api-v0.3.100'
        raw = b'{"schemaVersion":2,"layers":[]}'
        calls = []

        def cf(path, value, **kwargs):
            calls.append((path, value))
            if path == '/prepare':
                return {'digest': digest, 'missing': []}
            if path == '/commit':
                return {'digest': digest}
            if path == '/verify':
                return verified if verified is not None else {'verified': True, 'blobDigestsVerified': True, 'repository': MIRROR.PILOT.REPOSITORY, 'tag': tag, 'digest': digest, 'latest': 'employee-api-latest'}
            return {}
        with patch.dict(MIRROR.os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), patch.object(MIRROR.PILOT.Registry, 'verify_image', return_value=raw), patch.object(MIRROR, 'cf', side_effect=cf), patch.object(MIRROR.PILOT, 'request', return_value=(anonymous, {}, b'')):
            result = MIRROR.mirror('employee-api', tag, digest, raw, lambda: ['sha256:' + 'a' * 64])
        return calls, result

    def test_fresh_deployments_and_new_version_pinned_before_commit_and_receipt(self):
        calls, evidence = self.execute()
        self.assertEqual([call[0] for call in calls], ['/pins/add', '/prepare', '/pins/add', '/commit', '/verify'])
        self.assertEqual(calls[0][1]['ttlSeconds'], 172800)
        self.assertEqual(calls[0][1]['operationId'], 'ci-123-1')
        self.assertEqual(calls[0][1]['operationId'], calls[2][1]['operationId'])
        self.assertTrue(evidence['anonymous_pull_denied'])

    def test_wrong_cf_digest_or_anonymous_success_blocks_receipt(self):
        for status in (200, 403):
            with self.assertRaises(ValueError):
                self.execute(anonymous=status)
        with self.assertRaises(ValueError):
            self.execute(verified={'digest': 'sha256:' + 'e' * 64, 'latest': 'employee-api-latest'})

    def test_abort_is_best_effort_and_preserves_original_failure(self):
        digest = 'sha256:' + 'd' * 64
        raw = b'{}'
        calls = []
        original = ValueError('ORIGINAL_FAILURE')
        def cf(path, value, **kwargs):
            calls.append(path)
            if path == '/prepare':
                return {'digest': digest, 'missing': []}
            if path == '/pins/add' and calls.count(path) > 1:
                raise original
            if path == '/abort':
                raise RuntimeError('cleanup failed')
            return {}
        with patch.dict(MIRROR.os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), patch.object(MIRROR.PILOT.Registry, 'verify_image', return_value=raw), patch.object(MIRROR, 'cf', side_effect=cf), self.assertRaises(ValueError) as caught:
            MIRROR.mirror('employee-api', 'employee-api-v0.3.100', digest, raw, lambda: [])
        self.assertIs(caught.exception, original)
        self.assertEqual(calls[-1], '/abort')

    def test_plan_read_only_preflight_precedes_build_and_source_checkout(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/full-version-build.yml').read_text())
        plan = workflow['jobs']['plan']
        self.assertEqual(plan['permissions'], {'contents': 'read', 'id-token': 'write'})
        runs = [step.get('run', '') for step in plan['steps']]
        preflight = runs.index('python3 automation/ci/registry-mirror.py --preflight')
        self.assertLess(preflight, runs.index('python3 automation/ci/full-build.py checkout'))
        self.assertEqual(workflow['jobs']['build']['needs'], 'plan')
        with patch.object(MIRROR, 'cf', return_value={'budgetBytes': 9000000000, 'usedBytes': 10, 'reservedBytes': 20}) as cf:
            MIRROR.preflight()
        cf.assert_called_once_with('/status', None, method='GET')
        invalid = [
            {'budgetBytes': 9000000001},
            {'usedBytes': 9000000000},
            {'usedBytes': 8999999990, 'reservedBytes': 11},
        ]
        for changes in invalid:
            status = {'budgetBytes': 9000000000, 'usedBytes': 10, 'reservedBytes': 20, **changes}
            with self.subTest(changes=changes), patch.object(MIRROR, 'cf', return_value=status), self.assertRaises(ValueError):
                MIRROR.preflight()
        for reason in ('CF_HTTP_507', 'CF_HTTP_503', 'CF_HTTP_401'):
            original = ValueError(reason)
            with self.subTest(reason=reason), patch.object(MIRROR, 'cf', side_effect=original), self.assertRaises(ValueError) as caught:
                MIRROR.preflight()
            self.assertIs(caught.exception, original)

    def test_finalize_requires_matching_version_gc_and_retention_confirmation(self):
        tag, digest = 'employee-api-v0.3.100', 'sha256:' + 'd' * 64
        result = {'finalized': True, 'gcCompleted': True, 'repository': 'sdbcbt/cloudlinkpos', 'tag': tag, 'digest': digest, 'keepVersions': 2, 'usedBytes': 100, 'budgetBytes': 9000000000}
        with patch.object(MIRROR, 'cf', return_value=result) as cf:
            MIRROR.finalize(tag, digest)
        cf.assert_called_once_with('/finalize', {'tag': tag, 'digest': digest})
        for changed in ({'finalized': False}, {'gcCompleted': False}, {'digest': 'sha256:' + 'e' * 64}, {'keepVersions': 3}, {'usedBytes': 9000000001}):
            with self.subTest(changed=changed), patch.object(MIRROR, 'cf', return_value={**result, **changed}), self.assertRaises(ValueError):
                MIRROR.finalize(tag, digest)
        with patch.object(MIRROR, 'cf', side_effect=ValueError('CF_HTTP_503')), self.assertRaisesRegex(ValueError, 'CF_HTTP_503'):
            MIRROR.finalize(tag, digest)

    def test_finalize_is_after_both_registry_checks_and_before_receipt(self):
        source = Path(__file__).with_name('full-build.py').read_text()
        mirror = source.index('evidence = mirror.mirror(')
        latest = source.index('client.update_latest(service, digest)', mirror)
        finalize = source.index('mirror.finalize(version, digest)', latest)
        receipt = source.index("receipt = {'schema'", finalize)
        self.assertLess(mirror, latest)
        self.assertLess(latest, finalize)
        self.assertLess(finalize, receipt)
        calls, _ = self.execute()
        self.assertNotIn('/finalize', [call[0] for call in calls])

    def test_credentials_are_never_forwarded_to_untrusted_download_redirect(self):
        error = urllib.error.HTTPError('https://ghcr.io', 307, '', {'Location': 'https://evil.example/blob'}, None)
        with tempfile.TemporaryDirectory() as directory, patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=error), self.assertRaisesRegex(ValueError, 'UNTRUSTED_BLOB_REDIRECT'):
            MIRROR.download_blob({'digest': 'sha256:' + 'd' * 64, 'size': 10}, Path(directory) / 'blob', 'private-token')


if __name__ == '__main__':
    unittest.main()
