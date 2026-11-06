"""将收银构建失败详情脱敏后仅写入私库固定提交的检查结果。"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import urllib.request

LOG_NAME = 'cloudlinkpos-pos-first-failure.log'
API_URL = 'https://api.github.com/repos/sdbcbt/CloudLinkPOS/check-runs'
MAX_TEXT_BYTES = 24000
CHECKS = {
    'windows': ('pos-windows-build-failure', 'Windows 收银构建失败详情'),
    'android': ('pos-android-build-failure', 'Android 收银构建失败详情'),
}
SECRET_KEYS = (
    'ANDROID_KEYSTORE_BASE64', 'ANDROID_KEYSTORE_PASSWORD', 'ANDROID_KEY_PASSWORD', 'ANDROID_KEY_ALIAS',
    'CHECKS_TOKEN', 'SOURCE_TOKEN', 'GITHUB_TOKEN',
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def validate_target():
    if os.environ.get('GITHUB_REPOSITORY') != 'sdbcbt/CloudLinkPOS-CI' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('仅允许公开 CI 主线回写失败详情')
    sha = os.environ.get('SOURCE_SHA', '')
    platform = os.environ.get('RELEASE_PLATFORM', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch('[0-9a-f]{40}', sha) or platform not in CHECKS:
        raise ValueError('固定源码或平台无效')
    if not all(re.fullmatch('[1-9][0-9]*', value) for value in (run_id, attempt)):
        raise ValueError('工作流运行标识无效')
    plan = json.loads(os.environ['POS_RELEASE_PLAN'])
    if plan.get('repository') != 'sdbcbt/CloudLinkPOS' or plan.get('sourceSHA') != sha:
        raise ValueError('源码与可信计划不一致')
    # 只读取已检出的 Git 元数据，不导入或执行带写令牌的私库代码。
    root = Path('source').resolve()
    def git(*args):
        result = subprocess.run(['git', *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise ValueError('固定源码核验失败')
        return result.stdout.decode('ascii').splitlines()
    if git('rev-parse', 'HEAD') != [sha] or sha not in git('rev-list', '--first-parent', 'FETCH_HEAD'):
        raise ValueError('固定源码不属于已核验主线')
    return sha, platform, run_id, attempt


def read_log():
    path = Path(os.environ['RUNNER_TEMP']) / LOG_NAME
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
        raise ValueError('临时日志不允许链接')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('临时日志文件无效')
        if os.name != 'nt' and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise ValueError('临时日志权限无效')
        return stream.read()


def redact_tail(content):
    text = content.decode('utf-8', errors='replace')
    secrets = sorted({os.environ[key] for key in SECRET_KEYS if os.environ.get(key)}, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, '[REDACTED]')
    # 先脱敏再截尾，避免切开秘密；阻断日志闭合代码围栏后渲染外链。
    text = text.replace('```', '``\u200b`')
    tail = text.encode('utf-8')[-(MAX_TEXT_BYTES - 8):].decode('utf-8', errors='ignore')
    return '```\n' + tail + '\n```'


def report():
    sha, platform, run_id, attempt = validate_target()
    token = os.environ['CHECKS_TOKEN']
    if not token:
        raise ValueError('缺少检查写入令牌')
    name, title = CHECKS[platform]
    body = {
        'name': name,
        'head_sha': sha,
        'status': 'completed',
        'conclusion': 'failure',
        'external_id': f'pos-failure:{run_id}:{attempt}:{platform}',
        'details_url': f'https://github.com/sdbcbt/CloudLinkPOS-CI/actions/runs/{run_id}',
        'output': {
            'title': title,
            'summary': '失败日志仅在本私库可见；已替换签名秘密和令牌，仅保留最多 24KB 末尾。',
            'text': redact_tail(read_log()),
        },
    }
    request = urllib.request.Request(API_URL, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST', headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'cloudlinkpos-pos-failure-report',
    })
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        if response.status != 201:
            raise ValueError('私库检查回写失败')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['exists']:
            present = (Path(os.environ['RUNNER_TEMP']) / LOG_NAME).exists()
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write('exists=' + str(present).lower() + '\n')
        elif len(sys.argv) == 1:
            report()
            print('失败详情已写入私库检查结果。')
        else:
            raise ValueError('不支持的报告动作')
    except Exception:
        # 禁止输出异常、请求、响应、日志正文和任何凭据。
        print('::error::私库失败详情回写未完成，请核对固定提交和检查写入权限。', file=sys.stderr)
        sys.exit(1)
