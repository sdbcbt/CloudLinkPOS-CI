"""核验固定父摘要的官方双入口兜底、平台边界和公开诊断脱敏。"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('public_base', Path(__file__).with_name('pull-ci-base-image.py'))
BASE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BASE)
DIGEST = 'sha256:' + 'a' * 64
ECR = 'public.ecr.aws/docker/library/alpine:3.22.1@' + DIGEST
HUB = 'docker.io/library/alpine:3.22.1@' + DIGEST


def helper():
    return SimpleNamespace(SHARED=SimpleNamespace(OFFICIAL_BASE_IMAGES={'alpine': ECR, 'nginx': 'public.ecr.aws/docker/library/nginx:1.27.5-alpine@' + DIGEST}, reject_symlink_ancestors=Mock()), INDEX_MEDIA_TYPES=('application/vnd.oci.image.index.v1+json',), verify_index_platform=Mock(), remote_inspect=Mock())


def result(code=0, content=b'', error=b''):
    return subprocess.CompletedProcess([], code, content, error)


def metadata(image, **changes):
    repository = image.split('@')[0].rsplit(':', 1)[0]
    item = {'RepoDigests': [repository + '@' + DIGEST], 'Os': 'linux', 'Architecture': 'amd64'}
    item.update(changes)
    return result(content=json.dumps([item]).encode())


class OfficialBase(unittest.TestCase):
    def test_candidates_keep_fixed_source_tag_and_parent_digest(self):
        value = helper()
        self.assertEqual(BASE.candidates(value, 'alpine'), [('ecr', ECR), ('dockerhub', HUB)])
        self.assertTrue(all(image.endswith('@' + DIGEST) for _, image in BASE.candidates(value, 'nginx')))
        for image in ('evil.example/alpine:3.22.1@' + DIGEST, 'public.ecr.aws/docker/library/alpine:latest@' + DIGEST):
            value.SHARED.OFFICIAL_BASE_IMAGES['alpine'] = image
            with self.assertRaises(BASE.IntegrityError):
                BASE.candidates(value, 'alpine')

    def test_ecr_failure_uses_verified_hub_and_preserves_output_contract(self):
        commands = []
        def command(args, timeout):
            commands.append(args)
            if args[1] == 'pull':
                return result(1, error=b'sensitive password=private-code') if args[-1] == ECR else result()
            return metadata(HUB, RepoDigests=['alpine@' + DIGEST])
        with tempfile.TemporaryDirectory() as directory, patch.object(BASE, 'load_private_helper', return_value=helper()), patch.object(BASE, 'captured', side_effect=command), contextlib.redirect_stdout(io.StringIO()) as log:
            output = Path(directory) / 'base.out'
            self.assertEqual(BASE.prepare(Path(directory), 'alpine', output), HUB)
            self.assertEqual(output.read_text(), 'base_image=' + HUB + '\n')
        self.assertEqual([args[-1] for args in commands if args[1] == 'pull'], [ECR, HUB])
        self.assertNotIn('password', log.getvalue())
        self.assertNotIn('private-code', log.getvalue())
        self.assertNotIn('https://', log.getvalue())
        self.assertIn('dockerhub', log.getvalue())

    def test_successful_ecr_does_not_pull_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(BASE, 'load_private_helper', return_value=helper()), patch.object(BASE, 'captured', side_effect=[result(), metadata(ECR)]) as command, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(BASE.prepare(Path(directory), 'alpine', Path(directory) / 'out'), ECR)
        self.assertEqual(command.call_count, 2)

    def test_both_fail_without_output_or_raw_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(BASE, 'load_private_helper', return_value=helper()), patch.object(BASE, 'captured', return_value=result(1, error=b'secret token=private-path')) as command, contextlib.redirect_stdout(io.StringIO()) as log:
            output = Path(directory) / 'out'
            with self.assertRaisesRegex(ValueError, 'BASE_OFFICIAL_SOURCES_EXHAUSTED'):
                BASE.prepare(Path(directory), 'alpine', output)
            self.assertFalse(output.exists())
        self.assertEqual(command.call_count, 2)
        self.assertNotIn('secret', log.getvalue())
        self.assertNotIn('token=', log.getvalue())

    def test_wrong_digest_or_platform_stops_without_trying_other_source(self):
        for changes in ({'RepoDigests': ['alpine@sha256:' + 'f' * 64]}, {'Architecture': 'arm64'}, {'Os': '', 'Architecture': ''}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, patch.object(BASE, 'load_private_helper', return_value=helper()), patch.object(BASE, 'captured', side_effect=[result(), metadata(ECR, **changes)]) as command, contextlib.redirect_stdout(io.StringIO()):
                output = Path(directory) / 'out'
                with self.assertRaises(BASE.IntegrityError):
                    BASE.prepare(Path(directory), 'alpine', output)
                self.assertFalse(output.exists())
                self.assertEqual(command.call_count, 2)

    def test_empty_platform_index_delegates_parent_and_child_proof(self):
        value = helper()
        descriptor = {'mediaType': 'application/vnd.oci.image.index.v1+json', 'digest': DIGEST}
        with patch.object(BASE, 'captured', return_value=metadata(ECR, Os='', Architecture='', Descriptor=descriptor)), contextlib.redirect_stdout(io.StringIO()):
            BASE.verify_candidate(value, 'ecr', 'alpine', ECR)
        value.verify_index_platform.assert_called_once_with(ECR)
        value.verify_index_platform.side_effect = ValueError('官方基镜像远端索引与固定父摘要不匹配 sensitive')
        with patch.object(BASE, 'captured', return_value=metadata(ECR, Os='', Architecture='', Descriptor=descriptor)), contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(BASE.IntegrityError):
            BASE.verify_candidate(value, 'ecr', 'alpine', ECR)
        self.assertNotIn('sensitive', log.getvalue())

    def test_transient_pull_failure_retries_same_digest_with_bounded_wait(self):
        with patch.object(BASE, 'captured', side_effect=[result(1, error=b'429 sensitive'), result(1, error=b'connection reset private'), result()]) as command, patch.object(BASE.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()) as log:
            BASE.pull_candidate('ecr', ECR)
        self.assertEqual(command.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])
        self.assertTrue(all(call.args[0][-1] == ECR for call in command.call_args_list))
        self.assertNotIn('sensitive', log.getvalue())
        self.assertNotIn('private', log.getvalue())

    def test_timeout_exception_is_sanitized_and_fallback_remains_fixed(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(BASE, 'load_private_helper', return_value=helper()), patch.object(BASE, 'pull_candidate', side_effect=[subprocess.TimeoutExpired('secret-command', 120, stderr=b'private-code'), None]), patch.object(BASE, 'captured', return_value=metadata(HUB)), contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(BASE.prepare(Path(directory), 'alpine', Path(directory) / 'out'), HUB)
        self.assertIn('TimeoutExpired', log.getvalue())
        self.assertNotIn('secret-command', log.getvalue())
        self.assertNotIn('private-code', log.getvalue())

    def test_remote_index_and_config_failures_have_fixed_separate_diagnostics(self):
        for purpose, stage in [('父索引读取', 'remote-index'), ('子镜像配置读取', 'remote-config')]:
            value = helper()
            original = value.remote_inspect
            value.verify_index_platform.side_effect = lambda image: value.remote_inspect(['docker', 'sensitive-argument'], purpose)
            descriptor = {'mediaType': 'application/vnd.oci.image.index.v1+json', 'digest': DIGEST}
            responses = [metadata(ECR, Os='', Architecture='', Descriptor=descriptor)] + [result(1, error=b'502 sensitive-token private-path')] * 3
            with self.subTest(stage=stage), patch.object(BASE, 'captured', side_effect=responses), patch.object(BASE.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(RuntimeError):
                BASE.verify_candidate(value, 'ecr', 'alpine', ECR)
            self.assertIn('"stage": "' + stage + '"', log.getvalue())
            self.assertIn('"httpStatus": 502', log.getvalue())
            self.assertNotIn('sensitive', log.getvalue())
            self.assertNotIn('private-path', log.getvalue())
            self.assertIs(value.remote_inspect, original)

    def test_http_authentication_and_local_inspect_failures_are_distinguished(self):
        with patch.object(BASE, 'captured', return_value=result(1, error=b'403 secret-token')) as command, contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(RuntimeError):
            BASE.checked_command('ecr', 'pull', ['docker', 'private-command'], 120)
        self.assertEqual(command.call_count, 1)
        self.assertIn('"reason": "authentication"', log.getvalue())
        self.assertIn('"httpStatus": 403', log.getvalue())
        self.assertNotIn('secret-token', log.getvalue())
        with patch.object(BASE, 'captured', return_value=result(1, error=b'no such image private-code')), contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(RuntimeError):
            BASE.verify_candidate(helper(), 'ecr', 'alpine', ECR)
        self.assertIn('"stage": "local-inspect"', log.getvalue())
        self.assertIn('"reason": "docker_error"', log.getvalue())
        self.assertNotIn('private-code', log.getvalue())

    def test_invalid_inspect_json_fails_closed_with_structural_diagnostic(self):
        with patch.object(BASE, 'captured', return_value=result(content=b'sensitive malformed input')), contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(BASE.IntegrityError):
            BASE.verify_candidate(helper(), 'ecr', 'alpine', ECR)
        self.assertIn('"reason": "structure"', log.getvalue())
        self.assertNotIn('sensitive', log.getvalue())


    def test_private_import_and_unknown_uppercase_errors_never_reach_cli_logs(self):
        marker = 'UPPERCASE_SENSITIVE_MARKER'
        fixtures = [
            "print('" + marker + "')\nraise ValueError('" + marker + "')\n",
            "from types import SimpleNamespace\ndef reject(path):\n    raise ValueError('" + marker + "')\nSHARED = SimpleNamespace(reject_symlink_ancestors=reject)\n",
        ]
        for index, content in enumerate(fixtures):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                scripts = root / 'deploy/scripts'
                scripts.mkdir(parents=True)
                (scripts / 'pull-ci-base-image.py').write_text(content)
                command = [sys.executable, str(Path(BASE.__file__)), '--repo-root', directory, '--profile', 'alpine', '--github-output', str(root / 'out')]
                actual = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(actual.returncode, 1)
                self.assertNotIn(marker, actual.stdout + actual.stderr)
                self.assertNotIn(directory, actual.stdout + actual.stderr)
                self.assertNotIn('Traceback', actual.stderr)
                expected = 'BASE_SOURCE_MODULE_INVALID' if index == 0 else 'BASE_PREPARATION_FAILED'
                self.assertIn(expected, actual.stderr)
                self.assertFalse((root / 'out').exists())
                with self.assertRaises(ValueError) as caught:
                    BASE.prepare(root, 'alpine', root / 'out')
                self.assertNotIn(marker, str(caught.exception))
                self.assertEqual(str(caught.exception), expected)



if __name__ == '__main__':
    unittest.main()
