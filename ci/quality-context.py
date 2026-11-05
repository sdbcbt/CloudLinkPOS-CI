"""验证私库通知、检出固定提交，并回写检查结果。"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

REPO = "sdbcbt/CloudLinkPOS"
JOBS = ("scope", "backend", "pos", "admin", "semgrep")


def api(path, body=None, method=None):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["SOURCE_TOKEN"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "cloudlinkpos-ci",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def context():
    head = os.environ["SOURCE_HEAD"]
    base = os.environ["SOURCE_BASE"]
    if not all(re.fullmatch("[0-9a-f]{40}", value) for value in (head, base)):
        raise ValueError("通知缺少完整提交")
    if os.environ["SOURCE_EVENT"] == "pr":
        number = os.environ["SOURCE_PR"]
        if not re.fullmatch("[1-9][0-9]*", number):
            raise ValueError("PR 编号无效")
        pr = api(f"/repos/{REPO}/pulls/{number}")
        # GitHub 更新 PR 后可能尚未计算合并状态，有限重读相同事件。
        for _ in range(3):
            if pr["mergeable"] is not None:
                break
            time.sleep(2)
            pr = api(f"/repos/{REPO}/pulls/{number}")
        if pr["head"]["repo"]["full_name"] != REPO or pr["base"]["ref"] != "main":
            raise ValueError("PR 来源不在允许范围")
        if pr["head"]["sha"] != head or pr["base"]["sha"] != base:
            raise ValueError("PR 已更新，请等待最新通知")
        if pr["state"] != "open" or pr["mergeable"] is not True:
            raise ValueError("PR 已关闭、存在冲突或合并状态尚未就绪")
        ref = f"refs/pull/{number}/merge"
        commit = pr["merge_commit_sha"]
        parents = [base, head]
    elif os.environ["SOURCE_EVENT"] == "push":
        ref = "refs/heads/main"
        commit = head
        parents = []
        comparison = api(f"/repos/{REPO}/compare/{head}...main")
        if comparison["status"] not in ("ahead", "identical"):
            raise ValueError("提交不在当前主线历史中")
    else:
        raise ValueError("不支持的通知类型")
    return {"head": head, "base": base, "ref": ref, "commit": commit, "parents": parents}


def initialize():
    ctx = context()
    url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    checks = {}
    for name in JOBS:
        check = api(f"/repos/{REPO}/check-runs", {
            "name": name,
            "head_sha": ctx["head"],
            "status": "in_progress",
            "details_url": url,
            "external_id": f"{os.environ['GITHUB_RUN_ID']}:{name}",
            "output": {"title": "公开 CI 检查中", "summary": "任务在 sdbcbt/CloudLinkPOS-CI 中执行。"},
        }, "POST")
        checks[name] = check["id"]
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("context=" + json.dumps(ctx, separators=(",", ":")) + "\n")
        output.write("checks=" + json.dumps(checks, separators=(",", ":")) + "\n")


def checkout():
    ctx = json.loads(os.environ["CHECK_CONTEXT"])
    root = Path("source")
    root.mkdir()
    def git(*args, capture=False):
        return subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE)
    git("init", "--quiet")
    git("remote", "add", "origin", f"https://github.com/{REPO}.git")
    # 认证仅在 fetch 命令的进程配置中提供，不保存到 Git 配置。
    credential = base64.b64encode(("x-access-token:" + os.environ["SOURCE_TOKEN"]).encode()).decode()
    git("-c", "http.https://github.com/.extraheader=AUTHORIZATION: basic " + credential,
        "fetch", "--quiet", "--no-tags", "origin", ctx["ref"])
    tip = git("rev-parse", "FETCH_HEAD", capture=True).stdout.decode().strip()
    if ctx["parents"]:
        if tip != ctx["commit"]:
            raise ValueError("PR 已更新，禁止检查其他提交")
    else:
        history = git("rev-list", "--first-parent", tip, capture=True).stdout.decode().splitlines()
        if ctx["commit"] not in history:
            raise ValueError("提交不在主线首父历史中")
    git("checkout", "--quiet", "--detach", ctx["commit"])
    if ctx["parents"]:
        actual = git("show", "-s", "--format=%P", "HEAD", capture=True).stdout.decode().split()
        if actual != ctx["parents"]:
            raise ValueError("合并提交与通知范围不匹配")
    git("merge-base", "--is-ancestor", ctx["base"], "HEAD")


def report():
    ids = json.loads(os.environ["CHECK_IDS"])
    results = json.loads(os.environ["CHECK_RESULTS"])
    for name in JOBS:
        result = results.get(name, {}).get("result", "failure")
        if name != "scope" and results.get("scope", {}).get("result") != "success":
            result = "failure"
        conclusion = {"success": "success", "skipped": "skipped", "cancelled": "cancelled"}.get(result, "failure")
        api(f"/repos/{REPO}/check-runs/{ids[name]}", {
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": "公开 CI 检查结果",
                "summary": f"{name}：{conclusion}。Details 链接可查看公开运行状态。",
            },
        }, "PATCH")


if __name__ == "__main__":
    try:
        {"init": initialize, "checkout": checkout, "report": report}[sys.argv[1]]()
    except Exception:
        # 公开日志仅记录阶段失败，私库源码与完整诊断留在临时执行器。
        print("::error::通知验证、源码读取或检查状态回写失败，请核对权限及提交是否已更新。", file=sys.stderr)
        raise SystemExit(1)
