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
            env = dict(os.environ, GITHUB_REPOSITORY='sdbcbt/CloudLinkPOS-CI', GITHUB_REF='refs/heads/main', RUNNER_TEMP=folder)
            result = subprocess.run([os.sys.executable, str(script), 'test'], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('synthetic private source', result.stdout + result.stderr)
            self.assertNotIn('synthetic secret', result.stdout + result.stderr)
            self.assertIn('收银客户端构建停止于：test', result.stderr)
            saved = (root / 'cloudlinkpos-pos-first-failure.log').read_text(encoding='utf-8')
            self.assertIn('synthetic private source', saved)
            self.assertIn('synthetic secret', saved)

    def test_first_failure_is_private_and_cleanup_failure_does_not_overwrite_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'RUNNER_TEMP': folder}):
            for stdout, stderr in [(b'first stdout', b'first stderr'), (b'cleanup stdout', b'cleanup stderr')]:
                result = subprocess.CompletedProcess(['synthetic'], 7, stdout=stdout, stderr=stderr)
                with patch.object(build.subprocess, 'run', return_value=result), self.assertRaises(build.BuildFailure):
                    build.run(['synthetic'])
            path = Path(folder) / 'cloudlinkpos-pos-first-failure.log'
            self.assertEqual(path.read_bytes(), b'stdout:\nfirst stdout\nstderr:\nfirst stderr')
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failure_log_write_error_does_not_replace_original_failure(self):
        result = subprocess.CompletedProcess(['synthetic'], 9, stdout=b'private', stderr=b'private')
        with patch.dict(os.environ, {'RUNNER_TEMP': '/synthetic/missing-directory'}), \
                patch.object(build.subprocess, 'run', return_value=result), self.assertRaises(build.BuildFailure) as error:
            build.run(['synthetic'])
        self.assertEqual(error.exception.returncode, 9)

    def test_windows_tools_always_run_with_captured_output(self):
        result = subprocess.CompletedProcess(['synthetic'], 0, stdout=b'captured', stderr=b'captured')
        with patch.object(build.subprocess, 'run', return_value=result) as invoke:
            self.assertEqual(build.run(['synthetic']), b'captured')
        self.assertEqual(invoke.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(invoke.call_args.kwargs['stderr'], subprocess.PIPE)


class EncodingTests(unittest.TestCase):
    def successful_checkout(self, extra_env):
        script = Path(__file__).with_name('pos-build.py').resolve()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / 'automation/ci/full-build.py'
            checkout.parent.mkdir(parents=True)
            # 假读取只留下完成标记，覆盖真实入口的成功输出，不访问私库或令牌。
            checkout.write_text(
                "from pathlib import Path\n\ndef checkout():\n"
                "    Path('checkout-completed').write_text('ok', encoding='ascii')\n",
                encoding='utf-8',
            )
            env = dict(os.environ, GITHUB_REPOSITORY='sdbcbt/CloudLinkPOS-CI', GITHUB_REF='refs/heads/main',
                       PYTHONUTF8='0', PYTHONIOENCODING='cp1252')
            env.pop('SOURCE_TOKEN', None)
            env.update(extra_env)
            result = subprocess.run([os.sys.executable, str(script), 'checkout'], cwd=root, env=env, capture_output=True)
            self.assertEqual((root / 'checkout-completed').read_text(encoding='ascii'), 'ok')
            return result

    def test_legacy_windows_stdout_can_misreport_successful_checkout_as_failure(self):
        result = self.successful_checkout({})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'::error::\\u6536\\u94f6', result.stderr)
        self.assertIn(b'checkout', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)

    def test_workflow_utf8_environment_keeps_successful_checkout_successful(self):
        import yaml
        workflow = Path(__file__).resolve().parents[1] / '.github/workflows/build-pos-release.yml'
        environment = yaml.safe_load(workflow.read_text(encoding='utf-8'))['env']
        result = self.successful_checkout(environment)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode('utf-8').splitlines(), ['本阶段完成：checkout'])
        self.assertEqual(result.stderr, b'')


class DiagnosticTests(unittest.TestCase):
    def test_error_categories_are_fixed_and_narrow_features_take_priority(self):
        cases = [
            (b'Missing classes detected while running R8\nCompilation failed', 'r8_missing_classes'),
            (b'R8: Missing class private.Class\nCould not download secret', 'r8_missing_classes'),
            (b'Could not resolve all files for configuration /private/path\nCompilation failed', 'dependency_resolution'),
            (b'Version solving failed\nprivate package', 'dependency_resolution'),
            (b'Target kernel_snapshot_program failed\nprivate.dart:10', 'compilation'),
            (b'Release signing requires private/key.properties', 'signing'),
            (b'\xff\xfe secret=never-public\nunknown failure', 'unknown'),
        ]
        for output, category in cases:
            with self.subTest(category=category):
                self.assertEqual(build.classify_failure(output), category)
        error = build.BuildFailure('private/path', 'secret=never-public')
        self.assertEqual(error.returncode, 'none')
        self.assertEqual(error.category, 'unknown')

    def test_test_subphases_stop_at_the_failed_command(self):
        env = {'GITHUB_REPOSITORY': 'sdbcbt/CloudLinkPOS-CI', 'GITHUB_REF': 'refs/heads/main'}
        for index, stage in enumerate(['test.python_packaging', 'test.flutter_pub_get', 'test.flutter_cases']):
            with self.subTest(stage=stage), patch.dict(os.environ, env), \
                    patch.object(build, 'run', side_effect=[b''] * index + [build.BuildFailure(17, 'unknown')]) as invoke:
                with self.assertRaises(build.BuildFailure):
                    build.main('test')
                self.assertEqual(build.SUBSTAGE, stage)
                self.assertEqual(invoke.call_count, index + 1)

    def test_android_compile_and_missing_tools_have_distinct_subphases(self):
        env = {'POS_VERSION': '1.0.1', 'POS_BUILD_NUMBER': '1', 'POS_SOURCE_SHA': 'a' * 40}
        with patch.dict(os.environ, env), patch.object(build, 'flutter', side_effect=build.BuildFailure(19, 'compilation')), \
                patch.object(build, 'run') as invoke, self.assertRaises(build.BuildFailure):
            build.android()
        self.assertEqual(build.SUBSTAGE, 'android.compile')
        invoke.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'build-tools').mkdir()
            with patch.dict(os.environ, dict(env, ANDROID_HOME=folder)), patch.object(build, 'flutter'), \
                    patch.object(build, 'run') as invoke, self.assertRaises(ValueError):
                build.android()
            self.assertEqual(build.SUBSTAGE, 'android.signing_tools')
            invoke.assert_not_called()

    def test_android_signature_fingerprint_and_version_failures_stop_in_their_subphases(self):
        signature = ('Number of signers: 1\nSigner #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256 + '\n').encode()
        cases = [
            ('android.signing_tools', [build.BuildFailure(21, 'unknown')], 1),
            ('android.signer_fingerprint', [b'private invalid certificate'], 1),
            ('android.version', [signature, build.BuildFailure(23, 'unknown')], 2),
        ]
        for stage, results, count in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                tools = root / 'build-tools/35.0.0'
                tools.mkdir(parents=True)
                (tools / 'apksigner').touch()
                env = {'POS_VERSION': '1.0.1', 'POS_BUILD_NUMBER': '1', 'POS_SOURCE_SHA': 'a' * 40,
                       'ANDROID_HOME': folder, 'GITHUB_ENV': str(root / 'env'), 'RUNNER_TEMP': folder}
                with patch.dict(os.environ, env), patch.object(build, 'flutter'), \
                        patch.object(build, 'run', side_effect=results) as invoke, patch.object(build, 'module') as load:
                    with self.assertRaises((build.BuildFailure, ValueError)):
                        build.android()
                    self.assertEqual(build.SUBSTAGE, stage)
                    self.assertEqual(invoke.call_count, count)
                    load.assert_not_called()
                    if stage == 'android.signer_fingerprint':
                        saved = (root / 'cloudlinkpos-pos-first-failure.log').read_bytes()
                        self.assertIn(b'private invalid certificate', saved)

    def test_actual_entry_reports_only_safe_compile_metadata(self):
        script = Path(__file__).with_name('pos-build.py').resolve()
        code = """
import runpy, subprocess, sys
from unittest.mock import patch
failure = subprocess.CompletedProcess(['private argument'], 37, stdout=b'private stdout',
    stderr=b'Missing classes detected while running R8\\n/private/source.dart secret=never-public')
sys.argv = [sys.argv[1], 'android']
with patch.object(subprocess, 'run', return_value=failure):
    runpy.run_path(sys.argv[0], run_name='__main__')
"""
        env = dict(os.environ, GITHUB_REPOSITORY='sdbcbt/CloudLinkPOS-CI', GITHUB_REF='refs/heads/main',
                   POS_VERSION='1.0.1', POS_BUILD_NUMBER='1', POS_SOURCE_SHA='a' * 40, PYTHONIOENCODING='utf-8')
        env.pop('SOURCE_TOKEN', None)
        result = subprocess.run([os.sys.executable, '-c', code, str(script)], env=env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr.decode('utf-8').splitlines(), [
            '::error::收银客户端构建停止于：android；子阶段：android.compile；exit_code=37；category=r8_missing_classes；请在私库受控环境复现。',
        ])

    def test_actual_entry_masks_internal_version_errors_without_inventing_an_exit_code(self):
        script = Path(__file__).with_name('pos-build.py').resolve()
        code = """
import runpy, subprocess, sys
from unittest.mock import patch
signature = b'Number of signers: 1\\nSigner #1 certificate SHA-256 digest: b118b7c8f118d1c8b1c1115856615ee7ae1076022049a2c71f9190901a7c7d9e\\n'
results = [subprocess.CompletedProcess([], 0, stdout=value, stderr=b'private output')
    for value in (b'', signature, b'private badging')]
sys.argv = [sys.argv[1], 'android']
with patch.object(subprocess, 'run', side_effect=results):
    runpy.run_path(sys.argv[0], run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tools = root / 'build-tools/35.0.0'
            tools.mkdir(parents=True)
            (tools / 'apksigner').touch()
            verifier = root / 'source/dev/packaging/verify_android_version.py'
            verifier.parent.mkdir(parents=True)
            verifier.write_text("def verify(*args):\n    raise ValueError('/private/source secret=never-public')\n", encoding='utf-8')
            env = dict(os.environ, GITHUB_REPOSITORY='sdbcbt/CloudLinkPOS-CI', GITHUB_REF='refs/heads/main',
                       POS_VERSION='1.0.1', POS_BUILD_NUMBER='1', POS_SOURCE_SHA='a' * 40, PYTHONIOENCODING='utf-8',
                       ANDROID_HOME=folder, GITHUB_ENV=str(root / 'env'))
            env.pop('SOURCE_TOKEN', None)
            result = subprocess.run([os.sys.executable, '-c', code, str(script)], cwd=root, env=env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr.decode('utf-8').splitlines(), [
            '::error::收银客户端构建停止于：android；子阶段：android.version；exit_code=none；category=unknown；请在私库受控环境复现。',
        ])


class SigningTests(unittest.TestCase):
    def test_only_the_fixed_cloudlinkpos_release_certificate_is_accepted(self):
        fixed = 'Number of signers: 1\nSigner #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256 + '\n'
        build.verify_signer(fixed)
        modern = fixed.replace('Signer #1 certificate', 'V2 Signer: certificate')
        build.verify_signer(modern)
        for output in ['', 'Signer #1 certificate SHA-256 digest: ' + '0' * 64,
                       fixed + 'Signer #2 certificate SHA-256 digest: ' + 'f' * 64,
                       modern.replace('Number of signers: 1', 'Number of signers: 2'),
                       modern.replace(build.ANDROID_SIGNER_CERT_SHA256, '0' * 64),
                       modern.replace('V2 Signer:', 'Unsupported Signer:'),
                       modern + modern, modern.split('\n', 1)[1]]:
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, '固定正式证书'):
                build.verify_signer(output)


class WorkflowTests(unittest.TestCase):
    def test_public_pos_workflow_has_no_source_artifacts_or_write_source_token(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/build-pos-release.yml').read_text())
        self.assertFalse(workflow['permissions'])
        self.assertEqual(workflow['env']['PYTHONUTF8'], '1')
        self.assertEqual(workflow['env']['PYTHONIOENCODING'], 'utf-8')
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
                    if step.get('id') == 'failure_auth':
                        self.assertEqual(step['with']['permission-checks'], 'write')
                        self.assertEqual(step['if'], "failure() && steps.failure_log.outputs.exists == 'true'")
                        self.assertEqual(set(step['with']), {'app-id', 'private-key', 'owner', 'repositories', 'permission-contents', 'permission-checks'})
                    else:
                        self.assertNotIn('permission-checks', step['with'])
                if 'actions/checkout' in uses:
                    self.assertEqual(step['with']['path'], 'automation')
                    self.assertIs(step['with']['persist-credentials'], False)
                if step.get('env', {}).get('GITHUB_TOKEN'):
                    self.assertIn(step['run'], ['python automation/ci/pos-build.py publish', 'python automation/ci/report-pos-failure.py'])
        report = jobs['build']['steps'][-1]
        self.assertEqual(report['run'], 'python automation/ci/report-pos-failure.py')
        self.assertEqual(report['if'], "failure() && steps.failure_log.outputs.exists == 'true' && steps.failure_auth.outcome == 'success'")
        self.assertEqual(report['env']['CHECKS_TOKEN'], '${{ steps.failure_auth.outputs.token }}')
        for key in ['ANDROID_KEYSTORE_BASE64', 'ANDROID_KEYSTORE_PASSWORD', 'ANDROID_KEY_PASSWORD', 'ANDROID_KEY_ALIAS']:
            self.assertEqual(report['env'][key], '${{ secrets.' + key + ' }}')
        main = yaml.safe_load((root / '.github/workflows/private-main.yml').read_text())
        self.assertNotIn('pos', main['jobs'])
        self.assertEqual(main['jobs']['build']['uses'], './.github/workflows/full-version-build.yml')



if __name__ == '__main__':
    unittest.main()
