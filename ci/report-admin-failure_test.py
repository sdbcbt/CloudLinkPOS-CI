"""核对后台失败诊断只回写固定私库，并复用私有日志安全检查。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('admin_failure', Path(__file__).with_name('report-admin-failure.py'))
REPORT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(REPORT)
spec = importlib.util.spec_from_file_location('full_build', Path(__file__).with_name('full-build.py'))
BUILD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUILD)
SHA = 'a' * 40
ENV = {
    'GITHUB_REPOSITORY': 'sdbcbt/CloudLinkPOS-CI', 'GITHUB_REF': 'refs/heads/main',
    'SOURCE_SHA': SHA, 'BUILD_SERVICE': 'admin', 'GITHUB_RUN_ID': '12', 'GITHUB_RUN_ATTEMPT': '1',
    'CHECKS_TOKEN': 'fixture-check-token', 'ACR_PASSWORD': 'fixture-acr-password',
}


class Diagnostics(unittest.TestCase):
    def test_private_output_is_not_printed_and_first_failure_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RUNNER_TEMP': tmp}):
            output = io.StringIO()
            failed = subprocess.CompletedProcess(['pnpm'], 1, b'private-source', b'private-error')
            with patch.object(BUILD.subprocess, 'run', return_value=failed), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with self.assertRaises(RuntimeError):
                    BUILD.run(['pnpm', 'build'], private_admin=True)
                BUILD.save_admin_failure(b'later-error', b'')
            self.assertNotIn('private-source', output.getvalue())
            self.assertNotIn('private-error', output.getvalue())
            path = Path(tmp) / REPORT.PRIVATE.LOG_NAME
            self.assertEqual(path.read_bytes(), b'private-source\nprivate-error')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_report_redacts_secrets_and_targets_only_private_sha(self):
        requests = []

        class Response:
            status = 201
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, dict(ENV, RUNNER_TEMP=tmp)):
            with contextlib.redirect_stdout(io.StringIO()):
                BUILD.save_admin_failure(b'compiler fixture-check-token fixture-acr-password', b'')
            with patch.object(REPORT, 'validate_target', return_value=(SHA, '12', '1')), patch.object(REPORT.urllib.request, 'build_opener', return_value=Opener()):
                REPORT.report()
        self.assertEqual(requests[0].full_url, 'https://api.github.com/repos/sdbcbt/CloudLinkPOS/check-runs')
        body = json.loads(requests[0].data)
        self.assertEqual(body['head_sha'], SHA)
        self.assertEqual(body['name'], 'admin-build-failure')
        self.assertNotIn('fixture-acr-password', body['output']['text'])
        self.assertNotIn('fixture-check-token', body['output']['text'])
        self.assertIn('compiler', body['output']['text'])

    def test_wrong_context_or_unverified_source_is_rejected(self):
        for override in ({'GITHUB_REF': 'refs/pull/1/merge'}, {'BUILD_SERVICE': 'wallet-rpc'}, {'SOURCE_SHA': 'x'}, {'GITHUB_REPOSITORY': 'other/repo'}):
            with patch.dict(os.environ, dict(ENV, **override)), self.assertRaises(ValueError):
                REPORT.validate_target()
        for outputs in ([SHA + '\n', 'b' * 40 + '\n'], ['b' * 40 + '\n']):
            results = [subprocess.CompletedProcess([], 0, value.encode(), b'') for value in outputs]
            with patch.dict(os.environ, ENV), patch.object(REPORT.subprocess, 'run', side_effect=results), self.assertRaises(ValueError):
                REPORT.validate_target()
        results = [subprocess.CompletedProcess([], 0, (SHA + '\n').encode(), b'')] * 2
        with patch.dict(os.environ, ENV), patch.object(REPORT.subprocess, 'run', side_effect=results):
            self.assertEqual(REPORT.validate_target(), (SHA, '12', '1'))


if __name__ == '__main__':
    unittest.main()
