"""验证收银端触发范围、主线边界和私库输出隔离。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('pos_build', Path(__file__).with_name('pos-build.py'))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
real_module = build.module


class ScopeTests(unittest.TestCase):
    def test_only_pos_and_packaging_paths_select_client_builds(self):
        for path in ['apps/pos/lib/main.dart', 'dev/packaging/pos_release.py',
                     'dev/development/build-pos-windows.ps1', '.github/workflows/build-pos-release.yml',
                     '.github/workflows/build-terminal-release.yml']:
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
            env = {'BUILD_PRODUCT': 'pos', 'BUILD_PLATFORM': 'changed', 'SOURCE_SHA': 'b' * 40, 'SOURCE_BEFORE_SHA': 'a' * 40, 'GITHUB_OUTPUT': str(output)}
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
            with self.subTest(before=before), patch.dict(os.environ, {'BUILD_PRODUCT': 'pos', 'BUILD_PLATFORM': 'changed', 'SOURCE_SHA': 'b' * 40, 'SOURCE_BEFORE_SHA': before}), \
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
        workflow = Path(__file__).resolve().parents[1] / '.github/workflows/client-release.yml'
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


def fake_source(platforms=None):
    fake = Mock()
    if platforms is not None:
        fake.PLATFORMS = platforms
    else:
        del fake.PLATFORMS
    fake.make_plan.return_value = {'schema': 'synthetic', 'repository': 'sdbcbt/CloudLinkPOS',
        'sourceSHA': 'b' * 40, 'sourceTreeSHA': 'c' * 40, 'sourceRevision': 17,
        'version': '1.0.17', 'buildNumber': 17, 'softwareVersion': '1.0.17+17'}
    return fake


GRADLE_FAILURE = b"""FAILURE: Build failed with an exception.
* What went wrong:
Execution failed for task ':app:checkReleaseAarMetadata'.
> Could not resolve all files for configuration ':app:releaseRuntimeClasspath'.
   > Could not GET 'https://repo.maven.apache.org/maven2/org/jetbrains/kotlin/kotlin-bom/2.0.21/kotlin-bom-2.0.21.pom'. Received status code 429 from server: Too Many Requests
   > Could not HEAD 'https://x-access-token:ghs_secretTokenValue@maven.pkg.github.com/sdbcbt/CloudLinkPOS/private.pom'. Received status code 401 from server: Unauthorized
   > Could not GET 'http://10.0.0.8:8081/nexus/private.pom'. Received status code 503 from server
   > Could not GET 'https://plugins.gradle.org:443/m2/org/jetbrains/kotlin/kotlin-gradle-plugin-api/2.0.21/x.pom'.
Execution failed for task ':private plugin;rm -rf'.
ANDROID_KEY_PASSWORD=never-public /home/runner/work/source/apps/pos/android
"""


class GradleSummaryTests(unittest.TestCase):
    def test_summary_keeps_only_public_hosts_status_codes_and_gradle_paths(self):
        summary = build.gradle_failure_summary(GRADLE_FAILURE, 2)
        self.assertEqual(summary, {
            'repositoryHosts': ['other', 'plugins.gradle.org', 'repo.maven.apache.org'],
            'httpStatus': [401, 429, 503],
            'failedTasks': [':app:checkReleaseAarMetadata'],
            'configurations': [':app:releaseRuntimeClasspath'],
            'retries': 2,
        })
        text = json.dumps(summary)
        for secret in ['ghs_secretTokenValue', 'x-access-token', 'maven.pkg.github.com', 'sdbcbt', 'CloudLinkPOS', '10.0.0.8',
                       'nexus', 'never-public', '/home/runner', 'rm -rf']:
            self.assertNotIn(secret, text)

    def test_task_and_configuration_paths_must_be_gradle_identifiers_without_secret_values(self):
        output = (b"Execution failed for task ':app:10.0.0.8'.\n"
                  b"Execution failed for task ':app:1secret'.\n"
                  b"Execution failed for task ':app:a.b'.\n"
                  b"Execution failed for task ':app:SYNTHETIC_SECRET_123'.\n"
                  b"Execution failed for task ':flutter_plugin_android_lifecycle:compileReleaseKotlin'.\n"
                  b"> Could not resolve all files for configuration 'classpath'.\n"
                  b"> Could not resolve all files for configuration ':app:x_SYNTHETIC_SECRET_123_y'.\n")
        with patch.dict(os.environ, {'ANDROID_KEY_PASSWORD': 'SYNTHETIC_SECRET_123', 'SHORT_TOKEN': 'app'}):
            summary = build.gradle_failure_summary(output)
        self.assertEqual(summary['failedTasks'], [':flutter_plugin_android_lifecycle:compileReleaseKotlin'])
        self.assertEqual(summary['configurations'], ['classpath'])
        self.assertNotIn('10.0.0.8', json.dumps(summary))
        # 已知限制：合法标识符形态且不含环境密钥值的名称会原样公开。
        with patch.dict(os.environ, {'ANDROID_KEY_PASSWORD': 'unrelated-value'}):
            self.assertIn(':app:SYNTHETIC_SECRET_123', build.gradle_failure_summary(output)['failedTasks'])

    def test_summary_is_bounded_and_empty_for_non_gradle_output(self):
        self.assertFalse(any(build.gradle_failure_summary(b'private source text\nsecret value').values()))
        many = b''.join(b"Execution failed for task ':m%d:x'.\n" % index for index in range(50))
        self.assertEqual(len(build.gradle_failure_summary(many)['failedTasks']), build.SUMMARY_LIMIT)

    def test_entry_publishes_redacted_summary_after_retries_and_private_log_is_unchanged(self):
        script = Path(__file__).with_name('pos-build.py').resolve()
        code = """
import runpy, subprocess, sys, time
from unittest.mock import patch
failure = subprocess.CompletedProcess(['private argument'], 1, stdout=sys.stdin.buffer.read(), stderr=b'')
sys.argv = [sys.argv[1], 'android']
with patch.object(subprocess, 'run', return_value=failure) as invoke, patch.object(time, 'sleep'):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    finally:
        print('attempts=%d' % invoke.call_count)
"""
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, GITHUB_REPOSITORY='sdbcbt/CloudLinkPOS-CI', GITHUB_REF='refs/heads/main', RUNNER_TEMP=folder,
                       GRADLE_USER_HOME=str(Path(folder) / 'gradle'), POS_VERSION='1.0.1', POS_BUILD_NUMBER='1',
                       POS_SOURCE_SHA='a' * 40, PYTHONIOENCODING='utf-8')
            result = subprocess.run([os.sys.executable, '-c', code, str(script)], env=env, input=GRADLE_FAILURE,
                                    capture_output=True, cwd=folder)
            private = (Path(folder) / 'cloudlinkpos-pos-first-failure.log').read_bytes()
        self.assertEqual(result.returncode, 1)
        stdout, stderr = result.stdout.decode('utf-8'), result.stderr.decode('utf-8')
        # 摘要与 429 重试分属两个提交、可分别回滚：未启用重试时重试次数为 0。
        retries = build.GRADLE_RETRY_LIMIT if hasattr(build, 'run_gradle') else 0
        for attempt in range(1, retries + 1):
            self.assertIn(f'第 {attempt}/{retries} 次重试', stdout)
        self.assertIn('attempts=%d' % (retries + 1), stdout)
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('Gradle 构建失败摘要（已脱敏）：'))
        self.assertEqual(json.loads(lines[0].split('：', 1)[1])['retries'], retries)
        self.assertIn('"httpStatus": [401, 429, 503]', lines[0])
        self.assertTrue(lines[1].startswith('::error::收银客户端构建停止于：android；子阶段：android.compile；'))
        for secret in ['ghs_secretTokenValue', 'maven.pkg.github.com', '10.0.0.8', 'never-public', 'private argument']:
            self.assertNotIn(secret, stdout + stderr)
        # 私库 CheckRun 使用的私有日志仍是完整原文。
        self.assertIn(b'ghs_secretTokenValue', private)


class FakeScope:
    """模拟新源码 dev/packaging/client_scope.py：按固定表返回受影响产品。"""
    TABLE = {
        'apps/pos/lib/features/terminal/a.dart': ['terminal'],
        'apps/pos/lib/features/cashier/a.dart': ['pos'],
        'apps/pos/lib/shared/a.dart': ['pos', 'terminal'],
    }

    def __init__(self):
        self.calls = []

    def scopes(self, root, paths):
        self.calls.append((root, list(paths)))
        return sorted({item for path in paths for item in self.TABLE.get(path, [])})


class ProductPlanTests(unittest.TestCase):
    def planned(self, product, selected, source, entry=True, scope=None, paths=b'apps/pos/lib/shared/a.dart\n'):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'
            (root / 'apps/pos/lib').mkdir(parents=True)
            (root / 'dev/packaging').mkdir(parents=True)
            if entry:
                (root / 'apps/pos/lib/main_terminal.dart').write_text('void main() {}\n')
            if scope is not None:
                (root / 'dev/packaging/client_scope.py').write_text('# synthetic\n')
            output = Path(folder) / 'out'
            env = {'BUILD_PRODUCT': product, 'BUILD_PLATFORM': selected, 'SOURCE_SHA': 'b' * 40,
                   'SOURCE_BEFORE_SHA': 'a' * 40, 'GITHUB_OUTPUT': str(output)}
            history = ('b' * 40 + '\n' + 'a' * 40).encode()
            modules = lambda name, path: scope if name == 'client_scope' else source
            with patch.object(build, 'ROOT', root), patch.dict(os.environ, env), patch.object(build, 'module', side_effect=modules), \
                    patch.object(build, 'run', side_effect=[history, paths]):
                build.plan()
            result = dict(line.split('=', 1) for line in output.read_text().splitlines())
            return json.loads(result['matrix'])['platform']

    def test_products_never_share_a_run(self):
        supported = fake_source(('windows', 'android', 'terminal-android'))
        for product, expected in [('pos', ['windows', 'android']), ('terminal', ['terminal-android'])]:
            with self.subTest(product=product):
                self.assertEqual(self.planned(product, 'all', supported), expected)
                self.assertEqual(self.planned(product, 'changed', supported, scope=FakeScope()), expected)
        self.assertEqual(self.planned('pos', 'android', supported), ['android'])
        self.assertEqual(self.planned('terminal', 'terminal-android', supported), ['terminal-android'])

    def test_three_trigger_kinds_use_source_scope(self):
        supported = fake_source(('windows', 'android', 'terminal-android'))
        cases = [
            (b'apps/pos/lib/features/terminal/a.dart\n', [], ['terminal-android']),
            (b'apps/pos/lib/features/cashier/a.dart\n', ['windows', 'android'], []),
            (b'apps/pos/lib/shared/a.dart\n', ['windows', 'android'], ['terminal-android']),
            (b'apps/backend/go.mod\n', [], []),
        ]
        for paths, pos, terminal in cases:
            with self.subTest(paths=paths):
                scope = FakeScope()
                self.assertEqual(self.planned('pos', 'changed', supported, scope=scope, paths=paths), pos)
                self.assertEqual(self.planned('terminal', 'changed', supported, scope=scope, paths=paths), terminal)
                self.assertEqual([call[1] for call in scope.calls], [[paths.decode().strip()]] * 2)

    def test_legacy_source_without_scope_module_keeps_safe_path_rule(self):
        supported = fake_source(('windows', 'android', 'terminal-android'))
        for path in [b'apps/pos/lib/features/terminal/a.dart\n', b'apps/pos/lib/features/cashier/a.dart\n']:
            with self.subTest(path=path):
                self.assertEqual(self.planned('pos', 'changed', supported, paths=path), ['windows', 'android'])
                self.assertEqual(self.planned('terminal', 'changed', supported, paths=path), ['terminal-android'])
        self.assertEqual(self.planned('terminal', 'changed', supported, paths=b'docs/a.md\n'), [])

    def test_source_without_terminal_support_skips_or_rejects_terminal(self):
        for source, entry in [(fake_source(None), True), (fake_source(('windows', 'android')), True),
                              (fake_source(('windows', 'android', 'terminal-android')), False), (fake_source(['terminal-android']), True)]:
            with self.subTest(platforms=getattr(source, 'PLATFORMS', None), entry=entry):
                self.assertEqual(self.planned('terminal', 'changed', source, entry=entry), [])
                self.assertEqual(self.planned('pos', 'changed', source, entry=entry), ['windows', 'android'])
                for selected in ['terminal-android', 'all']:
                    with self.assertRaisesRegex(ValueError, '尚不支持'):
                        self.planned('terminal', selected, source, entry=entry)

    def test_platform_must_belong_to_product(self):
        supported = fake_source(('windows', 'android', 'terminal-android'))
        for product, selected in [('pos', 'terminal-android'), ('terminal', 'android'), ('terminal', 'windows'), ('other', 'all'), ('', 'all')]:
            with self.subTest(product=product, selected=selected), self.assertRaises(ValueError):
                self.planned(product, selected, supported)

    def test_entry_workflows_dispatch_one_product_each(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        expected = {
            'build-pos-release.yml': ('pos', ['all', 'windows', 'android', 'changed'], 'pos-permanent-releases'),
            'build-terminal-release.yml': ('terminal', ['terminal-android', 'changed'], 'terminal-permanent-releases'),
        }
        for name, (product, options, group) in expected.items():
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((root / '.github/workflows' / name).read_text())
                triggers = workflow[True] if True in workflow else workflow['on']
                self.assertEqual(set(triggers), {'workflow_dispatch'})
                self.assertEqual(triggers['workflow_dispatch']['inputs']['platform']['options'], options)
                self.assertFalse(workflow['permissions'])
                self.assertEqual(workflow['concurrency'], {'group': group, 'cancel-in-progress': False, 'queue': 'max'})
                self.assertEqual(list(workflow['jobs']), ['release'])
                job = workflow['jobs']['release']
                self.assertEqual(job['uses'], './.github/workflows/client-release.yml')
                self.assertEqual(job['with']['product'], product)
                self.assertEqual(job['permissions'], {'contents': 'write'})
                self.assertEqual(job['secrets'], 'inherit')
        shared = yaml.safe_load((root / '.github/workflows/client-release.yml').read_text())
        triggers = shared[True] if True in shared else shared['on']
        self.assertEqual(set(triggers), {'workflow_call'})
        plan = next(step for step in shared['jobs']['plan']['steps'] if step.get('id') == 'plan')
        self.assertEqual(plan['env']['BUILD_PRODUCT'], '${{ inputs.product }}')


class TerminalBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        tools = self.root / 'sdk/build-tools/35.0.0'
        tools.mkdir(parents=True)
        (tools / 'apksigner').touch()
        self.signature = ('Number of signers: 1\nSigner #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256 + '\n').encode()
        self.env = {'POS_VERSION': '1.0.2000', 'POS_BUILD_NUMBER': '2000', 'POS_SOURCE_SHA': 'a' * 40,
                    'ANDROID_HOME': str(self.root / 'sdk'), 'GITHUB_ENV': str(self.root / 'env'),
                    'ORG_GRADLE_PROJECT_POS_UPGRADE_PUBLIC_KEY': 'must-not-leak-into-terminal'}

    def test_terminal_uses_entry_variant_and_verifies_package_version_and_signer(self):
        badging = b"package: name='com.cloudlinkpos.terminal' versionCode='2000' versionName='1.0.2000'\n"
        verifier = Mock()
        with patch.object(build, 'ROOT', self.root), patch.dict(os.environ, self.env), \
                patch.object(build, 'module', return_value=verifier) as load, \
                patch.object(build, 'run', side_effect=[self.signature, badging]), patch.object(build, 'flutter') as flutter:
            build.terminal_android()
            self.assertEqual(os.environ['ORG_GRADLE_PROJECT_CLP_APP_VARIANT'], 'terminal')
            self.assertNotIn('ORG_GRADLE_PROJECT_POS_UPGRADE_PUBLIC_KEY', os.environ)
        args = flutter.call_args.args
        self.assertEqual(args[:5], ('build', 'apk', '--release', '--no-pub', '-t'))
        self.assertEqual(args[5], 'lib/main_terminal.dart')
        self.assertIn('--dart-define=POS_SOURCE_SHA=' + 'a' * 40, args)
        self.assertFalse([item for item in args if 'UPGRADE' in item or 'SIMULATOR' in item])
        load.assert_called_once()
        verifier.verify.assert_called_once_with(badging.decode(), '1.0.2000', 2000, 'com.cloudlinkpos.terminal')
        written = (self.root / 'env').read_text()
        self.assertIn('POS_ANDROID_SIGNER_CERT_SHA256=' + build.ANDROID_SIGNER_CERT_SHA256, written)
        self.assertIn('POS_ANDROID_PACKAGE_NAME=com.cloudlinkpos.terminal', written)
        self.assertEqual(build.SUBSTAGE, 'android.version')

    def test_terminal_rejects_pos_package_or_foreign_signer_before_release_env(self):
        cases = [
            ('android.version', [self.signature, b"package: name='com.cloudlinkpos.pos' versionCode='2000' versionName='1.0.2000'\n"]),
            ('android.signer_fingerprint', [self.signature.replace(build.ANDROID_SIGNER_CERT_SHA256.encode(), b'0' * 64)]),
        ]
        for stage, results in cases:
            with self.subTest(stage=stage), patch.object(build, 'ROOT', self.root), patch.dict(os.environ, dict(self.env, RUNNER_TEMP=str(self.root))), \
                    patch.object(build, 'module') as load, patch.object(build, 'run', side_effect=results), patch.object(build, 'flutter'):
                with self.assertRaises(ValueError):
                    build.terminal_android()
                self.assertEqual(build.SUBSTAGE, stage)
                load.assert_not_called()
                self.assertFalse((self.root / 'env').exists())

    def test_terminal_test_phase_runs_terminal_cases(self):
        env = {'GITHUB_REPOSITORY': 'sdbcbt/CloudLinkPOS-CI', 'GITHUB_REF': 'refs/heads/main', 'RELEASE_PLATFORM': 'terminal-android'}
        with patch.dict(os.environ, env), patch.object(build, 'run'), patch.object(build, 'flutter') as flutter:
            build.main('test')
        self.assertEqual(flutter.call_args.args, ('test', '--no-pub', 'test/terminal', 'test/services/device_information_service_test.dart'))

    def test_terminal_failure_report_and_package_accept_target(self):
        self.assertIn('terminal-android', build.TARGETS)
        env = {'GITHUB_REPOSITORY': 'sdbcbt/CloudLinkPOS-CI', 'GITHUB_REF': 'refs/heads/main', 'RELEASE_PLATFORM': 'terminal-android'}
        with patch.dict(os.environ, env), patch.object(build, 'run') as invoke:
            build.main('package')
        self.assertEqual(invoke.call_args.args[0][1:], ['dev/packaging/pos_release.py', 'package', '--platform', 'terminal-android',
                                                       '--input', 'apps/pos/build/app/outputs/flutter-apk'])
        with patch.dict(os.environ, dict(env, RELEASE_PLATFORM='linux')), self.assertRaises(ValueError):
            build.main('package')


RATE_LIMITED = (b"> Could not GET 'https://repo.maven.apache.org/maven2/org/jetbrains/kotlin/kotlin-stdlib/2.0.21/kotlin-stdlib-2.0.21.pom'. "
                b"Received status code 429 from server: Too Many Requests\n")


class GradleRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runner = self.base / 'runner'
        self.runner.mkdir()
        self.gradle = self.base / 'gradle-home'
        (self.gradle / '.tmp').mkdir(parents=True)
        self.log = self.runner / 'cloudlinkpos-pos-first-failure.log'
        self.env = {'RUNNER_TEMP': str(self.runner), 'GRADLE_USER_HOME': str(self.gradle)}

    def results(self, *outputs):
        # None 代表成功；字节串代表失败输出（由真实 run 写入私有首个失败日志）。
        return [subprocess.CompletedProcess(['gradle'], 0, stdout=b'ok', stderr=b'') if item is None else
                subprocess.CompletedProcess(['gradle'], 1, stdout=item, stderr=b'') for item in outputs]

    def invoke(self, outputs, reset=None):
        printed = []
        with patch.dict(os.environ, self.env), patch.object(build.subprocess, 'run', side_effect=self.results(*outputs)) as invoke, \
                patch.object(build.time, 'sleep') as sleep, patch.object(build.random, 'uniform', side_effect=lambda low, high: high) as jitter, \
                patch('builtins.print', side_effect=lambda *args, **kwargs: printed.append(' '.join(map(str, args)))):
            try:
                result, error = build.run_gradle(['gradle'], self.base, reset=reset), None
            except build.BuildFailure as failure:
                result, error = None, failure
        return result, error, invoke, sleep, jitter, printed

    def test_rate_limit_retries_after_clearing_partial_downloads_then_succeeds(self):
        partial = self.gradle / '.tmp/gradle_download123.bin'
        partial.write_bytes(b'half')
        done = self.gradle / 'caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-stdlib/2.0.21/abc/kotlin-stdlib-2.0.21.pom'
        done.parent.mkdir(parents=True)
        done.write_bytes(b'complete')
        unfinished = done.with_name('kotlin-stdlib-2.0.21.jar.part')
        unfinished.write_bytes(b'half')
        result, error, invoke, sleep, jitter, printed = self.invoke([RATE_LIMITED, None])
        self.assertIsNone(error)
        self.assertEqual(result, b'ok')
        self.assertEqual(invoke.call_count, 2)
        jitter.assert_called_once_with(0, 15.0)
        sleep.assert_called_once_with(45.0)
        self.assertFalse(partial.exists())
        self.assertFalse(unfinished.exists())
        self.assertTrue(done.exists())
        # 成功后不残留首个失败日志，后续阶段失败时私库检查拿到的是真实首个失败。
        self.assertFalse(self.log.exists())
        self.assertEqual(printed, ['Gradle 依赖下载收到 HTTP 429 限流：第 1/2 次重试，已清理未完成下载 2 个，等待 45.0 秒'])

    def test_rate_limit_is_retried_at_most_twice_with_exponential_backoff(self):
        last = RATE_LIMITED + b'attempt three'
        _, error, invoke, sleep, jitter, printed = self.invoke([RATE_LIMITED, RATE_LIMITED, last, None])
        self.assertIsInstance(error, build.BuildFailure)
        self.assertEqual(error.retries, 2)
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual([call.args for call in jitter.call_args_list], [(0, 15.0), (0, 30.0)])
        self.assertEqual([call.args for call in sleep.call_args_list], [(45.0,), (90.0,)])
        self.assertEqual(len(printed), 2)
        self.assertIn('第 2/2 次重试', printed[1])
        # 最终失败只保留最后一次尝试的私有日志。
        self.assertIn(b'attempt three', self.log.read_bytes())

    def test_other_failures_are_not_retried(self):
        for output in [b'Could not resolve all files for configuration\nReceived status code 403 from server: Forbidden',
                       b'Compilation failed; see the compiler error output', b'Received status code 4290 from proxy']:
            with self.subTest(output=output):
                if self.log.exists():
                    self.log.unlink()
                _, error, invoke, sleep, _, printed = self.invoke([output, None])
                self.assertEqual(error.retries, 0)
                self.assertEqual(invoke.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(printed, [])
                self.assertTrue(self.log.exists())

    def test_existing_failure_log_or_missing_runner_temp_disables_retry(self):
        self.log.write_bytes(b'earlier failure')
        _, error, invoke, sleep, _, _ = self.invoke([RATE_LIMITED, None])
        self.assertEqual((error.retries, invoke.call_count), (0, 1))
        sleep.assert_not_called()
        self.assertEqual(self.log.read_bytes(), b'earlier failure')
        del self.env['RUNNER_TEMP']
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('RUNNER_TEMP', None)
            _, error, invoke, sleep, _, _ = self.invoke([RATE_LIMITED, None])
        self.assertEqual((error.retries, invoke.call_count), (0, 1))
        sleep.assert_not_called()

    def test_reset_runs_before_retry_and_reset_failure_keeps_original_failure(self):
        reset = Mock()
        _, error, _, _, _, _ = self.invoke([RATE_LIMITED, None], reset=reset)
        self.assertIsNone(error)
        reset.assert_called_once_with()
        broken = Mock(side_effect=ValueError('拒绝清理'))
        _, error, invoke, sleep, _, _ = self.invoke([RATE_LIMITED, None], reset=broken)
        self.assertEqual((error.retries, invoke.call_count), (0, 1))
        sleep.assert_not_called()
        self.assertIn(b'status code 429', self.log.read_bytes())

    def test_partial_download_cleanup_failure_keeps_original_failure_and_private_log(self):
        with patch.object(build, 'clear_partial_downloads', side_effect=PermissionError('busy')):
            _, error, invoke, sleep, _, printed = self.invoke([RATE_LIMITED, None])
        self.assertIsInstance(error, build.BuildFailure)
        self.assertEqual((error.retries, error.returncode, invoke.call_count), (0, 1, 1))
        sleep.assert_not_called()
        self.assertEqual(printed, [])
        self.assertIn(b'status code 429', self.log.read_bytes())

    def test_only_gradle_build_commands_use_retry(self):
        signature = b'Number of signers: 1\nSigner #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256.encode() + b'\n'
        root = self.base / 'source'
        (root / 'sdk/build-tools/35.0.0').mkdir(parents=True)
        (root / 'sdk/build-tools/35.0.0/apksigner').write_text('')
        env = {'ANDROID_HOME': str(root / 'sdk'), 'POS_VERSION': '1.0.1', 'POS_BUILD_NUMBER': '1', 'POS_SOURCE_SHA': 'a' * 40,
               'GITHUB_ENV': str(self.base / 'env'), 'RELEASE_PLATFORM': ''}
        for command in (build.android, build.terminal_android):
            with self.subTest(command=command.__name__), patch.dict(os.environ, env), \
                    patch.object(build, 'ROOT', root), patch.object(build, 'module'), \
                    patch.object(build, 'run', side_effect=[signature, b"package: name='com.cloudlinkpos.terminal'\n"]), \
                    patch.object(build, 'run_gradle', side_effect=AssertionError('签名与版本核验不得重试')), \
                    patch.object(build, 'flutter') as flutter:
                command()
                self.assertEqual(flutter.call_args.args[0:2], ('build', 'apk'))
                self.assertIs(flutter.call_args.kwargs['gradle'], True)
        with patch.object(build, 'run_gradle') as retry, patch.object(build, 'run') as plain:
            build.flutter('pub', 'get', '--enforce-lockfile')
            build.flutter('build', 'apk', gradle=True)
        self.assertEqual(plain.call_count, 1)
        self.assertEqual(retry.call_count, 1)

    def test_api22_workspace_reset_removes_owned_signing_copy_and_workspace(self):
        workspace = self.runner / build.TERMINAL_BUILD_WORKSPACE
        (workspace / 'apps/pos/android').mkdir(parents=True)
        with patch.dict(os.environ, self.env), patch.object(build, 'cleanup_terminal_workspace') as cleanup:
            build.reset_terminal_build_workspace()
        cleanup.assert_called_once_with()
        self.assertFalse(workspace.exists())
        with patch.dict(os.environ, self.env), patch.object(build, 'cleanup_terminal_workspace', side_effect=ValueError('拒绝清理')), \
                self.assertRaises(ValueError):
            build.reset_terminal_build_workspace()


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


class AndroidUpgradeTests(unittest.TestCase):
    def test_new_source_embeds_runtime_trust_and_packages_verified_apk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            helper = root / 'dev/packaging/build_android_upgrade.py'
            helper.parent.mkdir(parents=True)
            helper.touch()
            tools = root / 'sdk/build-tools/36.0.0'
            tools.mkdir(parents=True)
            (tools / 'apksigner').touch()
            trust = {'POS_UPGRADE_PUBLIC_KEY': 'synthetic-public', 'POS_UPGRADE_PERMIT_PUBLIC_KEY': 'synthetic-permit',
                     'POS_UPGRADE_SIGNING_IDENTITY': build.ANDROID_SIGNER_CERT_SHA256, 'POS_UPGRADE_FLAVOR': 'production'}
            upgrade = Mock()
            upgrade.trust_defines.return_value = trust
            verifier = Mock()
            signature = ('Number of signers: 1\nSigner #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256 + '\n').encode()
            env = {'POS_VERSION': '1.0.2000', 'POS_BUILD_NUMBER': '2000', 'POS_SOURCE_SHA': 'a' * 40,
                   'ANDROID_HOME': str(root / 'sdk'), 'GITHUB_ENV': str(root / 'env')}
            with patch.object(build, 'ROOT', root), patch.dict(os.environ, env), \
                    patch.object(build, 'module', side_effect=[upgrade, verifier]), \
                    patch.object(build, 'run', side_effect=[signature, b"sdkVersion:'24'\n"]), \
                    patch.object(build, 'flutter') as flutter:
                build.android()
                for key, value in trust.items():
                    self.assertIn('--dart-define=' + key + '=' + value, flutter.call_args.args)
                    self.assertEqual(os.environ['ORG_GRADLE_PROJECT_' + key], value)
                upgrade.package.assert_called_once()
                self.assertEqual(build.SUBSTAGE, 'android.upgrade_manifest')


class WorkflowTests(unittest.TestCase):
    def test_public_pos_workflow_has_no_source_artifacts_or_write_source_token(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/client-release.yml').read_text())
        self.assertFalse(workflow['permissions'])
        self.assertEqual(workflow['env']['PYTHONUTF8'], '1')
        self.assertEqual(workflow['env']['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(workflow['defaults']['run']['shell'], 'bash')
        # 并发组放在两个入口工作流，各产品独立排队。
        self.assertNotIn('concurrency', workflow)
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
        steps = {step.get('name'): step for step in jobs['build']['steps']}
        android_like = "matrix.platform == 'android' || matrix.platform == 'terminal-android'"
        self.assertEqual(steps['准备固定安卓正式签名']['if'], android_like)
        self.assertEqual(steps['准备安卓固定工具链']['if'],
                         "matrix.platform == 'android' || (matrix.platform == 'terminal-android' && steps.toolchain.outputs.mode == 'default')")
        self.assertEqual(steps['清理安卓签名临时文件']['if'], 'always() && (' + android_like + ')')
        terminal = steps['静默构建并核验手持刷卡终端安装包']
        self.assertEqual(terminal['if'], "matrix.platform == 'terminal-android'")
        self.assertEqual(terminal['run'], 'python3 automation/ci/pos-build.py terminal-android')
        # 手持升级使用与横屏相同的信任公钥与发布私钥来源。
        self.assertEqual(terminal['env'], {
            'POS_UPGRADE_PUBLIC_KEY': '${{ vars.POS_UPGRADE_PUBLIC_KEY }}',
            'POS_UPGRADE_PERMIT_PUBLIC_KEY': '${{ vars.POS_UPGRADE_PERMIT_PUBLIC_KEY }}',
            'POS_RELEASE_SIGNING_KEY': '${{ secrets.POS_RELEASE_SIGNING_KEY }}',
        })
        self.assertEqual(steps['准备安卓签名清单 Go 工具链']['if'], android_like)
        self.assertEqual(steps['静默构建并核验固定正式签名安卓安装包']['if'], "matrix.platform == 'android'")
        main = yaml.safe_load((root / '.github/workflows/private-main.yml').read_text())
        self.assertNotIn('pos', main['jobs'])
        self.assertEqual(main['jobs']['build']['uses'], './.github/workflows/full-version-build.yml')
        # 补建入口：默认按变更识别，可显式指定逗号分隔的服务名。
        services = main[True]['workflow_dispatch']['inputs']['services'] if True in main else main['on']['workflow_dispatch']['inputs']['services']
        self.assertEqual(services['default'], 'changed')
        self.assertIs(services['required'], False)
        self.assertEqual(main['jobs']['build']['with']['services'], "${{ inputs.services || 'changed' }}")


class GradleCacheWorkflowTests(unittest.TestCase):
    def test_android_jobs_cache_only_relocatable_gradle_dependencies(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        steps = yaml.safe_load((root / '.github/workflows/client-release.yml').read_text())['jobs']['build']['steps']
        names = [step.get('name') for step in steps]
        cache = steps[names.index('恢复 Gradle 依赖缓存')]
        self.assertEqual(cache['if'], "matrix.platform == 'android' || matrix.platform == 'terminal-android'")
        self.assertEqual(cache['uses'], 'actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830')
        # 缓存在读取源码与判定工具链之后、任何 Gradle 构建之前恢复。
        self.assertLess(names.index('静默读取已合并固定源码'), names.index('恢复 Gradle 依赖缓存'))
        self.assertLess(names.index('判定手持安卓工具链'), names.index('恢复 Gradle 依赖缓存'))
        self.assertLess(names.index('恢复 Gradle 依赖缓存'), names.index('静默运行打包边界与软件版本测试'))
        self.assertEqual(set(cache['with']), {'path', 'key', 'restore-keys'})
        paths = cache['with']['path'].split()
        # actions/cache 对目录整体递归归档、不支持按文件名排除，所以不写无效的 ! 规则。
        self.assertEqual(paths, ['~/.gradle/caches/modules-2/files-2.1', '~/.gradle/caches/modules-2/metadata-*',
                                 '~/.gradle/wrapper/dists'])
        key, restore = cache['with']['key'], cache['with']['restore-keys'].strip()
        for part in ['matrix.platform', 'steps.toolchain.outputs.mode', "'source/apps/pos/android/gradle/wrapper/gradle-wrapper.properties'",
                     "'source/apps/pos/android/**/*.gradle.kts'", "'source/apps/pos/pubspec.lock'"]:
            self.assertIn(part, key)
        # 回退只允许同一 Gradle 配置哈希：镜像顺序变化后不复用旧解析结果。
        self.assertTrue(key.startswith(restore))
        self.assertNotIn('pubspec.lock', restore)
        self.assertIn('gradle-wrapper.properties', restore)

    def test_locks_and_partial_downloads_are_deleted_before_cache_is_saved(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        steps = yaml.safe_load((root / '.github/workflows/client-release.yml').read_text())['jobs']['build']['steps']
        names = [step.get('name') for step in steps]
        clean = steps[names.index('清理 Gradle 缓存中的锁与未完成下载')]
        self.assertEqual(clean['if'], "success() && (matrix.platform == 'android' || matrix.platform == 'terminal-android')")
        self.assertNotIn('${{', clean['run'])
        self.assertNotIn('env', clean)
        # 缓存在作业末尾保存：清理位于全部 Gradle 构建与发布之后，失败回写步骤之前。
        for build_step in ['静默构建并核验固定正式签名安卓安装包', '静默构建并核验手持刷卡终端安装包', '核验并发布公开永久下载附件']:
            self.assertLess(names.index(build_step), names.index('清理 Gradle 缓存中的锁与未完成下载'))
        self.assertLess(names.index('清理 Gradle 缓存中的锁与未完成下载'), names.index('检查私有失败日志'))
        # 用夹具实跑该脚本，证明待归档目录里只剩完整文件。
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            gradle = home / '.gradle'
            kept = [gradle / 'caches/modules-2/files-2.1/org/x/1.0/abc/x-1.0.pom',
                    gradle / 'caches/modules-2/metadata-2.107/descriptors/x/descriptor.bin',
                    gradle / 'wrapper/dists/gradle-8.12-all/hash/gradle-8.12-all.zip',
                    gradle / 'wrapper/dists/gradle-8.12-all/hash/gradle-8.12-all.zip.ok']
            removed = [gradle / 'caches/modules-2/metadata-2.107/sample.lock',
                       gradle / 'caches/modules-2/files-2.1/org/x/1.0/abc/x-1.0.jar.part',
                       gradle / 'wrapper/dists/gradle-8.12-all/hash/gradle.zip.part',
                       gradle / 'wrapper/dists/gradle-8.12-all/hash/gradle.zip.lck']
            outside = gradle / 'caches/modules-2/modules-2.lock'
            for path in kept + removed + [outside]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('x')
            result = subprocess.run(['bash', '-c', clean['run']], env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([path.exists() for path in kept], [True] * len(kept))
            self.assertEqual([path.exists() for path in removed], [False] * len(removed))
            # 未缓存的目录不受影响。
            self.assertTrue(outside.exists())
            empty = home / 'empty'
            empty.mkdir()
            result = subprocess.run(['bash', '-c', clean['run']], env=dict(os.environ, HOME=str(empty)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)



TOOLCHAIN = {
    'schema': 'cloudlinkpos.terminal-android-toolchain/v1',
    'flutter': {'version': '3.32.8', 'frameworkRevision': 'edada7c56edf4a183c1735310e123c7f923584f1',
                'engineRevision': 'ef0cd000916d64fa0c5d09cc809fa7ad244a5767', 'dartSdkVersion': '3.8.1'},
    'android': {'packageName': 'com.cloudlinkpos.terminal', 'minSdk': 22, 'requiredAbi': 'armeabi-v7a', 'ndkVersion': '26.3.11579264'},
    'entry': 'lib/main_terminal.dart',
}
MACHINE = ('Downloading synthetic\n' + json.dumps({'frameworkRevision': TOOLCHAIN['flutter']['frameworkRevision'],
           'engineRevision': TOOLCHAIN['flutter']['engineRevision'], 'dartSdkVersion': '3.8.1 (stable)'})).encode()
# 与私库 #492 build_terminal_android.py 同名契约函数的最小替身：公开 CI 测试不读取私库源码。
FAKE_BUILDER = r"""
import json, shutil
from pathlib import Path
CALLS = []
def load_toolchain(path=None):
    path = path or Path(__file__).resolve().parent / 'terminal-android/toolchain.json'
    return json.loads(Path(path).read_text(encoding='utf-8'))
def check_flutter(machine_json, toolchain):
    CALLS.append(('check_flutter', json.loads(machine_json)['frameworkRevision']))
def prepare_workspace(source, workspace, key_properties=None):
    app = Path(workspace) / 'apps/pos'
    if app.exists():
        raise ValueError('exists')
    (app / 'android').mkdir(parents=True)
    if key_properties:
        shutil.copyfile(key_properties, app / 'android/key.properties')
    CALLS.append(('prepare_workspace', str(source), str(workspace), str(key_properties) if key_properties else None))
    return app
"""


class TerminalApi22Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'source'
        self.runner = self.base / 'runner'
        self.runner.mkdir()
        (self.root / 'dev/packaging/terminal-android').mkdir(parents=True)
        self.write_toolchain(TOOLCHAIN)
        (self.root / build.TERMINAL_BUILDER).write_text(FAKE_BUILDER, encoding='utf-8')
        tools = self.base / 'sdk/build-tools/35.0.0'
        tools.mkdir(parents=True)
        (tools / 'apksigner').touch()
        self.tools = tools
        self.env = {'RELEASE_PLATFORM': 'terminal-android', 'RUNNER_TEMP': str(self.runner),
                    'POS_VERSION': '1.0.2185', 'POS_BUILD_NUMBER': '2185', 'POS_SOURCE_SHA': 'a' * 40,
                    'ANDROID_HOME': str(self.base / 'sdk'), 'GITHUB_ENV': str(self.base / 'env'),
                    'GITHUB_PATH': str(self.base / 'path'), 'GITHUB_OUTPUT': str(self.base / 'out'),
                    'POS_FLUTTER_BIN': str(self.runner / 'cloudlinkpos-terminal-flutter/flutter/bin/flutter'),
                    'POS_TERMINAL_NDK': str(self.base / 'sdk/ndk/26.3.11579264'),
                    'GITHUB_REPOSITORY': 'sdbcbt/CloudLinkPOS-CI', 'GITHUB_REF': 'refs/heads/main'}
        patcher = patch.object(build, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_toolchain(self, value):
        (self.root / build.TERMINAL_TOOLCHAIN).write_text(json.dumps(value), encoding='utf-8')

    def mutated(self, section, key, value):
        changed = json.loads(json.dumps(TOOLCHAIN))
        if section:
            changed[section][key] = value
        else:
            changed[key] = value
        return changed

    def test_only_terminal_matrix_with_full_declaration_selects_api22(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(build.terminal_toolchain()['sha256'], build.TERMINAL_FLUTTER['3.32.8']['sha256'])
            for platform in ['android', 'windows', '']:
                with self.subTest(platform=platform), patch.dict(os.environ, {'RELEASE_PLATFORM': platform}):
                    self.assertIsNone(build.terminal_toolchain())
            (self.root / build.TERMINAL_BUILDER).unlink()
            with self.assertRaisesRegex(ValueError, '不完整'):
                build.terminal_toolchain()
            (self.root / build.TERMINAL_TOOLCHAIN).unlink()
            # 旧固定源码：两个声明都没有，继续走 3.44。
            self.assertIsNone(build.terminal_toolchain())
            build.main('toolchain')
            self.assertEqual((self.base / 'out').read_text(), 'mode=default\n')

    def test_toolchain_mode_output_never_contains_private_manifest(self):
        value = dict(TOOLCHAIN, description='synthetic private description')
        self.write_toolchain(value)
        with patch.dict(os.environ, self.env):
            build.main('toolchain')
        self.assertEqual((self.base / 'out').read_text(), 'mode=api22\n')

    def test_manifest_cannot_choose_unpinned_flutter_or_other_targets(self):
        cases = [self.mutated('flutter', 'version', '3.44.0'), self.mutated('flutter', 'frameworkRevision', 'f' * 40),
                 self.mutated('flutter', 'engineRevision', 'f' * 40), self.mutated('flutter', 'dartSdkVersion', '3.12.0'),
                 self.mutated('android', 'minSdk', 24), self.mutated('android', 'requiredAbi', 'arm64-v8a'),
                 self.mutated('android', 'packageName', 'com.cloudlinkpos.pos'), self.mutated('android', 'ndkVersion', '27.0.12077973'),
                 self.mutated(None, 'entry', 'lib/main.dart')]
        for value in cases:
            with self.subTest(value=value), patch.dict(os.environ, self.env):
                self.write_toolchain(value)
                with self.assertRaises(ValueError):
                    build.terminal_toolchain()
                self.assertEqual(build.SUBSTAGE, 'toolchain.declaration')

    def install(self, content, machine=MACHINE, revision='26.3.11579264'):
        def fake_run(args, cwd=None, env=None):
            if args[0] == 'curl':
                Path(args[args.index('-o') + 1]).write_bytes(content)
            elif args[0].endswith('sdkmanager'):
                ndk = self.base / 'sdk/ndk' / args[-1].split(';')[1]
                ndk.mkdir(parents=True, exist_ok=True)
                (ndk / 'source.properties').write_text('Pkg.Desc = Android NDK\nPkg.Revision = ' + revision + '\n')
            elif args[1:2] == ['--version']:
                return machine
            return b''
        with patch.dict(os.environ, self.env), patch.object(build, 'run', side_effect=fake_run) as invoke:
            build.main('terminal-toolchain')
        return invoke

    def test_installs_official_archive_by_public_digest_into_isolated_directory(self):
        content = b'synthetic flutter archive'
        pinned = dict(build.TERMINAL_FLUTTER['3.32.8'], sha256=build.hashlib.sha256(content).hexdigest())
        with patch.dict(build.TERMINAL_FLUTTER, {'3.32.8': pinned}):
            invoke = self.install(content)
        calls = [call.args[0] for call in invoke.call_args_list]
        self.assertEqual(calls[0][-3], 'https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.32.8-stable.tar.xz')
        self.assertEqual(calls[1][:2], ['tar', '-xf'])
        self.assertEqual(calls[2][0], str(self.runner / 'cloudlinkpos-terminal-flutter/flutter/bin/flutter'))
        self.assertEqual(calls[3][-2:], ['--install', 'ndk;26.3.11579264'])
        written = (self.base / 'env').read_text()
        self.assertIn('POS_FLUTTER_BIN=' + str(self.runner / 'cloudlinkpos-terminal-flutter/flutter/bin/flutter'), written)
        self.assertIn('POS_TERMINAL_NDK=' + str(self.base / 'sdk/ndk/26.3.11579264'), written)
        self.assertEqual((self.base / 'path').read_text(), str(self.runner / 'cloudlinkpos-terminal-flutter/flutter/bin') + '\n')
        self.assertFalse((self.runner / 'cloudlinkpos-terminal-flutter.tar.xz').exists())

    def test_official_digest_revision_or_ndk_mismatch_stops(self):
        # 真实固定摘要 + 任意伪造归档：摘要不符即停，不解压。
        with self.assertRaisesRegex(ValueError, '摘要不匹配'):
            self.install(b'tampered archive')
        self.assertEqual(build.SUBSTAGE, 'toolchain.flutter_digest')
        self.assertFalse((self.base / 'env').exists())
        content = b'synthetic'
        pinned = dict(build.TERMINAL_FLUTTER['3.32.8'], sha256=build.hashlib.sha256(content).hexdigest())
        for machine, revision, stage in [(MACHINE.replace(b'3.8.1', b'3.12.0'), '26.3.11579264', 'toolchain.flutter_revision'),
                                         (MACHINE, '27.0.12077973', 'toolchain.ndk')]:
            with self.subTest(stage=stage), patch.dict(build.TERMINAL_FLUTTER, {'3.32.8': pinned}):
                for path in self.runner.iterdir():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                with self.assertRaises(ValueError):
                    self.install(content, machine, revision)
                self.assertEqual(build.SUBSTAGE, stage)
                self.assertFalse((self.base / 'env').exists())

    def evidence(self, apk, **override):
        value = {'schema': 'cloudlinkpos.terminal-android-evidence/v1', 'apkSHA256': build.file_sha256(apk),
                 'packageName': 'com.cloudlinkpos.terminal', 'versionName': '1.0.2185', 'versionCode': '2185', 'minSdk': 22,
                 'nativeCode': ['armeabi-v7a'], 'signerCertSHA256': build.ANDROID_SIGNER_CERT_SHA256, 'v1Signature': True,
                 'nativeLibs': ['lib/armeabi-v7a/libapp.so', 'lib/armeabi-v7a/libflutter.so'], 'flutter': TOOLCHAIN['flutter'],
                 'symbolCheckedLibs': ['libapp.so', 'libflutter.so']}
        value.update(override)
        return value

    def build_terminal(self, override=None, key=True, embedded=b'a' * 40, env=None):
        if key:
            properties = self.root / 'apps/pos/android/key.properties'
            properties.parent.mkdir(parents=True, exist_ok=True)
            properties.write_text('storeFile=/runner/cloudlinkpos-release.keystore\n')
        signature = ('Number of signers: 1\nSigner #1 certificate SHA-256 digest: ' + build.ANDROID_SIGNER_CERT_SHA256 + '\n').encode()
        badging = b"package: name='com.cloudlinkpos.terminal' versionCode='2185' versionName='1.0.2185'\nminSdkVersion:'22'\n"
        loaded = {}
        def fake_module(name, path):
            if name == 'build_terminal_android':
                loaded.setdefault('builder', build.module.__wrapped__(name, path) if hasattr(build.module, '__wrapped__') else real_module(name, path))
                return loaded['builder']
            loaded['verifier'] = Mock()
            return loaded['verifier']
        def fake_run(args, cwd=None, env=None):
            if args[1:2] == ['--version']:
                return MACHINE
            if args[2:3] == ['build']:
                output = Path(args[args.index('--workspace') + 1]) / 'apps/pos/build/app/outputs/flutter-apk'
                output.mkdir(parents=True)
                with zipfile.ZipFile(output / 'app-release.apk', 'w') as archive:
                    archive.writestr('lib/armeabi-v7a/libapp.so', b'\x00snapshot' + embedded + b'\x00')
                    archive.writestr('lib/armeabi-v7a/libflutter.so', b'engine')
            if 'apksigner' in args[0]:
                return signature
            if 'verify' in args[2:3]:
                apk = Path(args[args.index('--apk') + 1])
                Path(args[args.index('--evidence') + 1]).write_text(json.dumps(self.evidence(apk, **(override or {}))))
            if args[0].endswith('aapt'):
                return badging
            return b''
        with patch.dict(os.environ, dict(self.env, **(env or {}))), patch.object(build, 'module', side_effect=fake_module), \
                patch.object(build, 'run', side_effect=fake_run) as invoke:
            try:
                build.terminal_android()
            finally:
                os.environ.pop('ORG_GRADLE_PROJECT_CLP_APP_VARIANT', None)
        return invoke, loaded

    def test_api22_build_passes_plan_version_source_identity_and_formal_keystore(self):
        invoke, loaded = self.build_terminal()
        calls = [call.args[0] for call in invoke.call_args_list]
        app = self.runner / 'cloudlinkpos-terminal-build/apps/pos'
        flutter_bin = self.env['POS_FLUTTER_BIN']
        self.assertEqual(calls[0], [flutter_bin, '--version', '--machine', '--suppress-analytics'])
        # 调用私库脚本 build：本轮版本、完整私库源码 SHA 与既有正式签名 key.properties。
        self.assertEqual(calls[1], [os.sys.executable, str(self.root / build.TERMINAL_BUILDER), 'build',
                                    '--workspace', str(self.runner / 'cloudlinkpos-terminal-build'), '--flutter', flutter_bin,
                                    '--key-properties', str(self.root / 'apps/pos/android/key.properties'),
                                    '--build-name', '1.0.2185', '--build-number', '2185', '--source-sha', 'a' * 40])
        self.assertIsNone(invoke.call_args_list[1].kwargs.get('env'))
        self.assertFalse([key for key in os.environ if key.startswith('ORG_GRADLE_PROJECT_')])
        self.assertIn('apksigner', calls[2][0])
        verify = calls[3]
        self.assertEqual(verify[1:3], [str(self.root / build.TERMINAL_BUILDER), 'verify'])
        self.assertEqual(verify[verify.index('--ndk') + 1], self.env['POS_TERMINAL_NDK'])
        self.assertEqual(verify[verify.index('--apk') + 1], str(app / 'build/app/outputs/flutter-apk/app-release.apk'))
        self.assertTrue(calls[4][0].endswith('aapt'))
        loaded['verifier'].verify.assert_called_once_with(
            "package: name='com.cloudlinkpos.terminal' versionCode='2185' versionName='1.0.2185'\nminSdkVersion:'22'\n",
            '1.0.2185', 2185, 'com.cloudlinkpos.terminal')
        written = (self.base / 'env').read_text()
        self.assertIn('POS_ANDROID_SIGNER_CERT_SHA256=' + build.ANDROID_SIGNER_CERT_SHA256, written)
        self.assertIn('POS_ANDROID_PACKAGE_NAME=com.cloudlinkpos.terminal', written)

    def test_api22_terminal_upgrade_assets_when_source_declares_product(self):
        (self.root / 'dev/packaging/build_android_upgrade.py').write_text(
            "PRODUCTS = {'pos': (), 'terminal': ()}\n", encoding='utf-8')
        helper = Mock(PRODUCTS={'pos': (), 'terminal': ()})
        original = build.module

        def module(name, path):
            return helper if name == 'build_android_upgrade' else original(name, path)

        with patch.object(build, 'module', side_effect=module):
            self.assertIs(build.terminal_upgrade_helper(), helper)
        helper.PRODUCTS = {'pos': ()}
        with patch.object(build, 'module', side_effect=module):
            self.assertIsNone(build.terminal_upgrade_helper())
        (self.root / 'dev/packaging/build_android_upgrade.py').unlink()
        self.assertIsNone(build.terminal_upgrade_helper())

    def test_api22_build_requests_trust_and_packages_terminal_upgrade(self):
        helper = Mock()
        with patch.object(build, 'terminal_upgrade_helper', return_value=helper):
            invoke, _ = self.build_terminal()
        calls = [call.args[0] for call in invoke.call_args_list]
        self.assertEqual(calls[1][-1], '--upgrade-trust')
        helper.trust_defines.assert_called_once()
        package = helper.package.call_args
        self.assertEqual(package.kwargs['product'], 'terminal')
        self.assertEqual(package.args[0], self.runner / 'cloudlinkpos-terminal-build/apps/pos/build/app/outputs/flutter-apk/app-release.apk')
        self.assertEqual(build.SUBSTAGE, 'android.upgrade_manifest')

    def test_api22_apk_must_embed_this_round_source_sha(self):
        with self.assertRaisesRegex(ValueError, '源码身份'):
            self.build_terminal(embedded=b'b' * 40)
        self.assertEqual(build.SUBSTAGE, 'android.api22')
        self.assertFalse((self.base / 'env').exists())

    def test_api22_source_identity_must_be_the_fixed_full_sha(self):
        for env in [{'POS_SOURCE_SHA': 'a' * 12}, {'POS_SOURCE_SHA': 'A' * 40}, {'SOURCE_SHA': 'b' * 40}]:
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, '源码身份'):
                invoke, _ = self.build_terminal(env=env)
            self.assertFalse((self.base / 'env').exists())

    def test_api22_evidence_must_match_plan_certificate_and_target(self):
        cases = [{'minSdk': 24}, {'minSdk': '22'}, {'v1Signature': False}, {'signerCertSHA256': '0' * 64},
                 {'versionName': '1.0.1'}, {'versionCode': '1'}, {'packageName': 'com.cloudlinkpos.pos'},
                 {'nativeLibs': ['lib/arm64-v8a/libapp.so', 'lib/arm64-v8a/libflutter.so']}, {'apkSHA256': '0' * 64},
                 {'symbolCheckedLibs': []}, {'schema': 'other'}]
        for override in cases:
            with self.subTest(override=override):
                shutil.rmtree(self.runner)
                self.runner.mkdir()
                (self.base / 'env').unlink(missing_ok=True)
                with self.assertRaisesRegex(ValueError, '核验证据'):
                    self.build_terminal(override)
                self.assertEqual(build.SUBSTAGE, 'android.api22')
                self.assertFalse((self.base / 'env').exists())

    def test_api22_requires_existing_formal_keystore_before_compiling(self):
        with self.assertRaisesRegex(ValueError, '正式签名'):
            invoke, _ = self.build_terminal(key=False)
        self.assertEqual(build.SUBSTAGE, 'android.compile')

    def test_api22_tests_run_in_isolated_workspace(self):
        with patch.dict(os.environ, self.env), patch.object(build, 'run') as invoke:
            build.main('test')
        app = self.runner / 'cloudlinkpos-terminal-test/apps/pos'
        flutter_bin = self.env['POS_FLUTTER_BIN']
        self.assertEqual([call.args[:2] for call in invoke.call_args_list[1:]], [
            ([flutter_bin, '--suppress-analytics', 'pub', 'get', '--enforce-lockfile'], app),
            ([flutter_bin, '--suppress-analytics', 'test', '--no-pub', 'test/terminal', 'test/services/device_information_service_test.dart'], app)])
        self.assertFalse((app / 'android/key.properties').exists())

    def test_api22_package_reads_isolated_workspace_output(self):
        with patch.dict(os.environ, self.env), patch.object(build, 'run') as invoke:
            build.main('package')
        self.assertEqual(invoke.call_args.args[0][1:], ['dev/packaging/pos_release.py', 'package', '--platform', 'terminal-android', '--input',
                                                       str(self.runner / 'cloudlinkpos-terminal-build/apps/pos/build/app/outputs/flutter-apk')])

    def signing_copy(self, content, recorded):
        copy = self.runner / 'cloudlinkpos-terminal-build/apps/pos/android/key.properties'
        copy.parent.mkdir(parents=True)
        copy.write_bytes(content)
        marker = {str(self.root / 'apps/pos/android/key.properties'): build.hashlib.sha256(recorded).hexdigest(),
                  str(self.runner / 'cloudlinkpos-release.keystore'): '0' * 64}
        (self.runner / build.SIGNING_MARKER).write_text(json.dumps(marker))
        return copy

    def test_cleanup_removes_owned_workspace_signing_copy_then_existing_cleanup(self):
        copy = self.signing_copy(b'storeFile=x\n', b'storeFile=x\n')
        with patch.dict(os.environ, self.env), patch.object(build, 'run') as invoke:
            build.main('cleanup')
        self.assertFalse(copy.exists())
        self.assertEqual(invoke.call_args.args[0][1:], ['dev/packaging/prepare_android_signing.py', '--cleanup'])

    def test_cleanup_refuses_changed_copy_but_still_runs_existing_cleanup(self):
        copy = self.signing_copy(b'storeFile=changed\n', b'storeFile=x\n')
        with patch.dict(os.environ, self.env), patch.object(build, 'run') as invoke, self.assertRaisesRegex(ValueError, '拒绝清理'):
            build.main('cleanup')
        self.assertTrue(copy.exists())
        invoke.assert_called_once()

    def test_api22_private_failure_output_stays_private(self):
        script = Path(__file__).with_name('pos-build.py').resolve()
        code = """
import runpy, subprocess, sys
from unittest.mock import patch
failure = subprocess.CompletedProcess(['private'], 3, stdout=b'private source text', stderr=b'secret=never-public')
sys.argv = [sys.argv[1], 'terminal-toolchain']
with patch.object(subprocess, 'run', return_value=failure):
    runpy.run_path(sys.argv[0], run_name='__main__')
"""
        work = self.base / 'work'
        (work / 'source').parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.root, work / 'source')
        env = dict(os.environ, **self.env, PYTHONIOENCODING='utf-8')
        env.pop('SOURCE_TOKEN', None)
        result = subprocess.run([os.sys.executable, '-c', code, str(script)], cwd=work, env=env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'private', result.stdout + result.stderr)
        self.assertNotIn(b'never-public', result.stdout + result.stderr)
        self.assertEqual(result.stderr.decode('utf-8').splitlines(), [
            '::error::收银客户端构建停止于：terminal-toolchain；子阶段：toolchain.flutter_download；exit_code=3；category=unknown；请在私库受控环境复现。'])
        self.assertIn(b'private source text', (self.runner / 'cloudlinkpos-pos-first-failure.log').read_bytes())


class Api22WorkflowTests(unittest.TestCase):
    def test_isolated_toolchain_only_for_terminal_and_default_paths_unchanged(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/client-release.yml').read_text())
        steps = workflow['jobs']['build']['steps']
        names = [step.get('name') for step in steps]
        decide = steps[names.index('判定手持安卓工具链')]
        self.assertEqual(decide['if'], "matrix.platform == 'terminal-android'")
        self.assertEqual(decide['id'], 'toolchain')
        self.assertEqual(decide['run'], 'python3 automation/ci/pos-build.py toolchain')
        self.assertNotIn('env', decide)
        isolated = steps[names.index('准备手持 API 22 隔离工具链')]
        self.assertEqual(isolated['if'], "matrix.platform == 'terminal-android' && steps.toolchain.outputs.mode == 'api22'")
        self.assertIn('python3 automation/ci/pos-build.py terminal-toolchain', isolated['run'])
        self.assertNotIn('env', isolated)
        self.assertNotIn('secrets.', isolated['run'])
        default = steps[names.index('准备安卓固定工具链')]
        # 横屏收银 Android 与未声明新工具链的旧手持源码继续使用固定 3.44 摘要。
        self.assertIn('flutter_linux_3.44.0-stable.tar.xz', default['run'])
        self.assertIn('e1ec95e6c550458a34de93580cb85dac24da0e9bedb9bb42811f050ac5a0c7d5', default['run'])
        windows = steps[names.index('下载固定 Windows Flutter SDK')]
        self.assertEqual(windows['if'], "matrix.platform == 'windows'")
        self.assertIn('flutter_windows_3.44.0-stable.zip', windows['run'])
        # 顺序：签名 → 判定 → 工具链 → 测试 → 构建 → 打包 → 发布 → 清理。
        order = ['准备固定安卓正式签名', '判定手持安卓工具链', '准备安卓固定工具链', '准备手持 API 22 隔离工具链',
                 '静默运行打包边界与软件版本测试', '静默构建并核验手持刷卡终端安装包', '生成源码版本清单和 SHA-256',
                 '核验并发布公开永久下载附件', '清理安卓签名临时文件']
        self.assertEqual([name for name in names if name in order], order)
        # 两个入口工作流仍各自独立，不引用 API 22 步骤。
        for name in ['build-pos-release.yml', 'build-terminal-release.yml']:
            self.assertNotIn('terminal-toolchain', (root / '.github/workflows' / name).read_text())

    def test_public_pins_match_official_release_metadata(self):
        pinned = build.TERMINAL_FLUTTER
        self.assertEqual(list(pinned), ['3.32.8'])
        self.assertEqual(pinned['3.32.8']['sha256'], 'c2c7d75f9be53f10f3fdba2f7e042b2ef0852cc088392b374bcec34c6204ed8c')
        self.assertTrue(pinned['3.32.8']['archive'].startswith('https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/'))


if __name__ == '__main__':
    unittest.main()
