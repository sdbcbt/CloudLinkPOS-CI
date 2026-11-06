"""验证收银端触发范围、主线边界和私库输出隔离。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('pos_build', Path(__file__).with_name('pos-build.py'))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ScopeTests(unittest.TestCase):
    def test_only_pos_and_packaging_paths_select_client_builds(self):
        for path in ['apps/pos/lib/main.dart', 'dev/packaging/pos_release.py',
                     'dev/development/build-pos-windows.ps1', '.github/workflows/build-pos-release.yml']:
            with self.subTest(path=path):
                self.assertTrue(build.changed([path]))
        for path in ['apps/backend/go.mod', 'apps/admin/package.json', 'README.md', 'docs/pos.md',
                     '.github/workflows/release-service.yml', 'apps/pos-other/lib.dart']:
            with self.subTest(path=path):
                self.assertFalse(build.changed([path]))

    def test_unrelated_main_push_emits_empty_platforms(self):
        fake = Mock()
        fake.make_plan.return_value = {'schema': 'synthetic', 'repository': 'sdbcbt/CloudLinkPOS',
            'sourceSHA': 'b' * 40, 'sourceTreeSHA': 'c' * 40, 'sourceRevision': 17,
            'version': '1.0.17', 'buildNumber': 17, 'softwareVersion': '1.0.17+17', 'privateField': 'never public'}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out'
            env = {'BUILD_PLATFORM': 'changed', 'SOURCE_SHA': 'b' * 40, 'SOURCE_BEFORE_SHA': 'a' * 40, 'GITHUB_OUTPUT': str(output)}
            with patch.dict(os.environ, env), patch.object(build, 'module', return_value=fake), \
                    patch.object(build, 'run', side_effect=[('b' * 40 + '\n' + 'a' * 40).encode(), b'docs/plan.md\n']):
                build.plan()
            result = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(result['matrix']), {'platform': []})
            self.assertEqual(result['has_platforms'], 'false')
            self.assertNotIn('privateField', result['plan'])
            self.assertNotIn('never public', result['plan'])

    def test_push_baseline_cannot_be_missing_or_non_main_history(self):
        for before in ['', 'bad', '0' * 40, 'a' * 40]:
            with self.subTest(before=before), patch.dict(os.environ, {'BUILD_PLATFORM': 'changed', 'SOURCE_SHA': 'b' * 40, 'SOURCE_BEFORE_SHA': before}), \
                    patch.object(build, 'run', return_value=('b' * 40).encode()), self.assertRaises(ValueError):
                build.plan()

    def test_formal_build_requires_public_main(self):
        for repo, ref in [('sdbcbt/CloudLinkPOS', 'refs/heads/main'), ('sdbcbt/CloudLinkPOS-CI', 'refs/heads/test')]:
            with self.subTest(repo=repo, ref=ref), patch.dict(os.environ, {'GITHUB_REPOSITORY': repo, 'GITHUB_REF': ref}), self.assertRaises(ValueError):
                build.main('checkout')


class PrivacyTests(unittest.TestCase):
    def test_private_command_output_is_not_in_error(self):
        result = subprocess.CompletedProcess(['synthetic'], 1, stdout=b'private source text', stderr=b'secret value')
        with patch.object(build.subprocess, 'run', return_value=result), self.assertRaises(RuntimeError) as error:
            build.run(['synthetic'])
        self.assertNotIn('private', str(error.exception))
        self.assertNotIn('secret', str(error.exception))

    def test_failed_private_subprocess_never_leaks_to_public_console(self):
        script = Path(__file__).with_name('pos-build.py').resolve()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = root / 'source/dev/packaging'
            private.mkdir(parents=True)
            (private / 'test_private.py').write_text("print('synthetic private source')\nraise RuntimeError('synthetic secret')\n")
            env = dict(os.environ, GITHUB_REPOSITORY='sdbcbt/CloudLinkPOS-CI', GITHUB_REF='refs/heads/main')
            result = subprocess.run([os.sys.executable, str(script), 'test'], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('synthetic private source', result.stdout + result.stderr)
            self.assertNotIn('synthetic secret', result.stdout + result.stderr)
            self.assertIn('收银客户端构建停止于：test', result.stderr)

    def test_windows_tools_always_run_with_captured_output(self):
        result = subprocess.CompletedProcess(['synthetic'], 0, stdout=b'captured', stderr=b'captured')
        with patch.object(build.subprocess, 'run', return_value=result) as invoke:
            self.assertEqual(build.run(['synthetic']), b'captured')
        self.assertEqual(invoke.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(invoke.call_args.kwargs['stderr'], subprocess.PIPE)


class SigningTests(unittest.TestCase):
    def test_only_the_fixed_cloudlinkpos_release_certificate_is_accepted(self):
        fixed = 'Signer #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256 + '\n'
        build.verify_signer(fixed)
        for output in ['', 'Signer #1 certificate SHA-256 digest: ' + '0' * 64,
                       fixed + 'Signer #2 certificate SHA-256 digest: ' + 'f' * 64]:
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, '固定正式证书'):
                build.verify_signer(output)


class WorkflowTests(unittest.TestCase):
    def test_public_pos_workflow_has_no_source_artifacts_or_write_source_token(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/build-pos-release.yml').read_text())
        self.assertFalse(workflow['permissions'])
        self.assertEqual(workflow['defaults']['run']['shell'], 'bash')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(workflow['concurrency']['queue'], 'max')
        jobs = workflow['jobs']
        self.assertEqual(jobs['plan']['runs-on'], 'ubuntu-24.04')
        self.assertEqual(jobs['build']['permissions'], {'contents': 'write'})
        for job in jobs.values():
            for step in job['steps']:
                uses = step.get('uses', '')
                self.assertNotIn('upload-artifact', uses)
                if 'create-github-app-token' in uses:
                    self.assertEqual(step['with']['permission-contents'], 'read')
                    self.assertEqual(step['with']['repositories'], 'CloudLinkPOS')
                if 'actions/checkout' in uses:
                    self.assertEqual(step['with']['path'], 'automation')
                    self.assertIs(step['with']['persist-credentials'], False)
                if step.get('env', {}).get('GITHUB_TOKEN'):
                    self.assertIn('pos-build.py publish', step['run'])
        main = yaml.safe_load((root / '.github/workflows/private-main.yml').read_text())
        self.assertNotIn('pos', main['jobs'])
        self.assertEqual(main['jobs']['build']['uses'], './.github/workflows/full-version-build.yml')



if __name__ == '__main__':
    unittest.main()
