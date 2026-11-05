"""只将已核验的版本元数据保存到公开收据分支，不读取或发布私有源码。"""
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

REPO = 'sdbcbt/CloudLinkPOS-CI'
BRANCH = 'source-build-receipts'
IMAGE = 'registry.cn-zhangjiakou.aliyuncs.com/sdbcbt/cloudlinkpos'


def api(method, path, value=None):
    body = None if value is None else json.dumps(value).encode()
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + path, data=body, method=method, headers={'Authorization': 'Bearer ' + os.environ['RECEIPT_TOKEN'], 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'CloudLinkPOS-CI'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == 'GET':
            return None
        raise RuntimeError('公开收据仓库请求失败，HTTP ' + str(error.code)) from None


def build_batch(folder, env):
    plans = list(folder.rglob('source-build-plan.json'))
    if len(plans) != 1:
        raise ValueError('缺少唯一的构建计划')
    plan = json.loads(plans[0].read_text())
    names = plan['expectedServices']
    policy = plan['publicationPolicy']
    if plan['schema'] != 'cloudlinkpos.public-source-plan/v1' or not re.fullmatch('[0-9a-f]{40}', plan['sourceSHA']) or not re.fullmatch('[0-9a-f]{40}', plan['beforeSHA']) or len(names) != len(set(names)) or not all(re.fullmatch('[a-z][a-z0-9-]*', name) for name in names):
        raise ValueError('构建计划来源或服务范围无效')
    if type(policy.get('grayRequired')) is not bool or type(policy.get('generation')) is not int or policy['generation'] < 1:
        raise ValueError('发布策略未绑定')
    receipts = {}
    for path in folder.rglob('*.json'):
        if path == plans[0]:
            continue
        item = json.loads(path.read_text())
        name = item['service']
        if name not in names or name in receipts or item['schema'] != 'cloudlinkpos.public-full-build/v1' or item['sourceRepository'] != 'sdbcbt/CloudLinkPOS' or item['buildRepository'] != REPO or item['sourceSHA'] != plan['sourceSHA'] or item['workflowSHA'] != env['GITHUB_SHA'] or item['runID'] != env['GITHUB_RUN_ID'] or item['attempt'] != env['GITHUB_RUN_ATTEMPT']:
            raise ValueError('收据与本次构建不一致')
        evidence = item['privateEvidence']
        if item['repository'] != IMAGE or not re.fullmatch(re.escape(name) + r'-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', item['tag']) or not re.fullmatch('sha256:[0-9a-f]{64}', item['digest']) or item['tagPolicy'] != 'version-and-latest-v1' or evidence.get('authenticated_read') is not True or evidence.get('anonymous_pull_denied') is not True or evidence.get('image_digest') != item['digest']:
            raise ValueError('镜像版本或私有访问证据无效')
        # 明确白名单，避免构建工具新增字段时泄漏源码或凭据。
        receipts[name] = {key: item[key] for key in ('schema', 'sourceRepository', 'sourceSHA', 'buildRepository', 'workflowSHA', 'runID', 'attempt', 'service', 'repository', 'tag', 'digest', 'tagPolicy', 'metadataSHA256')}
        receipts[name]['privateEvidence'] = {key: evidence[key] for key in ('authenticated_read', 'anonymous_pull_denied', 'image_digest')}
    if set(receipts) != set(names):
        raise ValueError('选定服务的成功收据未齐')
    return dict(sourceSHA=plan['sourceSHA'], beforeSHA=plan['beforeSHA'], expectedServices=names, publicationPolicy={'grayRequired': policy['grayRequired'], 'generation': policy['generation']}, schema='cloudlinkpos.public-source-build/v1', sourceRepository='sdbcbt/CloudLinkPOS', buildRepository=REPO, runID=env['GITHUB_RUN_ID'], attempt=env['GITHUB_RUN_ATTEMPT'], workflowSHA=env['GITHUB_SHA'], workflowPath='.github/workflows/private-main.yml', receipts=[receipts[name] for name in sorted(receipts)])


def main():
    if os.environ['GITHUB_REPOSITORY'] != REPO or os.environ['GITHUB_REF'] != 'refs/heads/main':
        raise ValueError('只允许受管主线保存收据')
    batch = build_batch(Path('input'), os.environ)
    path = 'builds/' + batch['sourceSHA'] + '/' + batch['runID'] + '-' + batch['attempt'] + '.json'
    reference = api('GET', '/git/ref/heads/' + BRANCH)
    parent = reference['object']['sha'] if reference else None
    index = []
    if parent:
        old = api('GET', '/contents/index.json?ref=' + parent)
        if old:
            index = json.loads(base64.b64decode(old['content']))
    record = {key: batch[key] for key in ('sourceSHA', 'beforeSHA', 'runID', 'attempt', 'workflowSHA', 'publicationPolicy')}
    record['path'] = path
    index = [record] + [item for item in index if item['path'] != path]
    catalog = []
    if parent:
        previous = api('GET', '/contents/catalog.json?ref=' + parent)
        if previous:
            catalog = json.loads(base64.b64decode(previous['content']))
    summary = {key: value for key, value in batch.items() if key != 'receipts'}
    summary['path'] = path
    new_entries = [dict(summary, receipts=[receipt]) for receipt in batch['receipts']]
    counts, selected = {}, []
    for entry in new_entries + catalog:
        name = entry['receipts'][0]['service']
        identity = (name, entry['runID'], entry['attempt'])
        if counts.get(name, 0) < 2 and not any((value['receipts'][0]['service'], value['runID'], value['attempt']) == identity for value in selected):
            selected.append(entry)
            counts[name] = counts.get(name, 0) + 1
    files = {'catalog.json': selected, path: batch, 'sources/' + batch['sourceSHA'] + '.json': batch, 'index.json': index[:1000]}
    tree = {'tree': [{'path': name, 'mode': '100644', 'type': 'blob', 'content': json.dumps(value, ensure_ascii=False, indent=2) + '\n'} for name, value in files.items()]}
    if parent:
        tree['base_tree'] = api('GET', '/git/commits/' + parent)['tree']['sha']
    sha = api('POST', '/git/trees', tree)['sha']
    commit = api('POST', '/git/commits', {'message': '保存主线构建版本收据\n\n记录固定源码、服务版本与镜像摘要，供生产发布服务核对实际工作流结果后使用。', 'tree': sha, 'parents': [parent] if parent else []})['sha']
    if parent:
        api('PATCH', '/git/refs/heads/' + BRANCH, {'sha': commit, 'force': False})
    else:
        api('POST', '/git/refs', {'ref': 'refs/heads/' + BRANCH, 'sha': commit})
    print('已保存版本元数据；生产仍须核验工作流完成结果及当前发布策略。')


if __name__ == '__main__':
    main()
