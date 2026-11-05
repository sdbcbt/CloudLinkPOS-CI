"""核对版本递增及仓库分页失败保护。"""
import importlib.util
import json
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

    def test_registry_error_and_wrong_repository_are_rejected(self):
        class Client:
            repository = 'sdbcbt/cloudlinkpos'
            def authenticated(self, path):
                return 403, {}, b'{"name":"other","tags":[]}'
        with self.assertRaisesRegex(ValueError, '未核验'):
            BUILD.all_tags(Client())


if __name__ == '__main__':
    unittest.main()
