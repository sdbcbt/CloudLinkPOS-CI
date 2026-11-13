// 公开 Runner 仅处理不透明 job_id；消息、源码与 CLI 原始输出始终留在受保护内存和临时目录。
import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const PRIVATE_REPO = 'sdbcbt/CloudLinkPOS';
const REPORT_PATH = 'docs/06-部署运维/研发助手任务报告.json';
const MAX_PATCH = 1024 * 1024;
const MAX_FILES = 24;
const MAX_AUTH = 100_000;
const SHA = /^[0-9a-f]{40}$/;
const JOB_ID = /^[a-zA-Z0-9_-]{8,128}$/;
const ITEM_ID = /^[a-zA-Z0-9_-]{1,128}$/;
const TASK_ID = /^[a-zA-Z0-9_-]{6,128}$/;
const BRANCH = /^codex\/feishu-[a-zA-Z0-9_-]{1,90}$/;
const SAFE_SEGMENT = /^[\p{L}\p{N}._+-]+$/u;
const REJECT_NAME = /(?:^|[./_-])(secret|credential|password|token|private.?key|config(?:uration)?|settings|application|values|env|\.env|\.npmrc|\.pypirc)(?:[./_-]|$)/i;
const REJECT_EXT = /\.(?:pem|key|p12|pfx|der|jks|keystore|env|lock)$/i;
const REJECT_CHARACTER = /[\r\n\t\x00-\x1f\x7f`$"'<>|;:&?*~]/;
const VALID_DECISION = new Set(['fixed', 'no_issue', 'need_info', 'non_code']);
const SAFE_STATUSES = new Set(['ready', 'pending', 'running', 'in_progress', 'queued', 'failed', 'cancelled', 'canceled']);
function redact(text) {
  return text.replace(/-----BEGIN [\s\S]*?-----END [\w ]+-----/g, '[已脱敏私钥]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+)\b/gi, '[已脱敏令牌]')
    .replace(/\b(?:password|secret|api[_-]?key|token|密钥|密码)\s*[:=：]\s*[^\s,，;；]+/gi, '[已脱敏凭据]')
    .replace(/\b\d{17}[0-9Xx]\b/g, '[已脱敏证件号]');
}

export function assertPath(path, report = false) {
  if (report && path === REPORT_PATH) return path;
  if (typeof path !== 'string' || path.length > 240 || REJECT_CHARACTER.test(path) || path.includes('\\') || path.startsWith('/') || path.includes('//')) throw Error('路径无效');
  const segments = path.split('/');
  if (!['apps', 'docs', 'contracts'].includes(segments[0]) || segments.length < 2 || segments.some(s => !s || s.startsWith('.') || s.endsWith('.') || !SAFE_SEGMENT.test(s) || REJECT_NAME.test(s) || ['AGENTS.md', 'CLAUDE.md', 'CODEGEN.md', 'MEMORY.md'].includes(s))) throw Error('路径不在安全范围');
  if (/(^|\/)(?:etc|config)\/[^/]+\.(?:yaml|yml|json|toml)$/i.test(path) || REJECT_NAME.test(path) || REJECT_EXT.test(path) || path.includes('/.github/') || path.includes('/.git/') || path.includes('/node_modules/')) throw Error('配置或凭据文件不允许');
  if (!/\.(?:go|dart|vue|ts|tsx|js|mjs|json|md|mdx|yaml|yml|proto|api|css|scss|html|sql|txt|svg)$/i.test(path)) throw Error('文件类型不在安全范围');
  return path;
}

function command(program, args, options = {}) {
  return execFileSync(program, args, { encoding: 'buffer', stdio: ['pipe', 'pipe', 'pipe'], maxBuffer: MAX_PATCH + 65536, timeout: 60_000, ...options });
}

// git apply 自身输出 -z 路径；不依赖 git C-quote 的文本正则（中文、空格与引号路径）。
export function inspectPatch(patch, run = command) {
  const bytes = Buffer.isBuffer(patch) ? patch : Buffer.from(patch);
  if (!bytes.length || bytes.length > MAX_PATCH || bytes.includes(0)) throw Error('补丁大小或内容无效');
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  if (/(?:^|\n)(?:old mode|new mode|deleted file mode|rename from|rename to|copy from|copy to|GIT binary patch|Binary files |Submodule |index [0-9a-f]+\.\.[0-9a-f]+ 120000)(?: |\n)/m.test(text)) throw Error('补丁包含不支持的变更类型');
  const raw = run('git', ['apply', '--numstat', '-z', '-'], { input: bytes });
  const records = raw.toString('utf8').split('\0');
  if (records.at(-1) !== '') throw Error('git numstat 不完整');
  records.pop();
  if (!records.length || records.length > MAX_FILES + 1) throw Error('文件数量不允许');
  const paths = records.map(record => {
    const match = /^(\d+)\t(\d+)\t(.+)$/su.exec(record);
    if (!match) throw Error('git numstat 格式无效');
    return match[3];
  });
  if (new Set(paths).size !== paths.length || paths.filter(p => p !== REPORT_PATH).length > MAX_FILES) throw Error('重复或过多文件');
  paths.forEach(p => assertPath(p, true));
  const summary = run('git', ['apply', '--summary', '-'], { input: bytes }).toString('utf8');
  for (const line of summary.split('\n')) {
    if (!line) continue;
    // 只允许普通文本新增文件；任何 rename/delete/mode/symlink 都拒绝。
    if (!/^ create mode 100644 /.test(line)) throw Error('补丁含删除、重命名、链接或模式修改');
  }
  // 真正的 diff 解析交给 git；中文路径可以是 C-quote，严禁自己拆 header。
  return paths;
}

export function applyPatch(patch, paths, originals, run = command) {
  const dir = mkdtempSync(join(tmpdir(), 'clp-cloud-'));
  try {
    for (const path of paths) {
      const bytes = originals.get(path);
      if (bytes === undefined) continue;
      assertText(bytes);
      const file = join(dir, path);
      mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
      writeFileSync(file, bytes, { mode: 0o600, flag: 'wx' });
    }
    run('git', ['apply', '--check', '-'], { cwd: dir, input: patch });
    run('git', ['apply', '-'], { cwd: dir, input: patch });
    const contents = new Map();
    for (const path of paths) {
      const file = join(dir, path);
      const st = statSync(file);
      if (!st.isFile() || st.size > MAX_PATCH) throw Error('补丁结果不是普通文本');
      const bytes = readFileSync(file);
      assertText(bytes);
      contents.set(path, bytes);
    }
    // 即使路径解析出现意外，应用结果也只能包含许可文件。
    const seen = [];
    function walk(base, prefix = '') {
      for (const entry of readdirSync(base, { withFileTypes: true })) {
        const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
        if (entry.isDirectory()) walk(join(base, entry.name), relative);
        else if (entry.isFile()) seen.push(relative);
        else throw Error('补丁结果含链接');
      }
    }
    walk(dir);
    if (seen.length !== paths.length || seen.some(p => !paths.includes(p))) throw Error('补丁操作路径不一致');
    return contents;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function assertText(bytes) {
  if (bytes.includes(0) || bytes.length > MAX_PATCH) throw Error('非文本文件');
  new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

export function validateReport(value, job, changed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('任务报告无效');
  if (value.item_id !== job.item_id || value.revision !== job.revision || value.source_sha !== job.source_sha || !VALID_DECISION.has(value.decision)) throw Error('任务报告身份或结论无效');
  if (typeof value.evidence !== 'string' || !value.evidence.trim() || value.evidence.length > 3000 || typeof value.summary !== 'string' || !value.summary.trim() || value.summary.length > 1000) throw Error('任务依据不完整');
  if (!Array.isArray(value.tests) || value.tests.length > 20 || value.tests.some(t => !t || typeof t.command !== 'string' || !t.command.trim() || t.command.length > 600 || !['passed', 'failed', 'blocked'].includes(t.status) || typeof t.output !== 'string' || !t.output.trim() || t.output.length > 600)) throw Error('任务检查记录无效');
  if (value.decision === 'fixed' && (!value.tests.length || value.tests.some(t => t.status !== 'passed'))) throw Error('修复的检查未全部通过');
  if ((value.decision === 'fixed') !== changed) throw Error('结论与源码变更不符');
  return value;
}

export function signedHeaders(secret, body, now = Date.now(), nonce = randomBytes(16).toString('hex')) {
  const timestamp = String(Math.floor(now / 1000));
  const signature = createHmac('sha256', secret).update(`${timestamp}.${nonce}.${body}`).digest('hex');
  return { 'content-type': 'application/json', 'x-broker-timestamp': timestamp, 'x-broker-nonce': nonce, 'x-broker-signature': signature };
}

export function verifySignature(secret, body, headers, now = Date.now()) {
  if (typeof body !== 'string' || Buffer.byteLength(body) > 40_000 || !/^\d{10}$/.test(headers['x-broker-timestamp'] || '') || !/^[a-f0-9]{32}$/.test(headers['x-broker-nonce'] || '') || !/^[a-f0-9]{64}$/.test(headers['x-broker-signature'] || '') || Math.abs(now / 1000 - Number(headers['x-broker-timestamp'])) > 120) return false;
  const expected = signedHeaders(secret, body, Number(headers['x-broker-timestamp']) * 1000, headers['x-broker-nonce'])['x-broker-signature'];
  return timingSafeEqual(Buffer.from(expected, 'hex'), Buffer.from(headers['x-broker-signature'], 'hex'));
}

export function decryptAuth(auth, key) {
  if (!Number.isSafeInteger(auth?.version) || auth.version < 1 || !/^[a-zA-Z0-9+/=]+$/.test(auth.iv || '') || !/^[a-zA-Z0-9+/=]+$/.test(auth.ciphertext || '')) throw Error('登录态格式无效');
  const iv = Buffer.from(auth.iv, 'base64');
  const value = Buffer.from(auth.ciphertext, 'base64');
  if (iv.length !== 12 || value.length < 17 || value.length > MAX_AUTH) throw Error('登录态长度无效');
  const decoder = createDecipheriv('aes-256-gcm', key, iv);
  decoder.setAuthTag(value.subarray(-16));
  const data = Buffer.concat([decoder.update(value.subarray(0, -16)), decoder.final()]);
  if (data.length > MAX_AUTH) throw Error('登录态过大');
  JSON.parse(data.toString('utf8'));
  return data;
}

export function encryptAuth(value, key) {
  if (!Buffer.isBuffer(value) || value.length > MAX_AUTH) throw Error('登录态大小无效');
  JSON.parse(value.toString('utf8'));
  const iv = randomBytes(12);
  const encoder = createCipheriv('aes-256-gcm', key, iv);
  const ciphertext = Buffer.concat([encoder.update(value), encoder.final(), encoder.getAuthTag()]);
  return { iv: iv.toString('base64'), ciphertext: ciphertext.toString('base64') };
}

function github(token, method, path, body, fetcher = fetch) {
  if (typeof token !== 'string' || !token || !path.startsWith(`/repos/${PRIVATE_REPO}/`)) throw Error('GitHub 请求无效');
  return fetcher(`https://api.github.com${path}`, { method, redirect: 'error', headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', 'x-github-api-version': '2022-11-28', ...(body ? { 'content-type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
}

async function jsonResponse(response, limit = 1_000_000) {
  const text = await response.text();
  if (Buffer.byteLength(text) > limit) throw Error('远端响应过大');
  if (!response.ok) {
    let detail = text;
    try { const value = JSON.parse(text); detail = value.error?.message || value.message || value.msg || text; } catch { /* 可返回纯文本错误。 */ }
    throw Error(redact(`远端状态 ${response.status}：${String(detail).slice(0, 500)}`));
  }
  return JSON.parse(text);
}

export async function readFixedFile(token, sha, path, fetcher = fetch) {
  if (!SHA.test(sha)) throw Error('基线提交无效');
  assertPath(path, true);
  const response = await github(token, 'GET', `/repos/${PRIVATE_REPO}/contents/${path.split('/').map(encodeURIComponent).join('/')}?ref=${sha}`, undefined, fetcher);
  if (response.status === 404) return undefined;
  const data = await jsonResponse(response, 1_500_000);
  const commit = await jsonResponse(await github(token, 'GET', `/repos/${PRIVATE_REPO}/git/commits/${sha}`, undefined, fetcher));
  let treeSha = commit.tree?.sha;
  if (!SHA.test(treeSha || '')) throw Error('基线树无效');
  const parts = path.split('/');
  let entry;
  for (const part of parts) {
    const tree = await jsonResponse(await github(token, 'GET', `/repos/${PRIVATE_REPO}/git/trees/${treeSha}`, undefined, fetcher));
    entry = tree.tree?.find(e => e.path === part);
    if (!entry || !SHA.test(entry.sha || '')) throw Error('基线路径与树不符');
    treeSha = entry.sha;
  }
  if (entry.mode !== '100644' || entry.type !== 'blob' || entry.sha !== data.sha) throw Error('基线文件模式或摘要无效');
  if (data.type !== 'file' || data.encoding !== 'base64' || typeof data.content !== 'string' || !SHA.test(data.sha || '') || !Number.isSafeInteger(data.size) || data.size > MAX_PATCH) throw Error('基线文件无效');
  const bytes = Buffer.from(data.content.replace(/\s/g, ''), 'base64');
  if (bytes.length !== data.size) throw Error('基线文件长度不符');
  const blobSha = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
  if (blobSha !== data.sha) throw Error('基线文件摘要不符');
  assertText(bytes);
  return { bytes, mode: entry.mode };
}

export async function publishDraft(job, token, files, report, fetcher = fetch) {
  if (!SHA.test(job.source_sha) || !BRANCH.test(job.branch) || !ITEM_ID.test(job.item_id || '') || job.environment_id?.length > 128 || report.decision !== 'fixed' || files.size < 1) throw Error('发布数据无效');
  const root = `/repos/${PRIVATE_REPO}`;
  async function api(method, path, body) { return jsonResponse(await github(token, method, `${root}${path}`, body, fetcher)); }
  async function verifyCandidate(sha) {
    const comparison = await api('GET', `/compare/${job.source_sha}...${sha}`);
    if (!Array.isArray(comparison.files) || comparison.files.length !== files.size || comparison.files.some(file => !files.has(file.filename) || !['added', 'modified'].includes(file.status) || file.sha !== createHash('sha1').update(`blob ${files.get(file.filename).length}\0`).update(files.get(file.filename)).digest('hex'))) throw Error('候选提交与 Cloud 文件不符');
  }
  function confirmedPr(prs, sha) {
    if (prs.length !== 1) throw Error('固定分支 PR 数量不一致');
    const pr = prs[0];
    if (!Number.isInteger(pr.number) || pr.html_url !== `https://github.com/${PRIVATE_REPO}/pull/${pr.number}` || pr.head?.sha !== sha || pr.head?.ref !== job.branch || pr.base?.ref !== 'main' || pr.state !== 'open' || pr.draft !== true || pr.merged_at) throw Error('固定分支 PR 已关闭、已合并或非草稿，需人工核查');
    return { existing: true, state: 'open', number: pr.number, url: pr.html_url, commit_sha: sha };
  }
  async function createPr(sha) {
    const summary = redact(report.summary);
    const evidence = redact(report.evidence);
    const tests = report.tests.map(t => `- ${redact(t.command)}：${t.status}；${redact(t.output)}`).join('\n');
    const pr = await api('POST', '/pulls', { title: `[必审] 飞书事项 ${job.item_id}：${summary.slice(0, 60)}`, head: job.branch, base: 'main', draft: true, body: `Cloud 自述：${summary}\n\n影响范围：${[...files.keys()].join('、')}\n依据：${evidence}\n\n检查（Cloud 自述，调度器未独立运行）：\n${tests}\n\n修订：${job.revision}；基线：${job.source_sha}。请独立审阅后由维护者人工合并。` });
    if (!Number.isInteger(pr.number) || pr.state !== 'open' || pr.draft !== true || pr.html_url !== `https://github.com/${PRIVATE_REPO}/pull/${pr.number}`) throw Error('PR 回执无效');
    return { existing: false, state: 'open', number: pr.number, url: pr.html_url, commit_sha: sha };
  }
  const prs = await api('GET', `/pulls?state=all&head=sdbcbt:${encodeURIComponent(job.branch)}&base=main&per_page=100`);
  if (!Array.isArray(prs)) throw Error('PR 返回无效');
  if (prs.length) {
    // 已关闭或已合并的 PR 禁止重开；任何现存 PR 都不能再次提交代码。
    const pr = prs[0];
    if (!Number.isInteger(pr.number) || !['open', 'closed'].includes(pr.state) || pr.html_url !== `https://github.com/${PRIVATE_REPO}/pull/${pr.number}` || !SHA.test(pr.head?.sha || '') || pr.head?.ref !== job.branch || pr.base?.ref !== 'main') throw Error('现有 PR 状态不一致');
    if (pr.state !== 'open' || pr.merged_at || pr.draft !== true) throw Error('固定分支 PR 已关闭、已合并或非草稿，需人工核查');
    const branch = await api('GET', `/git/ref/heads/${job.branch}`);
    if (branch.object?.sha !== pr.head.sha) throw Error('现有 PR 分支状态不一致');
    const commit = await api('GET', `/git/commits/${pr.head.sha}`);
    if (commit.parents?.length !== 1 || commit.parents[0].sha !== job.source_sha) throw Error('现有 PR 基线不符');
    await verifyCandidate(pr.head.sha);
    return confirmedPr(prs, pr.head.sha);
  }
  const branch = await api('GET', `/git/ref/heads/${job.branch}`);
  if (branch.object?.sha !== job.source_sha) {
    if (!SHA.test(branch.object?.sha || '')) throw Error('固定分支无效');
    const candidate = await api('GET', `/git/commits/${branch.object.sha}`);
    if (candidate.parents?.length !== 1 || candidate.parents[0].sha !== job.source_sha) throw Error('专用分支已变化，需人工核查现有 PR');
    await verifyCandidate(branch.object.sha);
    const existing = await api('GET', `/pulls?state=all&head=sdbcbt:${encodeURIComponent(job.branch)}&base=main&per_page=100`);
    if (existing.length) return confirmedPr(existing, branch.object.sha);
    return createPr(branch.object.sha);
  }
  const commit = await api('GET', `/git/commits/${job.source_sha}`);
  if (!SHA.test(commit.tree?.sha || '')) throw Error('固定提交缺少树');
  const entries = [];
  for (const [path, bytes] of files) {
    assertPath(path);
    const blob = await api('POST', '/git/blobs', { content: bytes.toString('base64'), encoding: 'base64' });
    if (!SHA.test(blob.sha || '')) throw Error('GitHub blob 无效');
    entries.push({ path, mode: '100644', type: 'blob', sha: blob.sha });
  }
  const tree = await api('POST', '/git/trees', { base_tree: commit.tree.sha, tree: entries });
  if (!SHA.test(tree.sha || '')) throw Error('GitHub tree 无效');
  const next = await api('POST', '/git/commits', { message: `修复飞书事项 ${job.item_id} 第 ${job.revision} 版\n\n主要变更：${redact(report.summary)}\n影响范围：${[...files.keys()].join('、')}`, tree: tree.sha, parents: [job.source_sha] });
  if (!SHA.test(next.sha || '')) throw Error('GitHub commit 无效');
  // update ref 为非强制 CAS；提交超时结果未知时，仅查询 PR 和固定 ref，不盲目再次提交。
  const current = await api('GET', `/git/ref/heads/${job.branch}`);
  if (current.object?.sha !== job.source_sha) throw Error('分支提交结果未知，需查询固定分支');
  try { await api('PATCH', `/git/refs/heads/${job.branch}`, { sha: next.sha, force: false }); }
  catch {
    const observed = await api('GET', `/git/ref/heads/${job.branch}`);
    if (observed.object?.sha !== next.sha) throw Error('分支更新结果未知，需人工核查');
  }
  const confirmed = await api('GET', `/git/ref/heads/${job.branch}`);
  if (confirmed.object?.sha !== next.sha) throw Error('分支提交未确认');
  await verifyCandidate(next.sha);
  const existing = await api('GET', `/pulls?state=all&head=sdbcbt:${encodeURIComponent(job.branch)}&base=main&per_page=100`);
  if (existing.length) return confirmedPr(existing, next.sha);
  try { return await createPr(next.sha); }
  catch {
    const observed = await api('GET', `/pulls?state=all&head=sdbcbt:${encodeURIComponent(job.branch)}&base=main&per_page=100`);
    if (observed.length) return confirmedPr(observed, next.sha);
    throw Error('PR 创建结果未知，需人工核查');
  }
}

export function cloudTaskId(output) {
  const urls = [...output.matchAll(/https:\/\/chatgpt\.com\/codex\/tasks\/([a-zA-Z0-9_-]{6,128})(?![a-zA-Z0-9_-])/g)];
  if (urls.length !== 1) throw Error('云端任务地址格式未知');
  return urls[0][1];
}

export function findTask(json, id, environment) {
  if (!TASK_ID.test(id) || !Array.isArray(json?.tasks)) throw Error('云端任务列表无效');
  const task = json.tasks.find(t => t?.id === id);
  if (!task || !SAFE_STATUSES.has(task.status) || task.environment_id != null && task.environment_id !== environment || task.is_review === true) throw Error('云端任务不匹配');
  return task;
}

export function readCloudList(id, environment, run = command) {
  // 最多查 3 页；单任务缺席不能当成不存在或自动重提。
  let cursor;
  for (let page = 0; page < 3; page++) {
    const args = ['cloud', 'list', '--json', '--limit', '20', '--env', environment];
    if (cursor) args.push('--cursor', cursor);
    const parsed = JSON.parse(run('codex', args, { maxBuffer: 65536, timeout: 60_000 }).toString('utf8'));
    const task = parsed.tasks?.find(t => t?.id === id);
    if (task) return findTask(parsed, id, environment);
    cursor = parsed.cursor;
    if (!cursor || typeof cursor !== 'string' || cursor.length > 512) break;
  }
  throw Error('云端任务暂不可见');
}

export async function main({ sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now } = {}) {
  const env = process.env;
  if (!JOB_ID.test(env.CLOUD_JOB_ID || '') || !/^\d{1,20}$/.test(env.GITHUB_RUN_ID || '') || !/^https:\/\/[^/?#]+$/.test(env.CLOUD_BROKER_URL || '') || !env.CLOUD_BROKER_SECRET || !/^[a-zA-Z0-9+/]{43}=$/.test(env.CODEX_LOGIN_KEY || '')) throw Error('配置不足');
  const key = Buffer.from(env.CODEX_LOGIN_KEY, 'base64');
  if (key.length !== 32) throw Error('登录密钥无效');
  const url = env.CLOUD_BROKER_URL;
  async function broker(route, data) {
    const body = JSON.stringify(data);
    const response = await fetch(`${url}${route}`, { method: 'POST', redirect: 'error', headers: signedHeaders(env.CLOUD_BROKER_SECRET, body), body, signal: AbortSignal.timeout(30_000) });
    return jsonResponse(response, 500_000);
  }
  const claim = await broker('/cloud/claim', { job_id: env.CLOUD_JOB_ID, run_id: env.GITHUB_RUN_ID });
  const { job, lease_id: lease, private_token: token, auth } = claim;
  let temporary;
  let credentials;
  let authVersion;
  let authFile;
  let refreshAuth;
  let taskId = job?.cloud_task_id;
  let ambiguous = false;
  let authFault = false;
  async function report(phase, fields = {}) {
    if (!JOB_ID.test(job?.id || '') || !/^[a-zA-Z0-9_-]{8,128}$/.test(lease || '')) throw Error('领取身份不可用于回报');
    return broker('/cloud/report', { job_id: job.id, run_id: env.GITHUB_RUN_ID, lease_id: lease, phase, ...fields });
  }
  try {
    if (job?.id !== env.CLOUD_JOB_ID || !JOB_ID.test(job.id) || !SHA.test(job.source_sha || '') || !BRANCH.test(job.branch || '') || !/^[a-zA-Z0-9_-]{1,128}$/.test(job.environment_id || '') || !['submit', 'poll'].includes(job.operation) || !/^[a-zA-Z0-9_-]{8,128}$/.test(lease || '') || typeof token !== 'string' || token.length < 20 || typeof job.query !== 'string' || Buffer.byteLength(job.query) > 12000 || !Number.isSafeInteger(job.revision) || job.revision < 1) throw Error('领取数据无效');
    try { credentials = decryptAuth(auth, key); }
    catch { throw Error('登录态解密或认证失败'); }
    temporary = mkdtempSync(join(tmpdir(), 'clp-codex-'));
    const home = join(temporary, 'home');
    mkdirSync(home, { mode: 0o700 });
    authFile = join(home, 'auth.json');
    writeFileSync(authFile, credentials, { mode: 0o600, flag: 'wx' });
    authVersion = auth.version;
    refreshAuth = async () => {
      const updated = readFileSync(authFile);
      if (updated.equals(credentials)) return;
      const encrypted = encryptAuth(updated, key);
      let response;
      try {
        response = await report('auth', { auth_version: authVersion, auth_iv: encrypted.iv, auth_ciphertext: encrypted.ciphertext });
        if (!Number.isSafeInteger(response.auth_version) || response.auth_version !== authVersion + 1) throw Error('登录态 CAS 失败');
      } catch (error) {
        authFault = true;
        throw error;
      }
      authVersion = response.auth_version;
      credentials = updated;
    };
    const allowed = ['PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'no_proxy'];
    const cloudEnv = Object.fromEntries(allowed.filter(name => env[name]).map(name => [name, env[name]]));
    cloudEnv.CODEX_HOME = home;
    const runCloud = (args, opts = {}) => command('codex', args, { env: cloudEnv, cwd: temporary, maxBuffer: 65536, ...opts });
    if (job.operation === 'submit') {
      ambiguous = true;
      // exec 非幂等：CLI 超时和任何提交错误均为 unknown，不自动重试。
      const output = runCloud(['cloud', 'exec', '--env', job.environment_id, '--branch', job.branch, '--', job.query], { timeout: 180_000 });
      taskId = cloudTaskId(output.toString('utf8').trim());
      await refreshAuth();
      await report('started', { cloud_task_id: taskId });
      ambiguous = false;
    } else if (!TASK_ID.test(taskId || '')) throw Error('缺少可轮询的任务 ID');
    const deadline = now() + 18 * 60_000;
    while (now() < deadline) {
      const task = readCloudList(taskId, job.environment_id, (_program, args, options) => runCloud(args, options));
      await refreshAuth();
      if (task.status === 'ready') {
        const patch = runCloud(['cloud', 'diff', taskId], { maxBuffer: MAX_PATCH + 65536, timeout: 60_000 });
        await refreshAuth();
        const paths = inspectPatch(patch);
        if (!paths.includes(REPORT_PATH)) throw Error('云端报告缺失');
        const originals = new Map();
        for (const path of paths) {
          if (path === REPORT_PATH) continue;
          const source = await readFixedFile(token, job.source_sha, path);
          if (source !== undefined) originals.set(path, source.bytes);
        }
        const files = applyPatch(patch, paths, originals);
        if (originals.has(REPORT_PATH)) throw Error('报告不得取自仓库基线');
        const result = validateReport(JSON.parse(files.get(REPORT_PATH).toString('utf8')), job, files.size > 1);
        files.delete(REPORT_PATH);
        if (result.decision === 'fixed') {
          await report('permit', { cloud_task_id: taskId });
          const receipt = await publishDraft(job, token, files, result);
          await report('published', { cloud_task_id: taskId, decision: 'fixed', summary: result.summary, evidence: result.evidence, checks: result.tests, pr_number: receipt.number, pr_url: receipt.url, commit_sha: receipt.commit_sha });
        } else {
          await report(result.decision, { cloud_task_id: taskId, decision: result.decision, summary: result.summary, evidence: result.evidence, checks: result.tests });
        }
        return;
      }
      if (task.status === 'failed' || task.status === 'canceled' || task.status === 'cancelled') {
        await report('reconcile', { cloud_task_id: taskId });
        return;
      }
      await sleep(45_000);
    }
    await report('polling', { cloud_task_id: taskId });
  } catch (error) {
    let finalError = error;
    if (!authFault && authFile && authVersion && refreshAuth) try { await refreshAuth(); } catch (authError) { finalError = authError; }
    // execFileSync 的 message 可能包含完整命令参数，不读取；只保留 stderr 或可信静态校验文案。
    const isCommandError = finalError.status !== undefined || finalError.signal !== undefined || finalError.spawnargs || finalError.path;
    const detail = finalError.code === 'ENOENT' ? 'Codex CLI 不可用' : finalError.stderr ? String(finalError.stderr).slice(0, 400) : isCommandError ? `Codex CLI 执行失败 (${finalError.code || finalError.status || 'unknown'})` : finalError.message;
    const reason = redact(String(detail || '任务校验或执行失败')).slice(0, 400);
    try { await report(ambiguous ? 'unknown' : 'reconcile', { ...(taskId && TASK_ID.test(taskId) ? { cloud_task_id: taskId } : {}), reason }); } catch { /* 不在公开日志输出响应 */ }
    throw Error('云端任务未完成，已停止自动重提，请在 Worker 人工核查');
  } finally {
    if (temporary) rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === new URL(import.meta.url).pathname) {
  main().catch(() => { process.stderr.write('调度失败；详情仅查看受保护 Worker 状态。\n'); process.exitCode = 1; });
}
