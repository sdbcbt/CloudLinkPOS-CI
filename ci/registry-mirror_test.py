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
        # 已满状态仍只读报告，允许构建后在上传准备阶段清理额外历史再判容量。
        for status in (
            {'budgetBytes': 9000000000, 'usedBytes': 9000000000, 'reservedBytes': 0},
            {'budgetBytes': 9000000000, 'usedBytes': 8999999990, 'reservedBytes': 11},
        ):
            with self.subTest(status=status), patch.object(MIRROR, 'cf', return_value=status) as full:
                MIRROR.preflight()
            full.assert_called_once_with('/status', None, method='GET')
        invalid = [
            {'budgetBytes': 9000000001},
            {'usedBytes': -1},
            {'reservedBytes': -1},
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

    def test_only_explicit_prepare_busy_response_is_retried(self):
        cases = [(429, {'x-registry-publication-busy': '1', 'Retry-After': '5'}, MIRROR.PublicationBusy), (429, {}, ValueError), (507, {'x-registry-publication-busy': '1'}, ValueError), (401, {}, ValueError)]
        for status, headers, expected in cases:
            error = urllib.error.HTTPError('https://fixture.invalid', status, '', headers, None)
            with self.subTest(status=status, headers=headers), patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=error), self.assertRaises(expected) as caught:
                MIRROR.cf('/prepare', {})
            self.assertIs(type(caught.exception), expected)
        error = urllib.error.HTTPError('https://fixture.invalid', 429, '', {'x-registry-publication-busy': '1'}, None)
        with patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=error), self.assertRaises(ValueError) as caught:
            MIRROR.cf('/pins/add', {})
        self.assertIs(type(caught.exception), ValueError)

    def test_prepare_waits_on_busy_then_succeeds_and_stops_at_deadline(self):
        plan = {'digest': 'sha256:' + 'd' * 64}
        with patch.object(MIRROR, 'cf', side_effect=[MIRROR.PublicationBusy(), plan]) as cf, patch.object(MIRROR.time, 'sleep') as sleep, patch.object(MIRROR.random, 'uniform', return_value=9):
            self.assertIs(MIRROR.prepare_publication('employee-api', 'employee-api-v1.0.0', b'{}'), plan)
        self.assertEqual(cf.call_count, 2)
        sleep.assert_called_once_with(9)
        with patch.object(MIRROR, 'cf', side_effect=MIRROR.PublicationBusy()), patch.object(MIRROR.time, 'monotonic', side_effect=[0, MIRROR.PUBLICATION_WAIT_SECONDS]), patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaisesRegex(ValueError, 'CF_PUBLICATION_WAIT_TIMEOUT'):
            MIRROR.prepare_publication('employee-api', 'employee-api-v1.0.0', b'{}')
        sleep.assert_not_called()
        for reason in ('CF_HTTP_507', 'CF_HTTP_401', 'CF_HTTP_502'):
            with patch.object(MIRROR, 'cf', side_effect=ValueError(reason)) as cf, patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaisesRegex(ValueError, reason):
                MIRROR.prepare_publication('employee-api', 'employee-api-v1.0.0', b'{}')
            self.assertEqual(cf.call_count, 1)
            sleep.assert_not_called()

    def test_busy_deadline_is_checked_before_the_next_request(self):
        with patch.object(MIRROR, 'cf', side_effect=MIRROR.PublicationBusy()) as cf, patch.object(MIRROR.time, 'monotonic', side_effect=[0, 2695, 2700]), patch.object(MIRROR.time, 'sleep') as sleep, patch.object(MIRROR.random, 'uniform', return_value=12), self.assertRaisesRegex(ValueError, 'CF_PUBLICATION_WAIT_TIMEOUT'):
            MIRROR.prepare_publication('employee-api', 'sensitive-tag', b'{}')
        self.assertEqual(cf.call_count, 1)
        sleep.assert_called_once_with(5)

    def test_cf_timeout_and_logs_exclude_sensitive_request_and_error_contents(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import MagicMock
        secret = 'sensitive-marker'
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{}'
        output = io.StringIO()
        with redirect_stdout(output), patch.object(MIRROR, 'publication_token', return_value=secret), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', return_value=response) as opened:
            MIRROR.cf('/prepare', {'tag': secret, 'digest': secret})
        self.assertEqual(opened.call_args.kwargs['timeout'], 600)
        line = output.getvalue()
        self.assertNotIn(secret, line)
        self.assertNotIn('https://', line)
        self.assertIn('"category": "prepare"', line)
        self.assertIn('"httpStatus": 200', line)
        self.assertIn('"elapsedSeconds":', line)
        for failure, marker in [(TimeoutError(secret), 'TimeoutError'), (urllib.error.HTTPError(secret, 507, secret, {}, None), 'HTTPError')]:
            output = io.StringIO()
            with redirect_stdout(output), patch.object(MIRROR, 'publication_token', return_value=secret), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=failure), patch.object(MIRROR.time, 'sleep'), self.assertRaises(Exception):
                MIRROR.cf('/blob/' + secret, None, method='PUT', tag=secret)
            self.assertNotIn(secret, output.getvalue())
            self.assertIn('"category": "blob"', output.getvalue())
            self.assertIn(marker, output.getvalue())

    def test_blob_retry_reopens_file_and_recovers_lost_success_response(self):
        from unittest.mock import MagicMock
        import socket
        stored = []
        handles = []
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"exists":true}'
        def open_request(request, **kwargs):
            handles.append(request.data)
            stored.append(request.data.read())
            if len(handles) == 1:
                # 模拟服务端已收全文件，但成功响应返回时连接断开。
                raise urllib.error.URLError(ConnectionResetError(54, 'sensitive-marker'))
            return response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'blob'
            path.write_bytes(b'complete-blob')
            with patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=open_request), patch.object(MIRROR.time, 'sleep') as sleep:
                self.assertTrue(MIRROR.cf('/blob/sha256:' + 'd' * 64, path, method='PUT', tag='private-tag')['exists'])
        self.assertEqual(stored, [b'complete-blob', b'complete-blob'])
        self.assertIsNot(handles[0], handles[1])
        self.assertTrue(all(handle.closed for handle in handles))
        sleep.assert_called_once_with(1)

    def test_four_attempt_limit_preserves_error_and_certificate_and_capacity_fail_immediately(self):
        import ssl
        original = urllib.error.URLError(ConnectionResetError(54, 'sensitive-marker'))
        with patch.object(MIRROR, 'cf_once', side_effect=original) as once, patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaises(urllib.error.URLError) as caught:
            MIRROR.cf('/commit', {'tag': 'private-tag', 'digest': 'private-digest'})
        self.assertIs(caught.exception, original)
        self.assertEqual(once.call_count, 4)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [1, 3, 6])
        cert = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'private-message'))
        for error in (cert, ValueError('CF_HTTP_507'), ValueError('CF_HTTP_401'), ValueError('CF_HTTP_429')):
            with patch.object(MIRROR, 'cf_once', side_effect=error) as once, patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaises(Exception) as caught:
                MIRROR.cf('/prepare', {})
            self.assertIs(caught.exception, error)
            self.assertEqual(once.call_count, 1)
            sleep.assert_not_called()

    def test_fourth_blob_transient_retry_succeeds_and_exhaustion_aborts_original_error(self):
        digest = 'sha256:' + 'd' * 64
        raw = b'{}'
        blob_calls = 0
        paths = []
        def once(path, value, **kwargs):
            nonlocal blob_calls
            paths.append(path)
            if path == '/prepare':
                return {'digest': digest, 'missing': [{'digest': digest, 'size': 1}] * 4}
            if path.startswith('/blob/'):
                blob_calls += 1
                if blob_calls == 4:
                    raise urllib.error.URLError(ConnectionResetError(54, 'sensitive-marker'))
            if path == '/commit':
                return {'digest': digest}
            if path == '/verify':
                return {'verified': True, 'blobDigestsVerified': True, 'repository': 'sdbcbt/cloudlinkpos', 'tag': 'employee-api-v1.0.0', 'digest': digest, 'latest': 'employee-api-latest'}
            return {}
        with patch.dict(MIRROR.os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), patch.object(MIRROR.PILOT.Registry, 'verify_image', return_value=raw), patch.object(MIRROR, 'cf_once', side_effect=once), patch.object(MIRROR, 'download_blob', side_effect=lambda desc, file, token: file.write_bytes(b'x')), patch.object(MIRROR.PILOT, 'token', return_value='private-token'), patch.object(MIRROR.PILOT, 'request', return_value=(401, {}, b'')), patch.object(MIRROR.time, 'sleep'):
            self.assertTrue(MIRROR.mirror('employee-api', 'employee-api-v1.0.0', digest, raw, lambda: [])['authenticated_read'])
        self.assertEqual(blob_calls, 5)
        self.assertNotIn('/abort', paths)

    def test_http_gateway_failures_retry_but_non_idempotent_abort_does_not(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{}'
        for status in (502, 503, 504):
            error = urllib.error.HTTPError('https://fixture.invalid', status, '', {}, None)
            with self.subTest(status=status), patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=[error, response]) as opened, patch.object(MIRROR.time, 'sleep') as sleep:
                self.assertEqual(MIRROR.cf('/verify', {}), {})
            self.assertEqual(opened.call_count, 2)
            sleep.assert_called_once_with(1)
        original = urllib.error.URLError(ConnectionResetError(54, 'private-message'))
        with patch.object(MIRROR, 'cf_once', side_effect=original) as once, patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaises(urllib.error.URLError):
            MIRROR.cf('/abort', {})
        self.assertEqual(once.call_count, 1)
        sleep.assert_not_called()

    def test_exhausted_blob_retry_triggers_abort_without_replacing_original_error(self):
        original = urllib.error.URLError(ConnectionResetError(54, 'private-message'))
        digest = 'sha256:' + 'd' * 64
        raw = b'{}'
        calls = []
        def once(path, value, **kwargs):
            calls.append(path)
            if path == '/prepare':
                return {'digest': digest, 'missing': [{'digest': digest, 'size': 1}]}
            if path.startswith('/blob/'):
                raise original
            if path == '/abort':
                raise RuntimeError('cleanup failed')
            return {}
        with patch.dict(MIRROR.os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), patch.object(MIRROR.PILOT.Registry, 'verify_image', return_value=raw), patch.object(MIRROR, 'cf_once', side_effect=once), patch.object(MIRROR, 'download_blob', side_effect=lambda desc, file, token: file.write_bytes(b'x')), patch.object(MIRROR.PILOT, 'token', return_value='private-token'), patch.object(MIRROR.time, 'sleep'), self.assertRaises(urllib.error.URLError) as caught:
            MIRROR.mirror('employee-api', 'employee-api-v1.0.0', digest, raw, lambda: [])
        self.assertIs(caught.exception, original)
        self.assertEqual(sum(path.startswith('/blob/') for path in calls), 4)
        self.assertEqual(calls[-1], '/abort')

    def test_network_retry_does_not_cross_an_active_publication_queue_deadline(self):
        original = urllib.error.URLError(ConnectionResetError(54, 'private-message'))
        with patch.object(MIRROR, 'cf_once', side_effect=original) as once, patch.object(MIRROR.time, 'monotonic', side_effect=[2699, 2700]), patch.object(MIRROR.time, 'sleep'), self.assertRaisesRegex(ValueError, 'CF_PUBLICATION_WAIT_TIMEOUT'):
            MIRROR.cf('/prepare', {}, deadline=2700)
        self.assertEqual(once.call_count, 1)

    def test_safe_reason_log_distinguishes_dns_reset_and_ssl_without_messages(self):
        import io
        import socket
        import ssl
        from contextlib import redirect_stdout
        for reason in (socket.gaierror(-2, 'sensitive-marker'), ConnectionResetError(54, 'sensitive-marker'), ssl.SSLCertVerificationError(1, 'sensitive-marker')):
            output = io.StringIO()
            with redirect_stdout(output), patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=urllib.error.URLError(reason)), patch.object(MIRROR.time, 'sleep'), self.assertRaises(urllib.error.URLError):
                MIRROR.cf('/verify', {'tag': 'sensitive-marker'})
            self.assertNotIn('sensitive-marker', output.getvalue())
            self.assertIn(type(reason).__name__, output.getvalue())
            self.assertIn(str(reason.errno), output.getvalue())

    def test_credentials_are_never_forwarded_to_untrusted_download_redirect(self):
        error = urllib.error.HTTPError('https://ghcr.io', 307, '', {'Location': 'https://evil.example/blob'}, None)
        with tempfile.TemporaryDirectory() as directory, patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=error), self.assertRaisesRegex(ValueError, 'UNTRUSTED_BLOB_REDIRECT'):
            MIRROR.download_blob({'digest': 'sha256:' + 'd' * 64, 'size': 10}, Path(directory) / 'blob', 'private-token')

    def test_legacy_transient_auth_401_retries_with_fresh_identity_and_safe_reason(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{}'
        for reason in sorted(MIRROR.TRANSIENT_AUTH_REASONS):
            failure = urllib.error.HTTPError('https://private-fixture.invalid', 401, 'private-body-marker', {'x-registry-auth-reason': reason}, None)
            with self.subTest(reason=reason), patch.object(MIRROR, 'publication_token', side_effect=['first-secret-token', 'fresh-secret-token']) as token, patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=[failure, response]) as opened, patch.object(MIRROR.time, 'sleep') as sleep, redirect_stdout(io.StringIO()) as log:
                self.assertEqual(MIRROR.cf('/verify', {}), {})
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(token.call_count, 2)
            sleep.assert_called_once_with(1)
            self.assertEqual(opened.call_args_list[0].args[0].get_header('Authorization'), 'Bearer first-secret-token')
            self.assertEqual(opened.call_args_list[1].args[0].get_header('Authorization'), 'Bearer fresh-secret-token')
            self.assertIn('"authReason": "' + reason + '"', log.getvalue())
            self.assertNotIn('secret-token', log.getvalue())
            self.assertNotIn('private-fixture', log.getvalue())
            self.assertNotIn('private-body-marker', log.getvalue())

    def test_permanent_or_unknown_401_never_retries_or_exposes_unknown_reason(self):
        import io
        from contextlib import redirect_stdout
        permanent = ['', 'FORMAT', 'HEADER', 'TIME', 'CLAIMS', 'KEY_FETCH', 'KEY_FETCH_SYNTAXERROR', 'KEY_ID', 'KEY_IMPORT', 'SIGNATURE', 'CREDENTIALS', 'JWKS_HTTP_401', 'JWKS_HTTP_404', 'KEY_FETCH_TIMEOUTERROR_PRIVATE_SECRET']
        for reason in permanent:
            failure = urllib.error.HTTPError('https://private.invalid', 401, 'private-body', {'x-registry-auth-reason': reason, 'x-private-debug': 'private-header'}, None)
            with self.subTest(reason=reason), patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=failure) as opened, patch.object(MIRROR.time, 'sleep') as sleep, redirect_stdout(io.StringIO()) as log, self.assertRaises(ValueError) as caught:
                MIRROR.cf('/prepare', {})
            self.assertIs(type(caught.exception), ValueError)
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()
            self.assertNotIn('PRIVATE_SECRET', log.getvalue() + str(caught.exception))
            self.assertNotIn('private-header', log.getvalue())
            self.assertNotIn('private-body', log.getvalue())
            if reason not in MIRROR.SAFE_AUTH_REASONS:
                self.assertNotIn('authReason', log.getvalue())

    def test_transient_auth_exhausts_four_attempts_and_abort_is_not_retried(self):
        reason = 'KEY_FETCH_TIMEOUTERROR'
        failure = urllib.error.HTTPError('https://private.invalid', 401, '', {'x-registry-auth-reason': reason}, None)
        with patch.object(MIRROR, 'publication_token', return_value='private-token') as token, patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=failure) as opened, patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaises(MIRROR.TransientHttpError):
            MIRROR.cf('/pins/add', {})
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(token.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3, 6])
        with patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=failure) as opened, patch.object(MIRROR.time, 'sleep') as sleep, self.assertRaises(MIRROR.TransientHttpError):
            MIRROR.cf('/abort', {})
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()

    def test_worker_503_logs_only_allowlisted_auth_reason(self):
        import io
        from contextlib import redirect_stdout
        for reason in ('KEY_FETCH_TYPEERROR', 'JWKS_HTTP_503', 'JWKS_HTTP_503_PRIVATE_SECRET'):
            failure = urllib.error.HTTPError('https://private.invalid', 503, 'private-body', {'x-registry-auth-reason': reason}, None)
            with self.subTest(reason=reason), patch.object(MIRROR, 'publication_token', return_value='private-token'), patch.object(MIRROR.urllib.request.OpenerDirector, 'open', side_effect=failure), redirect_stdout(io.StringIO()) as log, self.assertRaises(MIRROR.TransientHttpError) as caught:
                MIRROR.cf_once('/verify', {})
            self.assertNotIn('PRIVATE_SECRET', log.getvalue() + str(caught.exception))
            self.assertNotIn('private-body', log.getvalue())
            if reason in MIRROR.SAFE_AUTH_REASONS:
                self.assertIn('"authReason": "' + reason + '"', log.getvalue())
            else:
                self.assertNotIn('authReason', log.getvalue())



if __name__ == '__main__':
    unittest.main()
