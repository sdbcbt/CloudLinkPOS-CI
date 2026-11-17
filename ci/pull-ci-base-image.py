#!/usr/bin/env python3
"""沿用固定源码的官方基镜像契约，入口不可用时仅切换同摘要的另一个官方来源。"""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.dont_write_bytecode = True
RETRY_DELAYS = (5, 15)
PROFILE_TAGS = {'alpine': '3.22.1', 'nginx': '1.27.5-alpine'}
PUBLIC_ERROR_CODES = frozenset({
    'BASE_PROFILE_INVALID', 'BASE_SOURCE_MODULE_INVALID', 'BASE_SOURCE_CONTRACT_INVALID',
    'BASE_INSPECT_INVALID', 'BASE_DIGEST_MISMATCH', 'BASE_PLATFORM_OR_INDEX_INVALID',
    'BASE_REMOTE_PURPOSE_INVALID', 'BASE_INDEX_VERIFICATION_FAILED', 'BASE_OFFICIAL_SOURCES_EXHAUSTED',
})


class IntegrityError(ValueError):
    pass


def load_private_helper(repo_root):
    path = repo_root / 'deploy/scripts/pull-ci-base-image.py'
    # 源码模块的输出与异常文本均不得进入公共日志。
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            spec = importlib.util.spec_from_file_location('fixed_source_base_image', path)
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
    except Exception:
        raise ValueError('BASE_SOURCE_MODULE_INVALID') from None
    return helper


def candidates(helper, profile):
    if profile not in PROFILE_TAGS:
        raise IntegrityError('BASE_PROFILE_INVALID')
    image = helper.SHARED.OFFICIAL_BASE_IMAGES[profile]
    pattern = r'public\.ecr\.aws/docker/library/' + re.escape(profile) + ':' + re.escape(PROFILE_TAGS[profile]) + r'@(sha256:[0-9a-f]{64})'
    match = re.fullmatch(pattern, image)
    if not match:
        raise IntegrityError('BASE_SOURCE_CONTRACT_INVALID')
    fallback = 'docker.io/library/' + profile + ':' + PROFILE_TAGS[profile] + '@' + match[1]
    return [('ecr', image), ('dockerhub', fallback)]


def diagnostic(source, stage, attempt, started, result, reason=None, status=None, stream=None):
    event = {'source': source, 'stage': stage, 'attempt': attempt, 'elapsedSeconds': round(max(0, time.monotonic() - started), 3), 'result': result}
    if reason:
        event['reason'] = reason
    if status in (401, 403, 404, 408, 429, 500, 502, 503, 504):
        event['httpStatus'] = status
    print('官方基镜像状态：' + json.dumps(event, ensure_ascii=False), file=stream, flush=True)


def captured(command, timeout):
    return subprocess.run(command, capture_output=True, timeout=timeout)


def failure_category(message):
    status = re.search(r'\b(401|403|404|408|429|500|502|503|504)\b', message)
    status = int(status[1]) if status else None
    if status == 429 or re.search(r'too ?many ?requests', message, re.I):
        return 'rate_limit', status, True
    if status in (401, 403):
        return 'authentication', status, False
    if status == 404:
        return 'not_found', status, False
    if re.search(r'certificate|x509', message, re.I):
        return 'certificate', status, False
    if status in (408, 500, 502, 503, 504) or re.search(r'timeout|timed out|connection reset|TLS|\bEOF\b|temporary|network is unreachable|no such host|connection refused', message, re.I):
        return 'network', status, True
    return 'docker_error', status, False


def checked_command(source, stage, command, timeout, stream=None):
    for attempt in range(len(RETRY_DELAYS) + 1):
        started = time.monotonic()
        try:
            result = captured(command, timeout)
        except subprocess.TimeoutExpired:
            reason, status, transient = 'timeout', None, True
        except OSError:
            diagnostic(source, stage, attempt + 1, started, 'failed', 'local_tool', stream=stream)
            raise RuntimeError('BASE_LOCAL_TOOL_FAILED') from None
        else:
            if result.returncode == 0:
                diagnostic(source, stage, attempt + 1, started, 'success', stream=stream)
                return result.stdout
            # 仅分类并输出白名单状态码，不公开工具响应、URL、源码路径或认证值。
            message = ((result.stdout or b'') + (result.stderr or b'')).decode(errors='replace')
            reason, status, transient = failure_category(message)
        diagnostic(source, stage, attempt + 1, started, 'failed', reason, status, stream)
        if not transient or attempt == len(RETRY_DELAYS):
            raise RuntimeError('BASE_SOURCE_REQUEST_FAILED')
        time.sleep(RETRY_DELAYS[attempt])


def pull_candidate(source, image):
    checked_command(source, 'pull', ['docker', 'pull', '--platform', 'linux/amd64', image], 120)


def verify_candidate(helper, source, profile, image):
    started = time.monotonic()
    raw = checked_command(source, 'local-inspect', ['docker', 'image', 'inspect', image], 30)
    try:
        items = json.loads(raw)
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise IntegrityError('BASE_INSPECT_INVALID')
        metadata = items[0]
        repository, digest = image.split('@', 1)
        repository = repository.rsplit(':', 1)[0]
        references = {repository + '@' + digest}
        if source == 'dockerhub':
            # Docker本地名称会规范化官方library引用；仅允许同一个官方仓库的等价名称。
            references.update({profile + '@' + digest, 'library/' + profile + '@' + digest, 'docker.io/library/' + profile + '@' + digest})
        actual = metadata.get('RepoDigests')
        if not isinstance(actual, list) or not any(reference in actual for reference in references):
            raise IntegrityError('BASE_DIGEST_MISMATCH')
        if metadata.get('Os') == 'linux' and metadata.get('Architecture') == 'amd64':
            diagnostic(source, 'local-validation', 1, started, 'success')
            return
        descriptor = metadata.get('Descriptor')
        if metadata.get('Os') not in (None, '') or metadata.get('Architecture') not in (None, '') or not isinstance(descriptor, dict) or descriptor.get('mediaType') not in helper.INDEX_MEDIA_TYPES or ('digest' in descriptor and descriptor['digest'] != digest):
            raise IntegrityError('BASE_PLATFORM_OR_INDEX_INVALID')
    except (json.JSONDecodeError, TypeError, KeyError):
        diagnostic(source, 'local-validation', 1, started, 'failed', 'structure')
        raise IntegrityError('BASE_INSPECT_INVALID') from None
    except IntegrityError as error:
        diagnostic(source, 'local-validation', 1, started, 'failed', 'digest' if str(error) == 'BASE_DIGEST_MISMATCH' else 'platform_or_structure')
        raise
    diagnostic(source, 'local-validation', 1, started, 'success')
    started = time.monotonic()
    # 仅替换私库校验器的网络读取；父索引原始hash、唯一amd64子manifest及配置算法保持原样。
    original_remote = helper.remote_inspect
    output = sys.stdout
    last_stage = 'remote-index'
    def read_remote(command, purpose):
        nonlocal last_stage
        if purpose not in ('父索引读取', '子镜像配置读取'):
            raise IntegrityError('BASE_REMOTE_PURPOSE_INVALID')
        last_stage = 'remote-index' if purpose == '父索引读取' else 'remote-config'
        return checked_command(source, last_stage, command, 120, output)
    helper.remote_inspect = read_remote
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            helper.verify_index_platform(image)
    except ValueError as error:
        reason = 'digest' if '摘要' in str(error) else 'platform' if '平台' in str(error) or 'amd64' in str(error) else 'structure'
        diagnostic(source, last_stage + '-validation', 1, started, 'failed', reason)
        raise IntegrityError('BASE_INDEX_VERIFICATION_FAILED') from None
    finally:
        helper.remote_inspect = original_remote
    diagnostic(source, 'index-platform-validation', 1, started, 'success')


def prepare(repo_root, profile, output):
    # full-build直接调用本入口，异常也必须先收敛为白名单，不能依赖CLI最后一层脱敏。
    try:
        return prepare_verified(repo_root, profile, output)
    except Exception as error:
        if isinstance(error, ValueError) and str(error) in PUBLIC_ERROR_CODES:
            raise
        raise ValueError('BASE_PREPARATION_FAILED') from None


def prepare_verified(repo_root, profile, output):
    helper = load_private_helper(repo_root)
    helper.SHARED.reject_symlink_ancestors(output)
    for source, image in candidates(helper, profile):
        started = time.monotonic()
        try:
            pull_candidate(source, image)
            verify_candidate(helper, source, profile, image)
        except IntegrityError:
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = type(error).__name__
            reason = reason if reason in ('RuntimeError', 'TimeoutExpired', 'OSError', 'FileNotFoundError') else 'ToolError'
            diagnostic(source, 'candidate', 1, started, 'failed', reason)
            continue
        with output.open('a', encoding='utf-8') as stream:
            stream.write('base_image=' + image + '\n')
        return image
    raise ValueError('BASE_OFFICIAL_SOURCES_EXHAUSTED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--profile', choices=sorted(PROFILE_TAGS), required=True)
    parser.add_argument('--github-output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.repo_root, args.profile, args.github_output)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        category = str(error) if isinstance(error, ValueError) and str(error) in PUBLIC_ERROR_CODES else 'BASE_PREPARATION_FAILED'
        print('::error::官方基镜像准备失败：' + category, file=sys.stderr)
        sys.exit(1)
