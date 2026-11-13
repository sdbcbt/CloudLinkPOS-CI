import { strict as assert } from 'node:assert';
import { createHash, randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import { applyPatch, assertPath, decryptAuth, encryptAuth, findTask, inspectPatch, signedHeaders, validateReport, verifySignature, publishDraft, readFixedFile, readCloudList, main } from './codex-cloud-dispatch.mjs';

const job = { item_id: 'item-23', revision: 2, source_sha: 'a'.repeat(40), branch: 'codex/feishu-item-23', environment_id: 'env-test' };
const reportPath = 'docs/06-部署运维/研发助手任务报告.json';
const passed = [{ command: 'node --test', status: 'passed', output: '测试通过' }];
function patchFor(path, value, orig) {
  const before = orig === undefined ? '/dev/null' : `a/${path}`;
  const old = orig === undefined ? '' : `--- ${before}\n`;
  const create = orig === undefined ? 'new file mode 100644\n' : '';
  const header = orig === undefined ? '--- /dev/null\n' : old;
  const oldRange = orig === undefined ? '0,0' : `1,${orig.split('\n').filter(Boolean).length}`;
  const removed = orig === undefined ? '' : orig.split('\n').filter(Boolean).map(line => `-${line}\n`).join('');
  const added = value.split('\n').filter(Boolean).map(line => `+${line}\n`).join('');
  return `diff --git a/${path} b/${path}\n${create}${header}+++ b/${path}\n@@ -${oldRange} +1,${value.split('\n').filter(Boolean).length} @@\n${removed}${added}`;
}

test('中文路径通过真实 git 解析和空目录应用，报告不会进入 PR 文件集', () => {
  const report = JSON.stringify({ item_id: job.item_id, revision: 2, source_sha: job.source_sha, decision: 'fixed', summary: '已修复', evidence: '重现及边界检查', tests: passed });
  const patch = Buffer.from(patchFor('docs/中文指南.md', '第二版\n', '第一版\n') + patchFor(reportPath, report + '\n'));
  const paths = inspectPatch(patch);
  assert.deepEqual(paths, ['docs/中文指南.md', reportPath]);
  const contents = applyPatch(patch, paths, new Map([['docs/中文指南.md', Buffer.from('第一版\n')]]));
  assert.equal(contents.get('docs/中文指南.md').toString(), '第二版\n');
  assert.equal(validateReport(JSON.parse(contents.get(reportPath)), job, true).decision, 'fixed');
  contents.delete(reportPath);
  assert.deepEqual([...contents.keys()], ['docs/中文指南.md']);
});

test('越界、路径 traversal、密钥文件、链接和模式变更拒绝', () => {
  for (const path of ['.github/workflows/a.yml', 'docs/../config.yml', 'apps/.env', 'apps/pos/AGENTS.md', 'docs/密钥.pem', 'apps/test/config-private-key.json', 'docs/foo.lock']) assert.throws(() => assertPath(path));
  const link = 'diff --git a/docs/link.md b/docs/link.md\nnew file mode 120000\n--- /dev/null\n+++ b/docs/link.md\n@@ -0,0 +1 @@\n+target\n';
  assert.throws(() => inspectPatch(link));
  const rename = 'diff --git a/docs/a.md b/docs/b.md\nsimilarity index 100%\nrename from docs/a.md\nrename to docs/b.md\n';
  assert.throws(() => inspectPatch(rename));
  assert.throws(() => inspectPatch(patchFor('docs/x.md', 'ok\n').repeat(25)));
  assert.throws(() => inspectPatch('x'.repeat(1024 * 1024 + 1)));
});

test('单文件非 fixed 报告允许安全新增，不包含源码修改', () => {
  const report = JSON.stringify({ item_id: job.item_id, revision: 2, source_sha: job.source_sha, decision: 'no_issue', summary: '没有问题', evidence: '复核正常', tests: [] });
  const patch = Buffer.from(patchFor(reportPath, report + '\n'));
  const paths = inspectPatch(patch);
  assert.deepEqual(paths, [reportPath]);
  const contents = applyPatch(patch, paths, new Map());
  assert.equal(validateReport(JSON.parse(contents.get(reportPath)), job, false).decision, 'no_issue');
});

test('固定提交的 GitHub 文件必须验证 blob SHA，错误摘要拒绝', async () => {
  const bytes = Buffer.from('固定文件内容\n');
  const hash = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
  const payload = { type: 'file', encoding: 'base64', size: bytes.length, content: bytes.toString('base64'), sha: hash };
  const fetcher = async url => ({ ok: true, text: async () => JSON.stringify(url.includes('/git/commits/') ? { tree: { sha: 'b'.repeat(40) } } : url.includes('/git/trees/') ? { tree: [{ path: url.endsWith('/' + 'b'.repeat(40)) ? 'docs' : 'test.md', sha: url.endsWith('/' + 'b'.repeat(40)) ? 'c'.repeat(40) : hash, mode: '100644', type: 'blob' }] } : payload) });
  assert.deepEqual((await readFixedFile('private-token', job.source_sha, 'docs/test.md', fetcher)).bytes, bytes);
  const tampered = async url => url.includes('/contents/') ? { ok: true, text: async () => JSON.stringify({ ...payload, content: Buffer.from('篡改').toString('base64') }) } : fetcher(url);
  await assert.rejects(readFixedFile('private-token', job.source_sha, 'docs/test.md', tampered), /长度不符|摘要不符/);
});

test('no_issue 与 need_info 只有无源码改动和完整证据才有效', () => {
  for (const decision of ['no_issue', 'need_info', 'non_code']) {
    const report = { item_id: job.item_id, revision: 2, source_sha: job.source_sha, decision, summary: '结论', evidence: '实际证据', tests: [] };
    assert.equal(validateReport(report, job, false), report);
    assert.throws(() => validateReport(report, job, true));
  }
  assert.throws(() => validateReport({ item_id: job.item_id, revision: 2, source_sha: job.source_sha, decision: 'fixed', summary: '修复', evidence: '', tests: [] }, job, true));
});

test('签名绑定 body、nonce、时间戳；不同 body 和过期均拒绝', () => {
  const secret = randomBytes(32).toString('hex');
  const now = Date.now();
  const body = JSON.stringify({ job_id: 'opaque-id' });
  const headers = signedHeaders(secret, body, now);
  assert.equal(verifySignature(secret, body, headers, now), true);
  assert.equal(verifySignature(secret, body + ' ', headers, now), false);
  assert.equal(verifySignature(secret, body, headers, now + 121000), false);
});

test('登录态 AES-GCM 有认证标签，篡改或错误密钥拒绝', () => {
  const key = randomBytes(32);
  const value = Buffer.from('{"tokens": "private"}');
  const encrypted = encryptAuth(value, key);
  assert.deepEqual(decryptAuth({ version: 3, ...encrypted }, key), value);
  assert.throws(() => decryptAuth({ version: 3, ...encrypted }, randomBytes(32)));
  const edited = Buffer.from(encrypted.ciphertext, 'base64'); edited[0] ^= 1;
  assert.throws(() => decryptAuth({ version: 3, iv: encrypted.iv, ciphertext: edited.toString('base64') }, key));
});

test('ready 仅表示任务状态，不是通过测试；云端任务必须匹配环境', () => {
  const task = { id: 'task_12345', environment_id: 'env-test', status: 'ready', summary: { files_changed: 1 } };
  assert.equal(findTask({ tasks: [task] }, task.id, 'env-test'), task);
  assert.equal(findTask({ tasks: [{ ...task, environment_id: null }] }, task.id, 'env-test').status, 'ready');
  assert.throws(() => findTask({ tasks: [{ ...task, environment_id: 'wrong-env' }] }, task.id, 'env-test'));
});

test('PR 已关闭不重开，固定分支基线不符不发布', async () => {
  const calls = [];
  const fetcher = async (url, opts) => {
    calls.push([url, opts.method]);
    if (url.endsWith('/git/ref/heads/codex/feishu-item-23')) return { ok: true, text: async () => JSON.stringify({ object: { sha: job.source_sha } }) };
    if (url.includes('/pulls?')) return { ok: true, text: async () => JSON.stringify([{ state: 'closed', number: 7, html_url: 'https://github.com/sdbcbt/CloudLinkPOS/pull/7', head: { sha: job.source_sha, ref: job.branch }, base: { ref: 'main' } }]) };
    throw Error('意外调用');
  };
  await assert.rejects(() => publishDraft(job, 'token-test-value-1234567890', new Map([['docs/x.md', Buffer.from('test')]]), { decision: 'fixed' }, fetcher), /已关闭/);
  assert.equal(calls.length, 1);
});

test('cloud list 使用实际 cursor 翻页并接受缺省环境字段', () => {
  const args = [];
  const task = { id: 'task_12345', status: 'ready', environment_id: null };
  const run = (_program, params) => {
    args.push(params);
    return Buffer.from(JSON.stringify(args.length === 1 ? { tasks: [], cursor: 'page-2' } : { tasks: [task], cursor: null }));
  };
  assert.deepEqual(readCloudList(task.id, 'env-test', run), task);
  assert.deepEqual(args[1].slice(-2), ['--cursor', 'page-2']);
});

test('报告拒绝错误身份、缺失测试和失败项；真实 455 字命令可记录', () => {
  const result = { item_id: job.item_id, revision: job.revision, source_sha: job.source_sha, decision: 'fixed', summary: '完成', evidence: '真实依据', tests: [{ command: 'a'.repeat(455), status: 'passed', output: '通过' }] };
  assert.equal(validateReport(result, job, true), result);
  assert.throws(() => validateReport({ ...result, source_sha: 'b'.repeat(40) }, job, true));
  assert.throws(() => validateReport({ ...result, tests: [] }, job, true));
  assert.throws(() => validateReport({ ...result, tests: [{ command: 'test', status: 'failed', output: '失败' }] }, job, true));
});

test('已存在草稿 PR 优先恢复，核对候选源码且不再次写入', async () => {
  const content = Buffer.from('修复\n');
  const hash = createHash('sha1').update(`blob ${content.length}\0`).update(content).digest('hex');
  const nextSha = 'b'.repeat(40);
  const calls = [];
  const fetcher = async (url, opts) => {
    calls.push([url, opts.method]);
    const path = new URL(url).pathname;
    let data;
    if (path.endsWith('/pulls')) data = [{ state: 'open', draft: true, number: 7, html_url: 'https://github.com/sdbcbt/CloudLinkPOS/pull/7', head: { sha: nextSha, ref: job.branch }, base: { ref: 'main' } }];
    else if (path.endsWith(`/git/ref/heads/${job.branch}`)) data = { object: { sha: nextSha } };
    else if (path.endsWith(`/git/commits/${nextSha}`)) data = { parents: [{ sha: job.source_sha }] };
    else if (path.includes('/compare/')) data = { files: [{ filename: 'docs/fix.md', status: 'added', sha: hash }] };
    else throw Error(`意外访问 ${path}`);
    return { ok: true, text: async () => JSON.stringify(data) };
  };
  const receipt = await publishDraft(job, 'private-token', new Map([['docs/fix.md', content]]), { decision: 'fixed', summary: '修复', evidence: '依据', tests: passed }, fetcher);
  assert.equal(receipt.existing, true);
  assert.equal(receipt.commit_sha, nextSha);
  assert.equal(calls.every(([, method]) => method === 'GET'), true);
});

test('PR 描述、commit 和检查记录发布前统一脱敏', async () => {
  const content = Buffer.from('修复\n');
  const blob = createHash('sha1').update(`blob ${content.length}\0`).update(content).digest('hex');
  const nextSha = 'b'.repeat(40);
  const posts = [];
  const fetcher = async (url, opts) => {
    const path = new URL(url).pathname;
    if (opts.method === 'POST') posts.push({ path, body: JSON.parse(opts.body) });
    let data;
    if (path.endsWith('/pulls') && opts.method === 'GET') data = [];
    else if (path.endsWith(`/git/ref/heads/${job.branch}`)) data = { object: { sha: job.source_sha } };
    else if (path.endsWith(`/git/commits/${job.source_sha}`)) data = { tree: { sha: 'c'.repeat(40) } };
    else if (path.endsWith('/git/blobs')) data = { sha: blob };
    else if (path.endsWith('/git/trees')) data = { sha: 'd'.repeat(40) };
    else if (path.endsWith('/git/commits') && opts.method === 'POST') data = { sha: nextSha };
    else if (path.includes('/compare/')) data = { files: [{ filename: 'docs/fix.md', status: 'added', sha: blob }] };
    else if (path.endsWith('/pulls') && opts.method === 'POST') data = { number: 9, state: 'open', draft: true, html_url: 'https://github.com/sdbcbt/CloudLinkPOS/pull/9' };
    else data = { object: { sha: nextSha } };
    return { ok: true, text: async () => JSON.stringify(data) };
  };
  const secret = 'sk-abcdefghijklmnop';
  const report = { decision: 'fixed', summary: `修复 ${secret}`, evidence: `依据 ${secret}`, tests: [{ command: `test ${secret}`, status: 'passed', output: `通过 ${secret}` }] };
  const candidate = { ...job, source_sha: job.source_sha };
  const branchSha = { value: job.source_sha };
  const wrapped = async (url, opts) => {
    if (url.endsWith(`/git/ref/heads/${job.branch}`)) return { ok: true, text: async () => JSON.stringify({ object: { sha: branchSha.value } }) };
    const response = await fetcher(url, opts);
    if (opts.method === 'PATCH') branchSha.value = nextSha;
    return response;
  };
  await publishDraft(candidate, 'private-token', new Map([['docs/fix.md', content]]), report, wrapped);
  const serialized = JSON.stringify(posts);
  assert.ok(!serialized.includes(secret));
  assert.match(serialized, /已脱敏令牌/);
});

test('claim 后解密失败回报受保护原因，不泄露登录密钥且不留临时文件', async () => {
  const before = readdirSync(tmpdir()).filter(name => name.startsWith('clp-codex-'));
  const previous = { CLOUD_JOB_ID: process.env.CLOUD_JOB_ID, GITHUB_RUN_ID: process.env.GITHUB_RUN_ID, CLOUD_BROKER_URL: process.env.CLOUD_BROKER_URL, CLOUD_BROKER_SECRET: process.env.CLOUD_BROKER_SECRET, CODEX_LOGIN_KEY: process.env.CODEX_LOGIN_KEY };
  const old = globalThis.fetch;
  const phases = [];
  const id = '00000000-0000-4000-8000-000000000001';
  Object.assign(process.env, { CLOUD_JOB_ID: id, GITHUB_RUN_ID: '123', CLOUD_BROKER_URL: 'https://broker.test', CLOUD_BROKER_SECRET: 'synthetic-broker-secret-minimum-32-characters', CODEX_LOGIN_KEY: randomBytes(32).toString('base64') });
  globalThis.fetch = async (_url, request) => {
    const body = JSON.parse(request.body);
    if (_url.endsWith('/cloud/report')) { phases.push(body); return Response.json({ status: 'reconcile' }); }
    return Response.json({ job: { id, source_sha: job.source_sha, branch: job.branch, environment_id: job.environment_id, operation: 'submit', query: '测试任务', revision: 1 }, lease_id: '00000000-0000-4000-8000-000000000002', private_token: 'synthetic-token-12345678901234567890', auth: { version: 1, iv: Buffer.alloc(12).toString('base64'), ciphertext: Buffer.alloc(32).toString('base64') } });
  };
  try {
    await assert.rejects(main(), /已停止自动重提/);
    assert.equal(phases.length, 1);
    assert.equal(phases[0].phase, 'reconcile');
    assert.match(phases[0].reason, /解密|登录态|认证/);
    assert.deepEqual(readdirSync(tmpdir()).filter(name => name.startsWith('clp-codex-')), before);
  } finally {
    globalThis.fetch = old;
    for (const [name, value] of Object.entries(previous)) { if (value === undefined) delete process.env[name]; else process.env[name] = value; }
  }
});

test('公开 workflow 仅传 job_id、固定主分支、固定 App、无私库签发秘密', () => {
  const workflow = readFileSync(new URL('../.github/workflows/codex-cloud-dispatch.yml', import.meta.url), 'utf8');
  assert.match(workflow, /job_id:/);
  assert.match(workflow, /cloudlinkpos-cloud-dispatch\[bot\]/);
  assert.match(workflow, /refs\/heads\/main/);
  assert.match(workflow, /@openai\/codex@0\.163\.0/);
  assert.doesNotMatch(workflow, /SOURCE_TOKEN|CI_APP_PRIVATE_KEY|query:|source_sha:|artifact|upload-artifact/i);
});
