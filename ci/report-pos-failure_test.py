"""核验私库失败详情的固定目标、脱敏、权限及公开日志边界。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('report_pos_failure', Path(__file__).with_name('report-pos-failure.py'))
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


class FailureReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sha = 'a' * 40
        self.env = {
            'GITHUB_REPOSITORY': 'sdbcbt/CloudLinkPOS-CI', 'GITHUB_REF': 'refs/heads/main',
            'SOURCE_SHA': self.sha, 'RELEASE_PLATFORM': 'android', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
            'POS_RELEASE_PLAN': json.dumps({'repository': 'sdbcbt/CloudLinkPOS', 'sourceSHA': self.sha}),
            'RUNNER_TEMP': str(self.root), 'CHECKS_TOKEN': 'synthetic-checks-token', 'SOURCE_TOKEN': 'synthetic-read-token',
            'GITHUB_TOKEN': 'synthetic-release-token', 'ANDROID_KEYSTORE_BASE64': 'synthetic-keystore-base64',
            'ANDROID_KEYSTORE_PASSWORD': 'synthetic-password-long', 'ANDROID_KEY_PASSWORD': 'synthetic-password',
            'ANDROID_KEY_ALIAS': 'synthetic-alias', 'PYTHONIOENCODING': 'utf-8',
        }

    def write_log(self, value):
        path = self.root / reporter.LOG_NAME
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
        return path

    def test_report_posts_only_to_fixed_private_sha_and_redacts_all_known_credentials(self):
        content = '\n'.join(self.env[key] for key in reporter.SECRET_KEYS)
        self.write_log((content + '\n```\n![private](https://invalid.example/image)\nprivate compiler output').encode())
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(status=201))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        git = subprocess.CompletedProcess([], 0, stdout=(self.sha + '\n').encode(), stderr=b'private git output')
        for platform, expected_name in [('android', 'pos-android-build-failure'), ('windows', 'pos-windows-build-failure')]:
            with self.subTest(platform=platform), patch.dict(os.environ, dict(self.env, RELEASE_PLATFORM=platform)), \
                    patch.object(reporter.subprocess, 'run', return_value=git), \
                    patch.object(reporter.urllib.request, 'build_opener', return_value=opener) as make_opener:
                reporter.report()
                self.assertIsInstance(make_opener.call_args.args[0], reporter.NoRedirect)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, reporter.API_URL)
                self.assertEqual(request.get_method(), 'POST')
                self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.env['CHECKS_TOKEN'])
                body = json.loads(request.data)
                self.assertEqual(body['head_sha'], self.sha)
                self.assertEqual(body['name'], expected_name)
                self.assertEqual(body['output']['title'], reporter.CHECKS[platform][1])
                self.assertEqual(body['details_url'], 'https://github.com/sdbcbt/CloudLinkPOS-CI/actions/runs/123')
                self.assertEqual(body['conclusion'], 'failure')
                text = body['output']['text']
                for key in reporter.SECRET_KEYS:
                    self.assertNotIn(self.env[key], text)
                self.assertEqual(text.count('```'), 2)
                self.assertLessEqual(len(text.encode('utf-8')), reporter.MAX_TEXT_BYTES)

    def test_redaction_precedes_utf8_tail_bound_even_when_secret_crosses_the_cut(self):
        secret = 'synthetic-cross-boundary-' + 'z' * 80
        content = ('prefix-' * 1000 + secret + '尾' * 7980).encode('utf-8')
        with patch.dict(os.environ, dict(self.env, CHECKS_TOKEN=secret)):
            text = reporter.redact_tail(content)
        self.assertLessEqual(len(text.encode('utf-8')), 24000)
        self.assertNotIn(secret, text)
        self.assertNotIn('z' * 20, text)
        self.assertTrue(text.endswith('\n```'))

    def test_invalid_context_or_plan_never_attempts_api(self):
        cases = [
            {'SOURCE_SHA': 'invalid'}, {'SOURCE_SHA': 'b' * 40}, {'RELEASE_PLATFORM': 'linux'},
            {'GITHUB_REPOSITORY': 'other/repo'}, {'GITHUB_REF': 'refs/pull/1/merge'},
            {'GITHUB_RUN_ID': '123/redirect'}, {'GITHUB_RUN_ATTEMPT': '0'},
            {'POS_RELEASE_PLAN': json.dumps({'repository': 'other/repo', 'sourceSHA': self.sha})},
        ]
        for changes in cases:
            with self.subTest(changes=changes), patch.dict(os.environ, dict(self.env, **changes)), \
                    patch.object(reporter.urllib.request, 'build_opener') as api, self.assertRaises(ValueError):
                reporter.report()
            api.assert_not_called()

    def test_changed_checkout_or_non_main_commit_never_attempts_api(self):
        good = subprocess.CompletedProcess([], 0, stdout=(self.sha + '\n').encode(), stderr=b'')
        other = subprocess.CompletedProcess([], 0, stdout=('b' * 40 + '\n').encode(), stderr=b'')
        for results in [[other], [good, other]]:
            with self.subTest(results=len(results)), patch.dict(os.environ, self.env), \
                    patch.object(reporter.subprocess, 'run', side_effect=results), \
                    patch.object(reporter.urllib.request, 'build_opener') as api, self.assertRaises(ValueError):
                reporter.report()
            api.assert_not_called()

    def test_symlinks_and_non_private_posix_files_are_rejected(self):
        path = self.write_log(b'private log')
        if os.name != 'nt':
            path.chmod(0o644)
            with patch.dict(os.environ, self.env), self.assertRaises(ValueError):
                reporter.read_log()
            path.chmod(0o600)
        target = self.root / 'other-private-file'
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest('当前 Windows 未允许创建测试符号链接')
        with patch.dict(os.environ, self.env), self.assertRaises(ValueError):
            reporter.read_log()

    def test_redirect_handler_never_forwards_authorization_or_private_body(self):
        self.assertIsNone(reporter.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://invalid.example'))

    def test_real_entry_hides_http_error_response_and_credentials(self):
        self.write_log(b'private compiler output synthetic-password-long')
        script = Path(__file__).with_name('report-pos-failure.py').resolve()
        code = """
import io, os, runpy, subprocess, sys, urllib.error, urllib.request
from unittest.mock import Mock, patch
git = subprocess.CompletedProcess([], 0, stdout=(os.environ['SOURCE_SHA'] + '\\n').encode(), stderr=b'private git')
opener = Mock()
opener.open.side_effect = urllib.error.HTTPError('https://invalid.example/private', 403,
    'secret=synthetic-checks-token', {}, io.BytesIO(b'private response synthetic-password-long'))
sys.argv = [sys.argv[1]]
with patch.object(subprocess, 'run', return_value=git), patch.object(urllib.request, 'build_opener', return_value=opener):
    runpy.run_path(sys.argv[0], run_name='__main__')
"""
        result = subprocess.run([sys.executable, '-c', code, str(script)], env=dict(os.environ, **self.env), capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr.decode('utf-8').splitlines(), [
            '::error::私库失败详情回写未完成，请核对固定提交和检查写入权限。',
        ])

    def test_real_entry_invalid_sha_does_not_call_api(self):
        script = Path(__file__).with_name('report-pos-failure.py').resolve()
        code = """
import os, runpy, sys, urllib.request
from pathlib import Path
from unittest.mock import patch
def forbidden(*args, **kwargs):
    Path(os.environ['RUNNER_TEMP'], 'unexpected-api').touch()
    raise RuntimeError('private response synthetic-checks-token')
sys.argv = [sys.argv[1]]
with patch.object(urllib.request, 'build_opener', side_effect=forbidden):
    runpy.run_path(sys.argv[0], run_name='__main__')
"""
        env = dict(os.environ, **dict(self.env, SOURCE_SHA='invalid'))
        result = subprocess.run([sys.executable, '-c', code, str(script)], env=env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / 'unexpected-api').exists())
        self.assertNotIn(b'synthetic-checks-token', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
