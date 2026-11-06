"""在公开执行器静默构建私库收银端，仅发布安装包和版本摘要。"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import importlib.util

ROOT = Path('source').resolve()
STAGE = '初始化'
SUBSTAGE = 'none'
PLATFORMS = ('windows', 'android')
ANDROID_SIGNER_CERT_SHA256 = 'b118b7c8f118d1c8b1c1115856615ee7ae1076022049a2c71f9190901a7c7d9e'


class BuildFailure(RuntimeError):
    def __init__(self, returncode, category):
        super().__init__('阶段失败')
        self.returncode = returncode if type(returncode) is int else 'none'
        self.category = category if category in ('dependency_resolution', 'r8_missing_classes', 'compilation', 'signing', 'unknown') else 'unknown'


def run(args, cwd=ROOT, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        save_failure_log(result.stdout or b'', result.stderr or b'')
        # 只保留退出码与固定类别，不把命令、私库输出或凭据带入异常。
        category = classify_failure((result.stdout or b'') + b'\n' + (result.stderr or b''))
        raise BuildFailure(result.returncode, category)
    return result.stdout


def save_failure_log(stdout, stderr):
    # 首个失败独占临时文件，后续清理失败不能覆盖；保存失败不改变原构建结果。
    temporary = os.environ.get('RUNNER_TEMP')
    if not temporary:
        return
    try:
        path = Path(temporary) / 'cloudlinkpos-pos-first-failure.log'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(b'stdout:\n' + stdout + b'\nstderr:\n' + stderr)
    except OSError:
        pass


def classify_failure(output):
    output = output.lower()
    if b'missing classes detected while running r8' in output or b'r8: missing class' in output:
        return 'r8_missing_classes'
    if any(marker in output for marker in (
        b'could not resolve all files for configuration', b'could not resolve all artifacts',
        b'could not resolve all dependencies', b'could not download ', b'could not get resource ',
        b'version solving failed', b'pub get failed', b'failed host lookup:',
    )):
        return 'dependency_resolution'
    if any(marker in output for marker in (
        b'release signing requires ', b'keystore was tampered with', b'failed to read key ',
    )):
        return 'signing'
    if any(marker in output for marker in (
        b'compilation failed', b'target kernel_snapshot_program failed', b'dart compilation failed',
    )):
        return 'compilation'
    return 'unknown'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def changed(paths):
    return any(path.startswith(('apps/pos/', 'dev/packaging/', '.github/workflows/build-pos-'))
               or path == 'dev/development/build-pos-windows.ps1' for path in paths)


def plan():
    selected = os.environ.get('BUILD_PLATFORM', 'changed')
    if selected not in ('all', 'changed', *PLATFORMS):
        raise ValueError('构建平台无效')
    platforms = list(PLATFORMS) if selected in ('all', 'changed') else [selected]
    if selected == 'changed':
        before = os.environ.get('SOURCE_BEFORE_SHA', '')
        if not re.fullmatch('[0-9a-f]{40}', before) or before == '0' * 40:
            raise ValueError('主线推送基线无效')
        history = run(['git', 'rev-list', '--first-parent', os.environ['SOURCE_SHA']]).decode().splitlines()
        if before not in history:
            raise ValueError('主线推送基线不属于当前提交历史')
        paths = run(['git', 'diff', '--name-only', '--no-renames', before, os.environ['SOURCE_SHA']]).decode().splitlines()
        if not changed(paths):
            platforms = []
    source = module('pos_release', ROOT / 'dev/packaging/pos_release.py')
    value = source.make_plan(ROOT)
    # 仅允许既有协议中的版本和摘要进入公开 job 输出。
    keys = ('schema', 'repository', 'sourceSHA', 'sourceTreeSHA', 'sourceRevision', 'version', 'buildNumber', 'softwareVersion')
    value = {key: value[key] for key in keys}
    if value['sourceSHA'] != os.environ['SOURCE_SHA']:
        raise ValueError('版本计划没有绑定固定源码')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'plan.json'
        path.write_text(json.dumps(value))
        source.read_plan(path)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
        stream.write('plan=' + json.dumps(value) + '\n')
        stream.write('matrix=' + json.dumps({'platform': platforms}) + '\n')
        stream.write('has_platforms=' + str(bool(platforms)).lower() + '\n')


def prepare():
    value = json.loads(os.environ['POS_RELEASE_PLAN'])
    path = ROOT / 'pos-release-plan.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    source = module('pos_release', ROOT / 'dev/packaging/pos_release.py')
    if source.read_plan(path) != source.make_plan(ROOT):
        raise ValueError('构建源码与计划不一致')
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
        for key, item in [('POS_VERSION', value['version']), ('POS_BUILD_NUMBER', value['buildNumber']), ('POS_SOURCE_SHA', value['sourceSHA'])]:
            stream.write(f'{key}={item}\n')


def flutter(*args):
    # Windows SDK 使用其批处理入口，参数来自固定阶段与已校验版本计划。
    executable = 'flutter.bat' if os.name == 'nt' else 'flutter'
    run([executable, '--suppress-analytics', *args], ROOT / 'apps/pos')


def windows():
    run(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
         str(ROOT / 'dev/development/build-pos-windows.ps1'),
         '-OutputDirectory', str(ROOT / 'windows-output'),
         '-BuildVersion', os.environ['POS_VERSION'], '-BuildNumber', os.environ['POS_BUILD_NUMBER'],
         '-SourceSha', os.environ['POS_SOURCE_SHA']])


def installer():
    script = r'''
    $ErrorActionPreference = 'Stop'
    $prereq = Join-Path $env:RUNNER_TEMP 'prerequisites'
    New-Item -ItemType Directory -Path $prereq | Out-Null
    Invoke-WebRequest 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile "$prereq/VC_redist.x64.exe"
    Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile "$prereq/WebView2Setup.exe"
    Get-ChildItem $prereq -Filter *.exe | ForEach-Object {
      $sig = Get-AuthenticodeSignature $_.FullName
      if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw '微软依赖签名验证失败' }
    }
    $stage = Get-ChildItem windows-output -Directory | Select-Object -First 1
    if (-not $stage) { throw '缺少完整客户端目录' }
    & "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe" "/DSourceDir=$($stage.FullName)" "/DPrereqDir=$prereq" "/DOutputDir=$PWD/windows-output" "/DReleaseVersion=$env:POS_VERSION" dev/packaging/windows/setup.iss
    if ($LASTEXITCODE -ne 0) { throw '安装包编译失败' }
    '''
    run(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script])


def android():
    global SUBSTAGE
    SUBSTAGE = 'android.compile'
    flutter('build', 'apk', '--release', '--no-pub', '--build-name', os.environ['POS_VERSION'],
            '--build-number', os.environ['POS_BUILD_NUMBER'], '--dart-define=POS_SOURCE_SHA=' + os.environ['POS_SOURCE_SHA'])
    SUBSTAGE = 'android.signing_tools'
    tools = Path(os.environ['ANDROID_HOME']) / 'build-tools'
    versions = sorted((path for path in tools.iterdir() if re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', path.name) and (path / 'apksigner').is_file()),
                      key=lambda path: tuple(int(part) for part in path.name.split('.')))
    if not versions:
        raise ValueError('缺少安卓签名验证工具')
    apk = ROOT / 'apps/pos/build/app/outputs/flutter-apk/app-release.apk'
    signature = run([str(versions[-1] / 'apksigner'), 'verify', '--verbose', '--print-certs', str(apk)]).decode()
    SUBSTAGE = 'android.signer_fingerprint'
    try:
        verify_signer(signature)
    except ValueError:
        save_failure_log(signature.encode('utf-8'), b'')
        raise
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
        stream.write('POS_ANDROID_SIGNER_CERT_SHA256=' + ANDROID_SIGNER_CERT_SHA256 + '\n')
    SUBSTAGE = 'android.version'
    badging = run([str(versions[-1] / 'aapt'), 'dump', 'badging', str(apk)]).decode()
    verifier = module('verify_android_version', ROOT / 'dev/packaging/verify_android_version.py')
    verifier.verify(badging, os.environ['POS_VERSION'], int(os.environ['POS_BUILD_NUMBER']))


def verify_signer(output):
    # 新版 SDK 将单个 V2 签名者标为 V2 Signer，仍严格核对数量与同一正式证书。
    counts = re.findall(r'^Number of signers: ([0-9]+)[ \t]*$', output, re.M)
    certificates = [line for line in output.splitlines() if 'certificate SHA-256 digest:' in line]
    fingerprint = re.fullmatch(r'(?:Signer #1|V2 Signer:) certificate SHA-256 digest: ([0-9a-fA-F]{64})',
                               certificates[0]) if len(certificates) == 1 else None
    if counts != ['1'] or fingerprint is None or fingerprint.group(1).lower() != ANDROID_SIGNER_CERT_SHA256:
        raise ValueError('APK 签名证书与固定正式证书不一致')


def main(command):
    global SUBSTAGE
    SUBSTAGE = 'none'
    if os.environ.get('GITHUB_REPOSITORY') != 'sdbcbt/CloudLinkPOS-CI' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('仅允许公开 CI 主线执行正式构建')
    if command == 'checkout':
        module('source_checkout', Path('automation/ci/full-build.py')).checkout()
    elif command == 'plan':
        plan()
    elif command == 'prepare':
        prepare()
    elif command == 'test':
        SUBSTAGE = 'test.python_packaging'
        run([sys.executable, '-m', 'unittest', 'discover', '-s', 'dev/packaging', '-p', 'test_*.py'])
        SUBSTAGE = 'test.flutter_pub_get'
        flutter('pub', 'get', '--enforce-lockfile')
        SUBSTAGE = 'test.flutter_cases'
        flutter('test', '--no-pub', 'test/hardware/keyboard_service_test.dart', 'test/services/device_information_service_test.dart')
    elif command == 'signing':
        required = ('ANDROID_KEYSTORE_BASE64', 'ANDROID_KEYSTORE_PASSWORD', 'ANDROID_KEY_PASSWORD', 'ANDROID_KEY_ALIAS')
        if any(not os.environ.get(key) for key in required):
            print('::error::缺少公开 CI 仓库的固定安卓正式签名 Secrets；不能使用测试或 debug 签名。', file=sys.stderr)
            raise ValueError('缺少原发布签名')
        run([sys.executable, 'dev/packaging/prepare_android_signing.py'])
    elif command == 'windows':
        windows()
    elif command == 'installer':
        installer()
    elif command == 'android':
        android()
    elif command == 'package':
        platform = os.environ['RELEASE_PLATFORM']
        if platform not in PLATFORMS:
            raise ValueError('构建平台无效')
        source = 'windows-output' if platform == 'windows' else 'apps/pos/build/app/outputs/flutter-apk'
        run([sys.executable, 'dev/packaging/pos_release.py', 'package', '--platform', platform, '--input', source])
    elif command == 'publish':
        run([sys.executable, 'dev/packaging/pos_release.py', 'publish', '--platform', os.environ['RELEASE_PLATFORM']])
        print('安装包和 SHA-256 已核验并发布至 https://github.com/sdbcbt/CloudLinkPOS-CI/releases')
    elif command == 'cleanup':
        run([sys.executable, 'dev/packaging/prepare_android_signing.py', '--cleanup'])
    else:
        raise ValueError('未知构建阶段')


if __name__ == '__main__':
    try:
        STAGE = sys.argv[1]
        main(STAGE)
        print('本阶段完成：' + STAGE)
    except Exception as error:
        # 私库构建日志、源码路径、堆栈与异常原文不进入公开日志。
        code = error.returncode if isinstance(error, BuildFailure) else 'none'
        category = error.category if isinstance(error, BuildFailure) else 'unknown'
        if SUBSTAGE in ('android.signing_tools', 'android.signer_fingerprint'):
            category = 'signing'
        print('::error::收银客户端构建停止于：' + STAGE + '；子阶段：' + SUBSTAGE +
              '；exit_code=' + str(code) + '；category=' + category + '；请在私库受控环境复现。', file=sys.stderr)
        sys.exit(1)
