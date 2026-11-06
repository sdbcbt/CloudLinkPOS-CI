"""验证跨仓库检查不会接受其他仓库、旧提交或不匹配的合并结果。"""
import importlib.util
import os
from pathlib import Path
import subprocess
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

    def api(self, compare=None):
        """模拟 GitHub：PR 详情与 compare 结果；compare 以 (left, right) 为键。"""
        compare = compare or {}
        def call(path, *args):
            if path.startswith("/repos/" + quality.REPO + "/compare/"):
                left, right = path.rsplit("/", 1)[1].split("...")
                return {"status": compare.get((left, right), "ahead")}
            return self.pr
        return call

    def test_valid_pr_merges_head_on_current_base_locally(self):
        with patch.dict(os.environ, self.env), patch.object(quality, "api", side_effect=self.api()):
            value = quality.context()
        self.assertEqual(value["ref"], "refs/pull/63/head")
        self.assertEqual(value["commit"], self.head)
        self.assertEqual(value["parents"], [self.base, self.head])

    def test_base_advance_keeps_checking_the_same_head(self):
        current = "e" * 40
        self.pr["base"]["sha"] = current
        with patch.dict(os.environ, self.env), patch.object(quality, "api", side_effect=self.api()):
            value = quality.context()
        self.assertEqual(value["base"], current, "main 前移后按当前基准检查")
        self.assertEqual(value["parents"], [current, self.head])
        self.assertEqual(value["notifiedBase"], self.base, "保留通知 base 供首父历史校验")

    def test_rejects_rewritten_or_off_main_base(self):
        current = "e" * 40
        self.pr["base"]["sha"] = current
        for key, status in (((self.base, current), "diverged"), ((current, "main"), "behind")):
            with self.subTest(key=key):
                with patch.dict(os.environ, self.env), patch.object(quality, "api", side_effect=self.api({key: status})):
                    with self.assertRaises(ValueError):
                        quality.context()

    def test_rejects_fork_stale_head_closed_and_other_target(self):
        for changed in ("repo", "sha", "state", "ref"):
            with self.subTest(changed=changed):
                self.setUp()
                if changed == "repo":
                    self.pr["head"]["repo"] = {"full_name": "other/repo"}
                elif changed == "sha":
                    self.pr["head"]["sha"] = "d" * 40
                elif changed == "state":
                    self.pr["state"] = "closed"
                else:
                    self.pr["base"]["ref"] = "release"
                with patch.dict(os.environ, self.env), patch.object(quality, "api", side_effect=self.api()):
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


class MainlineTest(unittest.TestCase):
    def test_accepts_forward_move_on_first_parent_chain(self):
        quality.check_mainline(["c", "b", "a"], "c", "a")
        quality.check_mainline(["c", "b", "a"], "b", "b")

    def test_rejects_side_branch_or_backward_base(self):
        with self.assertRaises(ValueError):
            quality.check_mainline(["c", "m", "a"], "c", "side")
        with self.assertRaises(ValueError):
            quality.check_mainline(["c", "b", "a"], "a", "c")
        with self.assertRaises(ValueError):
            quality.check_mainline(["c", "b", "a"], "x", "a")


class WorkflowScopeTest(unittest.TestCase):
    def test_pr_scope_uses_verified_current_base(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/private-quality.yml").read_text()
        self.assertIn('PR_BASE: "${{ fromJSON(needs.context.outputs.context).base }}"', workflow)


class LocalMergeTest(unittest.TestCase):
    def repo(self):
        root = Path(tempfile.mkdtemp())
        def git(*args):
            return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                                  check=True, stdout=subprocess.PIPE).stdout.decode().strip()
        git("init", "--quiet")
        (root / "a.txt").write_text("1\n")
        git("add", "a.txt")
        git("commit", "--quiet", "-m", "init")
        return root, git

    def test_same_inputs_produce_same_merge_commit(self):
        root, git = self.repo()
        git("checkout", "--quiet", "-b", "feature")
        (root / "b.txt").write_text("feature\n")
        git("add", "b.txt")
        git("commit", "--quiet", "-m", "feature")
        head = git("rev-parse", "HEAD")
        git("checkout", "--quiet", "-")
        (root / "c.txt").write_text("main moved\n")
        git("add", "c.txt")
        git("commit", "--quiet", "-m", "bot")
        base = git("rev-parse", "HEAD")
        quality.local_merge(root, base, head)
        first = git("rev-parse", "HEAD")
        quality.local_merge(root, base, head)
        self.assertEqual(git("rev-parse", "HEAD"), first, "各作业本地合并结果必须一致")
        self.assertEqual(git("show", "-s", "--format=%P", "HEAD").split(), [base, head])

    def test_conflict_rejects_check(self):
        root, git = self.repo()
        git("checkout", "--quiet", "-b", "feature")
        (root / "a.txt").write_text("feature\n")
        git("commit", "--quiet", "-am", "feature")
        head = git("rev-parse", "HEAD")
        git("checkout", "--quiet", "-")
        (root / "a.txt").write_text("main\n")
        git("commit", "--quiet", "-am", "main")
        base = git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "冲突"):
            quality.local_merge(root, base, head)


if __name__ == "__main__":
    unittest.main()
