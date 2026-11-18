"""核对整批重跑制品隔离，禁止混入以前 attempt 的计划或服务收据。"""
from pathlib import Path
import unittest


class WorkflowReceipts(unittest.TestCase):
    def test_plan_and_receipts_are_named_and_downloaded_for_current_attempt(self):
        content = (Path(__file__).parents[1] / '.github/workflows/full-version-build.yml').read_text()
        self.assertEqual(content.count('name: source-build-plan-${{ github.run_attempt }}'), 2)
        self.assertIn('name: candidate-${{ matrix.service }}-attempt-${{ github.run_attempt }}', content)
        self.assertIn('pattern: candidate-*-attempt-${{ github.run_attempt }}', content)
        self.assertEqual(content.count('name: oci-receipts-attempt-${{ github.run_attempt }}'), 2)
        self.assertIn('needs.distribute.result ==', content)
        self.assertNotIn('pattern: receipt-*\n', content)
        self.assertIn('Re-run all jobs', content)
        self.assertIn('  queue: max', content)
        self.assertIn('只重跑失败任务', content)


if __name__ == '__main__':
    unittest.main()
