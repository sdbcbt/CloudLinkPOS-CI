"""将后台编译失败输出脱敏后仅写入私库固定提交的检查结果。"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

spec = importlib.util.spec_from_file_location('private_failure', Path(__file__).with_name('report-pos-failure.py'))
PRIVATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PRIVATE)
PRIVATE.LOG_NAME = 'cloudlinkpos-admin-first-failure.log'
PRIVATE.SECRET_KEYS += ('ACR_USERNAME', 'ACR_PASSWORD')


def validate_target():
    if os.environ.get('GITHUB_REPOSITORY') != 'sdbcbt/CloudLinkPOS-CI' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('仅允许公开 CI 主线回写失败详情')
    sha = os.environ.get('SOURCE_SHA', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch('[0-9a-f]{40}', sha) or os.environ.get('BUILD_SERVICE') != 'admin':
        raise ValueError('固定源码或服务无效')
    if not all(re.fullmatch('[1-9][0-9]*', value) for value in (run_id, attempt)):
        raise ValueError('运行标识无效')
    root = Path('source').resolve()
    def git(*args):
        result = subprocess.run(['git', *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise ValueError('固定源码核验失败')
        return result.stdout.decode('ascii').splitlines()
    if git('rev-parse', 'HEAD') != [sha] or sha not in git('rev-list', '--first-parent', 'FETCH_HEAD'):
        raise ValueError('固定源码不属于已核验主线')
    return sha, run_id, attempt


def report():
    sha, run_id, attempt = validate_target()
    token = os.environ['CHECKS_TOKEN']
    if not token:
        raise ValueError('缺少检查写入令牌')
    body = {
        'name': 'admin-build-failure', 'head_sha': sha, 'status': 'completed', 'conclusion': 'failure',
        'external_id': f'admin-failure:{run_id}:{attempt}',
        'details_url': f'https://github.com/sdbcbt/CloudLinkPOS-CI/actions/runs/{run_id}',
        'output': {
            'title': '后台编译失败详情',
            'summary': '失败日志仅在本私库可见；已替换凭据，仅保留最多 24KB 末尾。',
            'text': PRIVATE.redact_tail(PRIVATE.read_log()),
        },
    }
    request = urllib.request.Request(PRIVATE.API_URL, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST', headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'cloudlinkpos-admin-failure-report',
    })
    with urllib.request.build_opener(PRIVATE.NoRedirect()).open(request, timeout=30) as response:
        if response.status != 201:
            raise ValueError('私库检查回写失败')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['exists']:
            present = (Path(os.environ['RUNNER_TEMP']) / PRIVATE.LOG_NAME).exists()
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write('exists=' + str(present).lower() + '\n')
        elif len(sys.argv) == 1:
            report()
            print('后台失败详情已写入私库检查结果。')
        else:
            raise ValueError('不支持的报告动作')
    except Exception:
        print('::error::后台失败详情回写未完成，请核对固定提交和检查写入权限。', file=sys.stderr)
        sys.exit(1)
