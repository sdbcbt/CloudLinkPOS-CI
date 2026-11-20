import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/miniprogram-upload.yml"
JOBS = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


class MiniprogramWorkflowTest(unittest.TestCase):
    def test_embedded_shell_syntax(self):
        for job in JOBS.values():
            for step in job["steps"]:
                if "run" in step:
                    subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)

    def test_upload_plan_preserves_app_selection(self):
        script = next(step["run"] for step in JOBS["plan"]["steps"] if step.get("id") == "plan")
        cases = [
            ([], []),
            ([{"app": "member", "key": "MEMBER"}], [("member", "weixin"), ("member", "alipay")]),
            (
                [{"app": "organization", "key": "ORGANIZATION"}, {"app": "store-front", "key": "STORE_FRONT"}],
                [("organization", "weixin"), ("organization", "alipay"), ("store-front", "weixin")],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner = root / "source/deploy/miniprogram/upload.mjs"
            planner.parent.mkdir(parents=True)
            planner.write_text("process.stdout.write(process.env.TEST_APPS)\n", encoding="utf-8")
            output = root / "output"
            for apps, expected_jobs in cases:
                with self.subTest(apps=apps):
                    output.write_text("", encoding="utf-8")
                    env = dict(os.environ, GITHUB_OUTPUT=str(output), TEST_APPS=json.dumps(apps))
                    subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True, check=True)
                    fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
                    actual = [(job["app"], job["platform"]) for job in json.loads(fields["apps"])]
                    self.assertEqual(actual, expected_jobs)
                    self.assertEqual(fields["has_apps"], str(bool(expected_jobs)).lower())

    def test_alipay_upload_refuses_mismatched_target_and_requires_version_receipt(self):
        script = next(
            step["run"] for step in JOBS["upload"]["steps"]
            if "MP_ALIPAY_IDENTITY_JSON" in step.get("env", {})
        )
        appid = "2021000000000001"
        tool_id = "a" * 32
        private_key = subprocess.check_output(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "source/apps/miniprogram/member/dist/build/mp-alipay/mini.project.json"
            project.parent.mkdir(parents=True)
            cli = root / "minidev/node_modules/.bin/minidev"
            cli.parent.mkdir(parents=True)
            cli.write_text(
                """#!/bin/bash
set -eu
[[ -z "${MP_ALIPAY_IDENTITY_JSON:-}" ]]
[[ "$1" == upload && "$4" == --app-id && "$5" == "$MP_APPID" ]]
python3 - "$3/mini.project.json" "$7" <<'PYTHON'
import json
import os
from pathlib import Path
import sys
config, identity = map(Path, sys.argv[1:])
assert json.loads(config.read_text())["appid"] == os.environ["MP_APPID"]
assert (identity.stat().st_mode & 0o777) == 0o600
auth = json.loads(identity.read_text())["alipay"]["authentication"]
assert auth["toolId"] == "a" * 32 and auth["privateKey"].startswith("-----BEGIN PRIVATE KEY-----")
PYTHON
touch "$RUNNER_TEMP/cli-invoked"
if [[ "$TEST_RECEIPT" == yes ]]; then printf '本次上传版本: %s\\n' "$9"; fi
""",
                encoding="utf-8",
            )
            cli.chmod(0o700)
            env = dict(
                os.environ,
                APP="member",
                GITHUB_RUN_ATTEMPT="1",
                GITHUB_RUN_NUMBER="2",
                GITHUB_STEP_SUMMARY=str(root / "summary"),
                MP_APPID=appid,
                MP_ALIPAY_IDENTITY_JSON=json.dumps({"toolId": tool_id, "privateKey": private_key}),
                PRIVATE_LOG=str(root / "private.log"),
                RUNNER_TEMP=str(root),
                SOURCE_SHA="b" * 40,
            )
            cases = [
                ("2021000000000002", "yes", False, False),
                ("touristappid", "no", False, True),
                ("touristappid", "yes", True, True),
            ]
            for artifact_id, receipt, success, called in cases:
                with self.subTest(artifact_id=artifact_id, receipt=receipt):
                    project.write_text(json.dumps({"appid": artifact_id}), encoding="utf-8")
                    (root / "cli-invoked").unlink(missing_ok=True)
                    (root / "private.log").unlink(missing_ok=True)
                    result = subprocess.run(
                        ["bash", "-e", "-c", script],
                        cwd=root,
                        env=dict(env, TEST_RECEIPT=receipt),
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    self.assertEqual((root / "cli-invoked").exists(), called)
                    self.assertFalse((root / "alipay-identity.json").exists())
                    if success:
                        self.assertIn("MP-UPLOAD-RESULT app=member platform=alipay", result.stdout)


if __name__ == "__main__":
    unittest.main()
