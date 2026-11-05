"""核对公开收据的范围绑定、字段白名单和失败闭锁。"""
import importlib.util
import json
import tempfile
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('publish_receipts', Path(__file__).with_name('publish-receipts.py'))
PUBLISH = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PUBLISH)


class PublicReceipts(unittest.TestCase):
    def execute(self, mutate=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {'GITHUB_SHA': 'c' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
            plan = {'schema': 'cloudlinkpos.public-source-plan/v1', 'sourceSHA': 'b' * 40, 'beforeSHA': 'a' * 40, 'expectedServices': ['user-api'], 'publicationPolicy': {'grayRequired': False, 'generation': 1}}
            receipt = {'schema': 'cloudlinkpos.public-full-build/v1', 'sourceRepository': 'sdbcbt/CloudLinkPOS', 'sourceSHA': 'b' * 40, 'buildRepository': PUBLISH.REPO, 'workflowSHA': env['GITHUB_SHA'], 'runID': '123', 'attempt': '2', 'service': 'user-api', 'repository': PUBLISH.IMAGE, 'tag': 'user-api-v1.2.4', 'digest': 'sha256:' + 'd' * 64, 'tagPolicy': 'version-and-latest-v1', 'metadataSHA256': 'e' * 64, 'privateEvidence': {'authenticated_read': True, 'anonymous_pull_denied': True, 'image_digest': 'sha256:' + 'd' * 64, 'unexpected': 'private'}, 'unexpected': 'private'}
            if mutate:
                mutate(plan, receipt)
            (root / 'source-build-plan.json').write_text(json.dumps(plan))
            (root / 'user-api.json').write_text(json.dumps(receipt))
            return PUBLISH.build_batch(root, env)

    def test_only_curated_metadata_is_published(self):
        batch = self.execute()
        self.assertNotIn('unexpected', batch['receipts'][0])
        self.assertNotIn('unexpected', batch['receipts'][0]['privateEvidence'])
        self.assertEqual(batch['publicationPolicy'], {'grayRequired': False, 'generation': 1})

    def test_wrong_source_attempt_tag_or_incomplete_batch_is_rejected(self):
        changes = [lambda p, r: r.update(sourceSHA='f' * 40), lambda p, r: r.update(attempt='1'), lambda p, r: r.update(tag='user-api-latest'), lambda p, r: p['expectedServices'].append('user-rpc'), lambda p, r: p.update(publicationPolicy={'grayRequired': None, 'generation': 1}), lambda p, r: r['privateEvidence'].update(anonymous_pull_denied=False)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.execute(change)


if __name__ == '__main__':
    unittest.main()
