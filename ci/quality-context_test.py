"""验证跨仓库检查不会接受其他仓库、旧提交或不匹配的合并结果。"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("quality_context", Path(__file__).with_name("quality-context.py"))
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.head = "a" * 40
        self.base = "b" * 40
        self.env = {"SOURCE_EVENT": "pr", "SOURCE_PR": "63", "SOURCE_HEAD": self.head, "SOURCE_BASE": self.base}
        self.pr = {
            "head": {"repo": {"full_name": quality.REPO}, "sha": self.head},
            "base": {"ref": "main", "sha": self.base},
            "state": "open", "mergeable": True, "merge_commit_sha": "c" * 40,
        }

    def test_valid_pr_uses_verified_merge_ref(self):
        with patch.dict(os.environ, self.env), patch.object(quality, "api", return_value=self.pr):
            value = quality.context()
        self.assertEqual(value["ref"], "refs/pull/63/merge")
        self.assertEqual(value["parents"], [self.base, self.head])

    def test_rejects_fork_and_stale_head(self):
        for changed in ("repo", "sha"):
            with self.subTest(changed=changed):
                self.setUp()
                self.pr["head"][changed] = {"full_name": "other/repo"} if changed == "repo" else "d" * 40
                with patch.dict(os.environ, self.env), patch.object(quality, "api", return_value=self.pr):
                    with self.assertRaises(ValueError):
                        quality.context()

    def test_earlier_main_push_keeps_its_exact_commit(self):
        self.env["SOURCE_EVENT"] = "push"
        with patch.dict(os.environ, self.env), patch.object(quality, "api", return_value={"status": "ahead"}):
            value = quality.context()
        self.assertEqual(value["commit"], self.head)
        self.assertEqual(value["base"], self.base)

    def test_failed_scope_cannot_make_dependent_checks_green(self):
        env = {"CHECK_IDS": '{"scope":1,"backend":2,"pos":3,"admin":4,"semgrep":5}',
               "CHECK_RESULTS": '{"scope":{"result":"failure"},"backend":{"result":"skipped"}}'}
        with patch.dict(os.environ, env), patch.object(quality, "api") as api:
            quality.report()
        self.assertTrue(all(call.args[1]["conclusion"] == "failure" for call in api.call_args_list))


if __name__ == "__main__":
    unittest.main()
