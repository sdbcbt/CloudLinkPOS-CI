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


if __name__ == '__main__':
    unittest.main()
