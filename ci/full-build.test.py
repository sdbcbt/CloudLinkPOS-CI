"""核对版本递增及仓库分页失败保护。"""
import importlib.util
import json
import hashlib
import tempfile
from unittest.mock import patch
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('full_build', Path(__file__).with_name('full-build.py'))
BUILD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUILD)


def manifest(generation, gray, comment=True):
    body = json.dumps({'releasePolicy': {'grayRequired': gray, 'generation': generation}, 'flagger': {}})
    return ('# 受管清单\n' if comment else '') + body + '\n'


class Policy(unittest.TestCase):
    def test_deploy_branch_policy_wins_only_with_higher_generation(self):
        # 迁移前部署分支滞后同步：主线代次更高时用主线。
        self.assertEqual(BUILD.release_policy(manifest(5, True), manifest(4, False)), {'grayRequired': True, 'generation': 5})
        # 迁移后主线清单冻结：部署分支代次更高时用部署分支。
        self.assertEqual(BUILD.release_policy(manifest(4, False), manifest(6, True)), {'grayRequired': True, 'generation': 6})
        self.assertEqual(BUILD.release_policy(manifest(4, False), manifest(4, True)), {'grayRequired': False, 'generation': 4})

    def test_missing_deploy_branch_falls_back_to_main(self):
        self.assertEqual(BUILD.release_policy(manifest(3, True, False), None)['generation'], 3)

    def test_invalid_policy_is_rejected_on_either_branch(self):
        broken = json.dumps({'releasePolicy': {'grayRequired': 'yes', 'generation': 1}})
        with self.assertRaises(ValueError):
            BUILD.release_policy(broken, None)
        with self.assertRaises(ValueError):
            BUILD.release_policy(manifest(3, True), broken)


class DeployFetch(unittest.TestCase):
    def run_fetch(self, outputs):
        calls = []

        def fake(args, cwd=None, env=None, data=None):
            calls.append(args)
            value = outputs.get(args[1])
            if isinstance(value, Exception):
                raise value
            return value or b''
        with tempfile.TemporaryDirectory() as directory, patch.object(BUILD, 'run', fake), patch.object(BUILD, 'DEPLOY_MANIFEST', Path(directory) / 'm.yaml') as target:
            BUILD.fetch_deploy_manifest({})
            return calls, (target.read_text() if target.exists() else None)

    def test_absent_branch_falls_back_without_fetch(self):
        calls, saved = self.run_fetch({'ls-remote': b''})
        self.assertIsNone(saved)
        self.assertEqual([c[1] for c in calls], ['ls-remote'])

    def test_read_failure_stops_instead_of_falling_back(self):
        with self.assertRaises(RuntimeError):
            self.run_fetch({'ls-remote': RuntimeError('阶段失败')})
        with self.assertRaises(RuntimeError):
            self.run_fetch({'ls-remote': ('a' * 40 + '\trefs/heads/x\n').encode(), 'fetch': RuntimeError('阶段失败')})

    def test_branch_is_fetched_in_separate_repository(self):
        calls, saved = self.run_fetch({'ls-remote': ('a' * 40 + '\trefs/heads/x\n').encode(), 'show': manifest(7, True).encode()})
        self.assertEqual(BUILD.release_policy(manifest(4, False), saved)['generation'], 7)
        fetch = next(c for c in calls if c[1] == 'fetch')
        self.assertIn('a' * 40, fetch)
        self.assertNotIn(str(BUILD.ROOT), fetch)


class Versions(unittest.TestCase):
    def test_next_version_uses_highest_service_version_and_excludes_other_aliases(self):
        tags = ['user-api-v0.1.99', 'user-api-v1.2.3', 'user-api-v1.2.4-rc1', 'user-api-v01.2.9', 'other-v9.0.0', 'user-api-sha-' + 'a' * 64, 'user-api-latest']
        self.assertEqual(BUILD.next_version('user-api', tags), 'user-api-v1.2.4')
        self.assertEqual(BUILD.next_version('new-rpc', tags), 'new-rpc-v0.1.1')

    def test_registry_pagination_cannot_silently_loop(self):
        class Client:
            repository = 'sdbcbt/cloudlinkpos'
            def authenticated(self, path):
                return 200, {}, json.dumps({'name': self.repository, 'tags': ['same'] * 1000}).encode()
        with self.assertRaisesRegex(ValueError, '分页停滞'):
            BUILD.all_tags(Client())

    def test_short_pages_are_read_until_empty_instead_of_truncating_versions(self):
        class Client:
            repository = 'sdbcbt/cloudlinkpos'
            pages = [['service-v0.1.1'], ['service-v1.0.2'], []]
            def authenticated(self, path):
                return 200, {}, json.dumps({'name': self.repository, 'tags': self.pages.pop(0)}).encode()
        self.assertEqual(BUILD.next_version('service', BUILD.all_tags(Client())), 'service-v1.0.3')

    def test_registry_error_and_wrong_repository_are_rejected(self):
        class Client:
            repository = 'sdbcbt/cloudlinkpos'
            def authenticated(self, path):
                return 403, {}, b'{"name":"other","tags":[]}'
        with self.assertRaisesRegex(ValueError, '未核验'):
            BUILD.all_tags(Client())


class AdminContext(unittest.TestCase):
    def test_registry_version_preserves_manual_source_and_refreshes_archive_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'context/artifacts').mkdir(parents=True)
            (root / 'context/Dockerfile').write_text('FROM fixture\nLABEL io.cloudlinkpos.release-tag="admin-v0.1.12"\n')
            (root / 'release.json').write_text(json.dumps({'release_tag': 'admin-v0.1.12', 'release_source': 'manual', 'git_tag': '', 'context_sha256': 'old'}))
            with patch.object(BUILD, 'run', return_value=b'new-normalized-archive'):
                BUILD.stamp_admin_version(root, 'admin-v0.3.5')
            metadata = json.loads((root / 'release.json').read_text())
            self.assertEqual(metadata['git_tag'], '')
            self.assertEqual(metadata['release_source'], 'manual')
            self.assertEqual(metadata['release_tag'], 'admin-v0.3.5')
            self.assertEqual(metadata['context_sha256'], hashlib.sha256((root / 'context.tar').read_bytes()).hexdigest())
            self.assertEqual(metadata['aliases'], [BUILD.IMAGE + ':admin-v0.3.5'])
            self.assertIn('release-tag="admin-v0.3.5"', (root / 'context/Dockerfile').read_text())


class Retry(unittest.TestCase):
    def test_retries_once_then_succeeds_and_waits(self):
        waits, calls = [], []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('网络')
            return 'ok'
        with patch.object(BUILD, 'SLEEP', waits.append):
            self.assertEqual(BUILD.retry('阶段', flaky), 'ok')
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [BUILD.RETRY_WAIT_SECONDS])

    def test_gives_up_after_limit(self):
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError('网络')
        with patch.object(BUILD, 'SLEEP', lambda _: None), self.assertRaises(RuntimeError):
            BUILD.retry('阶段', broken)
        self.assertEqual(len(calls), BUILD.RETRY_ATTEMPTS)
        self.assertLessEqual(BUILD.RETRY_ATTEMPTS, 2)

    def test_non_retryable_error_stops_immediately(self):
        calls = []

        def wrong():
            calls.append(1)
            raise ValueError('提交不属于主线')
        with patch.object(BUILD, 'SLEEP', lambda _: None), self.assertRaises(ValueError):
            BUILD.retry('读取私库源码', wrong, (RuntimeError,))
        self.assertEqual(len(calls), 1)

    def test_checkout_cleans_partial_directory_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            seen = []

            def once():
                seen.append(root.exists())
                root.mkdir()
                if len(seen) == 1:
                    raise RuntimeError('网络')
            with patch.object(BUILD, 'ROOT', root), patch.object(BUILD, 'DEPLOY_MANIFEST', Path(directory) / 'm.yaml'), \
                    patch.object(BUILD, 'checkout_once', once), patch.object(BUILD, 'SLEEP', lambda _: None), \
                    patch.dict(BUILD.os.environ, {'SOURCE_SHA': 'a' * 40}):
                BUILD.checkout()
            self.assertEqual(seen, [False, False])


class Push(unittest.TestCase):
    def test_timeout_becomes_retryable_stage_failure(self):
        import subprocess
        with patch.object(BUILD.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 1)):
            with self.assertRaises(RuntimeError):
                BUILD.run(['docker', 'push', 'x'], timeout=1)

    def test_ghcr_push_checks_local_repo_digest_without_acr_fallback(self):
        commands = []
        digest = 'sha256:' + 'a' * 64

        def fake(args, **kwargs):
            commands.append(args)
            if args[:3] == ['docker', 'image', 'inspect']:
                return json.dumps([BUILD.ORIGIN_IMAGE + '@' + digest]).encode()
            return b''
        with patch.object(BUILD, 'run', fake):
            self.assertEqual(BUILD.push_image(None, 'svc-v1.0.1', Path('/tmp'), {}), digest)
        self.assertEqual(commands[1], ['docker', 'push', 'ghcr.io/sdbcbt/cloudlinkpos:svc-v1.0.1'])

    def test_fleet_floor_prevents_empty_ghcr_reset(self):
        self.assertEqual(BUILD.next_version('svc', [], ['svc-v2.3.99']), 'svc-v2.3.100')
        self.assertEqual(BUILD.next_version('svc', ['svc-v2.4.1'], ['svc-v2.3.99']), 'svc-v2.4.2')

    def test_fleet_records_include_stable_and_candidate_and_reject_invalid_digest(self):
        row = {'tag': 'svc-v2.0.0', 'digest': 'sha256:' + 'a' * 64, 'repository': BUILD.IMAGE}
        self.assertEqual(BUILD.fleet_records(json.dumps({'stable': {'services': {'svc': row}}, 'canary': {'svc': row}})), [row, row])
        with self.assertRaises(ValueError):
            BUILD.fleet_records('{}')
        row['digest'] = 'invalid'
        with self.assertRaises(ValueError):
            BUILD.fleet_records(json.dumps({'stable': row}))


class Baseline(unittest.TestCase):
    def execute(self, value, sha='a' * 40):
        calls = []
        path = 'deploy/ci/registry-version-baseline.json'
        def run(args, cwd=None):
            calls.append(args)
            if args[1] == 'ls-tree':
                return (path + '\n').encode() if value is not None else b''
            return json.dumps(value).encode()
        with patch.dict(BUILD.os.environ, {'SOURCE_SHA': sha}), patch.object(BUILD, 'run', side_effect=run):
            return BUILD.baseline_floors(), calls

    def test_baseline_is_read_from_input_sha_not_head_or_working_copy(self):
        value = {'schema': 'cloudlinkpos.registry-baseline/v1', 'services': {'applyment-api': 'applyment-api-v0.3.88'}}
        floors, calls = self.execute(value)
        self.assertEqual(floors, value['services'])
        self.assertEqual(calls[-1], ['git', 'show', 'a' * 40 + ':deploy/ci/registry-version-baseline.json'])
        self.assertEqual(BUILD.next_version('applyment-api', ['applyment-api-v0.3.90'], floors.values()), 'applyment-api-v0.3.91')

    def test_missing_optional_baseline_returns_no_floor(self):
        self.assertEqual(self.execute(None)[0], {})

    def test_wrong_schema_cross_service_tag_and_invalid_sha_are_rejected(self):
        for value in ({'schema': 'other', 'services': {}}, {'schema': 'cloudlinkpos.registry-baseline/v1', 'services': {'applyment-api': 'other-v9.0.0'}}):
            with self.assertRaises(ValueError):
                self.execute(value)
        with self.assertRaises(ValueError):
            self.execute(None, 'main')


class Concurrency(unittest.TestCase):
    def test_queue_is_per_service_not_whole_workflow(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/full-version-build.yml').read_text())
        self.assertNotIn('concurrency', workflow)
        build = workflow['jobs']['build']['concurrency']
        self.assertEqual(build['group'], 'full-version-publication-${{ matrix.service }}')
        self.assertFalse(build['cancel-in-progress'])
        self.assertEqual(build['queue'], 'max')
        receipts = workflow['jobs']['publish-receipts']['concurrency']
        self.assertEqual(receipts['group'], 'full-version-receipts')
        self.assertEqual(receipts['queue'], 'max')
        self.assertNotIn('concurrency', workflow['jobs']['plan'])
        self.assertEqual(workflow['jobs']['build']['timeout-minutes'], 120)


if __name__ == '__main__':
    unittest.main()
