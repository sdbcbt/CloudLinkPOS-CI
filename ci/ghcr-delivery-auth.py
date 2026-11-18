"""读取受信 GitHub Actions 的短期拉取身份。"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

spec = importlib.util.spec_from_file_location('ghcr_publish', Path(__file__).with_name('ghcr-publish.py'))
GHCR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GHCR)


def pull_token():
    """取得当前 GitHub Actions 任务的短期拉取身份。"""
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.actions.githubusercontent.com') or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('OIDC_ENDPOINT_INVALID')
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if key != 'audience']
    query.append(('audience', 'cloudlinkpos-registry'))
    url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    headers = {'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'], 'User-Agent': 'cloudlinkpos-registry/1.0'}
    try:
        with urllib.request.build_opener(GHCR.NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            token = json.loads(response.read(32000))['value']
    except urllib.error.HTTPError as error:
        raise ValueError('OIDC_HTTP_' + str(error.code)) from None
    if not isinstance(token, str) or not 20 < len(token) < 16000:
        raise ValueError('OIDC_TOKEN_INVALID')
    # 本地仅预检固定声明；可信签名及 JWKS 验证由 Worker 完成。
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        expected = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'cloudlinkpos-registry', 'repository': 'sdbcbt/CloudLinkPOS-CI', 'repository_id': '1446734771', 'repository_owner_id': '286992896', 'ref': 'refs/heads/main', 'event_name': 'workflow_dispatch', 'runner_environment': 'github-hosted', 'actor': 'cloudlinkpos-ci-notifier[bot]'}
        workflow = claims.get('workflow_ref')
        callers = ['sdbcbt/CloudLinkPOS-CI/.github/workflows/' + name + '@refs/heads/main' for name in ('private-main.yml', 'full-version-build.yml')]
        if any(claims.get(key) != value for key, value in expected.items()) or workflow not in callers or claims.get('sub') not in ('repo:sdbcbt/CloudLinkPOS-CI:ref:refs/heads/main', 'repo:sdbcbt@286992896/CloudLinkPOS-CI@1446734771:ref:refs/heads/main'):
            raise ValueError('OIDC_CLAIMS_INVALID')
        if claims.get('job_workflow_ref', callers[1]) != callers[1]:
            raise ValueError('OIDC_WORKFLOW_INVALID')
    except (ValueError, IndexError, KeyError):
        raise ValueError('OIDC_CLAIMS_INVALID') from None
    return token
