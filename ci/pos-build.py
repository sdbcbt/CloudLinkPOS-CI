"""在公开执行器静默构建私库收银端，仅发布安装包和版本摘要。"""
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import importlib.util
import zipfile

ROOT = Path('source').resolve()
STAGE = '初始化'
SUBSTAGE = 'none'
PLATFORMS = ('windows', 'android')
# 手持刷卡终端是独立构建目标：同一 apps/pos 包的 lib/main_terminal.dart 入口加 CLP_APP_VARIANT=terminal。
TERMINAL_PLATFORM = 'terminal-android'
TARGETS = (*PLATFORMS, TERMINAL_PLATFORM)
# 两个产品分别由独立工作流派发与运行：pos 为横屏收银（Windows+Android），terminal 为手持 POS（Android）。
PRODUCTS = {'pos': PLATFORMS, 'terminal': (TERMINAL_PLATFORM,)}
TERMINAL_PACKAGE = 'com.cloudlinkpos.terminal'
TERMINAL_ENTRY = 'lib/main_terminal.dart'
ANDROID_SIGNER_CERT_SHA256 = 'b118b7c8f118d1c8b1c1115856615ee7ae1076022049a2c71f9190901a7c7d9e'
# 手持 Android 5.1（API 22／armeabi-v7a）隔离工具链：固定源码同时带有工具链清单与构建脚本才启用，
# 缺少新声明的旧固定源码继续走 Flutter 3.44 路径；Windows 与横屏 Android 始终不改工具链。
TERMINAL_TOOLCHAIN = 'dev/packaging/terminal-android/toolchain.json'
TERMINAL_BUILDER = 'dev/packaging/build_terminal_android.py'
TERMINAL_MIN_SDK = 22
TERMINAL_ABI = 'armeabi-v7a'
# 公开 CI 自持的官方 Flutter 发行摘要（releases_linux.json）；私库清单只能选择此表中的版本，
# 归档地址与 SHA-256 不由私库提供，修订或 Dart 版本不一致即停止。
TERMINAL_FLUTTER = {
    '3.32.8': {
        'frameworkRevision': 'edada7c56edf4a183c1735310e123c7f923584f1',
        'engineRevision': 'ef0cd000916d64fa0c5d09cc809fa7ad244a5767',
        'dartSdkVersion': '3.8.1',
        'archive': 'https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.32.8-stable.tar.xz',
        'sha256': 'c2c7d75f9be53f10f3fdba2f7e042b2ef0852cc088392b374bcec34c6204ed8c',
        # NDK 只经 Android SDK 官方 sdkmanager 安装（按官方仓库清单校验归档摘要），用于 API 22 原生符号核验。
        'ndkVersion': '26.3.11579264',
    },
}
TERMINAL_SDK_DIR = 'cloudlinkpos-terminal-flutter'
TERMINAL_TEST_WORKSPACE = 'cloudlinkpos-terminal-test'
TERMINAL_BUILD_WORKSPACE = 'cloudlinkpos-terminal-build'
TERMINAL_EVIDENCE = 'cloudlinkpos-terminal-evidence.json'
TERMINAL_TEST_CASES = ('test/terminal', 'test/services/device_information_service_test.dart')
SIGNING_MARKER = 'cloudlinkpos-signing-owner.json'


class BuildFailure(RuntimeError):
    def __init__(self, returncode, category):
        super().__init__('阶段失败')
        self.returncode = returncode if type(returncode) is int else 'none'
        self.category = category if category in ('dependency_resolution', 'r8_missing_classes', 'compilation', 'signing', 'unknown') else 'unknown'


def run(args, cwd=None, env=None):
    result = subprocess.run(args, cwd=ROOT if cwd is None else cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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


# Gradle 依赖下载遭仓库匿名限流（HTTP 429）时的有限重试：最多 2 次，指数退避加抖动；其他错误一律不重试。
GRADLE_RETRY_LIMIT = 2
GRADLE_RETRY_BASE_SECONDS = 30
GRADLE_RATE_LIMITED = re.compile(rb'received status code 429\b|\bhttp/[0-9.]+ 429\b|\b429 too many requests\b', re.I)


def gradle_rate_limited(log):
    # 只读本阶段刚写入的私有首个失败日志判断，日志内容不进入公开输出。
    try:
        return log.is_file() and not log.is_symlink() and GRADLE_RATE_LIMITED.search(log.read_bytes()) is not None
    except OSError:
        return False


def clear_partial_downloads():
    # 只删除 Gradle 未完成的下载临时文件与 wrapper 半截发行包，已完成的依赖缓存保留。
    home = Path(os.environ.get('GRADLE_USER_HOME') or Path.home() / '.gradle')
    removed = 0
    for pattern in ('.tmp/gradle_download*', 'caches/modules-2/files-2.1/**/*.part', 'wrapper/dists/*/*/*.part'):
        for path in home.glob(pattern):
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed += 1
    return removed


def run_gradle(args, cwd=None, env=None, reset=None):
    """运行内含 Gradle 依赖解析的构建命令；仅 HTTP 429 限流时清理未完成下载并有限重试。
    重试前删除本次尝试刚写入的私有失败日志，最终失败仍只保留一份首个失败日志。"""
    temporary = os.environ.get('RUNNER_TEMP')
    log = Path(temporary) / 'cloudlinkpos-pos-first-failure.log' if temporary else None
    for attempt in range(GRADLE_RETRY_LIMIT + 1):
        existed = log is None or log.exists() or log.is_symlink()
        try:
            return run(args, cwd, env)
        except BuildFailure as failure:
            failure.retries = attempt
            if attempt >= GRADLE_RETRY_LIMIT or existed or not gradle_rate_limited(log):
                raise
            if reset is not None:
                try:
                    reset()
                except Exception:
                    raise failure from None
            log.unlink()
            removed = clear_partial_downloads()
            window = GRADLE_RETRY_BASE_SECONDS * 2 ** attempt
            delay = window + random.uniform(0, window / 2)
            print(f'Gradle 依赖下载收到 HTTP 429 限流：第 {attempt + 1}/{GRADLE_RETRY_LIMIT} 次重试，'
                  f'已清理未完成下载 {removed} 个，等待 {delay:.1f} 秒', flush=True)
            time.sleep(delay)


def reset_terminal_build_workspace():
    # 手持 API 22 重试前按签名所有权标记删除签名副本，再移除本轮隔离工作区，私库脚本重新建立。
    cleanup_terminal_workspace()
    workspace = Path(os.environ['RUNNER_TEMP']) / TERMINAL_BUILD_WORKSPACE
    if workspace.is_symlink():
        raise ValueError('手持工作区是链接，拒绝清理')
    if workspace.exists():
        shutil.rmtree(workspace)


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def changed(paths):
    return any(path.startswith(('apps/pos/', 'dev/packaging/', '.github/workflows/build-pos-', '.github/workflows/build-terminal-'))
               or path == 'dev/development/build-pos-windows.ps1' for path in paths)


def affected(product, paths):
    # 新源码自带 client_scope.py：按私库路由表与 Dart 导入闭包精确判断本产品是否受影响。
    # 旧固定源码没有该模块时沿用原路径规则，两个产品都视为受影响，宁可多构建也不漏构建。
    helper = ROOT / 'dev/packaging/client_scope.py'
    if not helper.is_file():
        return changed(paths)
    return product in module('client_scope', helper).scopes(ROOT, paths)


def terminal_supported(source):
    # 兼容旧固定源码：只有私库打包脚本声明 terminal-android 且终端入口存在时才规划手持终端构建。
    declared = getattr(source, 'PLATFORMS', ())
    return isinstance(declared, tuple) and TERMINAL_PLATFORM in declared and (ROOT / 'apps/pos' / TERMINAL_ENTRY).is_file()


def plan():
    product = os.environ.get('BUILD_PRODUCT', '')
    if product not in PRODUCTS:
        raise ValueError('构建产品无效')
    selected = os.environ.get('BUILD_PLATFORM', 'changed')
    if selected not in ('all', 'changed', *PRODUCTS[product]):
        raise ValueError('构建平台不属于本产品')
    hit = True
    if selected == 'changed':
        before = os.environ.get('SOURCE_BEFORE_SHA', '')
        if not re.fullmatch('[0-9a-f]{40}', before) or before == '0' * 40:
            raise ValueError('主线推送基线无效')
        history = run(['git', 'rev-list', '--first-parent', os.environ['SOURCE_SHA']]).decode().splitlines()
        if before not in history:
            raise ValueError('主线推送基线不属于当前提交历史')
        paths = run(['git', 'diff', '--name-only', '--no-renames', before, os.environ['SOURCE_SHA']]).decode().splitlines()
        hit = affected(product, paths)
    source = module('pos_release', ROOT / 'dev/packaging/pos_release.py')
    if product == 'terminal' and not terminal_supported(source):
        # 旧固定源码没有手持入口：自动派发只完成规划不构建，显式指定则明确失败。
        if selected != 'changed':
            raise ValueError('固定源码尚不支持手持终端构建')
        hit = False
    platforms = ([selected] if selected in PRODUCTS[product] else list(PRODUCTS[product])) if hit else []
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


def flutter_executable():
    # 手持隔离工具链明确使用已核验的 SDK 路径；其他平台沿用 PATH 中的固定 3.44。
    # Windows SDK 使用其批处理入口，参数来自固定阶段与已校验版本计划。
    return os.environ.get('POS_FLUTTER_BIN') or ('flutter.bat' if os.name == 'nt' else 'flutter')


def flutter(*args, cwd=None, env=None, gradle=False):
    # gradle=True 的构建内含 Gradle 依赖解析，仅 HTTP 429 限流时有限重试。
    (run_gradle if gradle else run)([flutter_executable(), '--suppress-analytics', *args], ROOT / 'apps/pos' if cwd is None else cwd, env)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def terminal_toolchain():
    """仅终端矩阵：返回固定源码声明、且与公开 CI 官方摘要白名单一致的 API 22 工具链；未声明返回 None。"""
    global SUBSTAGE
    if os.environ.get('RELEASE_PLATFORM') != TERMINAL_PLATFORM:
        return None
    manifest, builder = ROOT / TERMINAL_TOOLCHAIN, ROOT / TERMINAL_BUILDER
    present = [path.exists() or path.is_symlink() for path in (manifest, builder)]
    if not any(present):
        return None
    SUBSTAGE = 'toolchain.declaration'
    if not all(path.is_file() and not path.is_symlink() for path in (manifest, builder)):
        raise ValueError('手持隔离工具链声明不完整')
    declared = module('build_terminal_android', builder).load_toolchain(manifest)
    flutter_info, android_info = declared.get('flutter') or {}, declared.get('android') or {}
    pinned = TERMINAL_FLUTTER.get(flutter_info.get('version'))
    if pinned is None or any(flutter_info.get(key) != pinned[key] for key in ('frameworkRevision', 'engineRevision', 'dartSdkVersion')):
        raise ValueError('手持 Flutter 版本不在公开 CI 官方摘要白名单')
    expected = {'packageName': TERMINAL_PACKAGE, 'minSdk': TERMINAL_MIN_SDK, 'requiredAbi': TERMINAL_ABI, 'ndkVersion': pinned['ndkVersion']}
    if any(android_info.get(key) != value for key, value in expected.items()) or declared.get('entry') != TERMINAL_ENTRY:
        raise ValueError('手持工具链目标与公开 CI 约定不一致')
    return dict(pinned, version=flutter_info['version'])


def check_flutter_revision(output, chain):
    # 公开 CI 自行核对实际 SDK，不只依赖私库脚本的同类检查。
    info = json.loads(output[output.index('{'):])
    actual = (info.get('frameworkRevision'), info.get('engineRevision'), (info.get('dartSdkVersion') or '').split(' ')[0])
    if actual != (chain['frameworkRevision'], chain['engineRevision'], chain['dartSdkVersion']):
        raise ValueError('隔离 Flutter 修订与官方摘要不一致')


def ndk_revision(path):
    text = (path / 'source.properties').read_text(encoding='utf-8')
    match = re.search(r'^Pkg\.Revision\s*=\s*([0-9.]+)\s*$', text, re.M)
    return match.group(1) if match else None


def install_terminal_toolchain():
    """下载官方 Flutter 归档并按公开 CI 固定 SHA-256 校验，解压到独立目录；经 sdkmanager 安装对应 NDK。"""
    global SUBSTAGE
    chain = terminal_toolchain()
    if chain is None:
        raise ValueError('固定源码未声明手持隔离工具链')
    temporary = Path(os.environ['RUNNER_TEMP'])
    sdk = temporary / TERMINAL_SDK_DIR
    archive = temporary / (TERMINAL_SDK_DIR + '.tar.xz')
    if sdk.exists() or archive.exists():
        raise ValueError('隔离 Flutter 目录已存在，拒绝混用')
    SUBSTAGE = 'toolchain.flutter_download'
    run(['curl', '--fail', '--location', '--retry', '3', '--silent', '--show-error', chain['archive'], '-o', str(archive)], temporary)
    SUBSTAGE = 'toolchain.flutter_digest'
    if file_sha256(archive) != chain['sha256']:
        raise ValueError('Flutter SDK 摘要不匹配')
    SUBSTAGE = 'toolchain.flutter_extract'
    sdk.mkdir()
    run(['tar', '-xf', str(archive), '-C', str(sdk)], temporary)
    archive.unlink()
    executable = sdk / 'flutter/bin/flutter'
    SUBSTAGE = 'toolchain.flutter_revision'
    check_flutter_revision(run([str(executable), '--version', '--machine', '--suppress-analytics'], temporary).decode(), chain)
    SUBSTAGE = 'toolchain.ndk'
    android_home = Path(os.environ['ANDROID_HOME'])
    run([str(android_home / 'cmdline-tools/latest/bin/sdkmanager'), '--install', 'ndk;' + chain['ndkVersion']], temporary)
    ndk = android_home / 'ndk' / chain['ndkVersion']
    if ndk_revision(ndk) != chain['ndkVersion']:
        raise ValueError('NDK 版本与手持工具链不一致')
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
        stream.write('POS_FLUTTER_BIN=' + str(executable) + '\n')
        stream.write('POS_TERMINAL_NDK=' + str(ndk) + '\n')
    with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as stream:
        stream.write(str(sdk / 'flutter/bin') + '\n')
    print('手持隔离工具链已核验：Flutter ' + chain['version'] + '，NDK ' + chain['ndkVersion'])


def terminal_workspace(name, key_properties=None):
    # 私库脚本负责复制 apps/pos 并放入手持专用依赖覆盖与锁文件，原目录不改动。
    builder = module('build_terminal_android', ROOT / TERMINAL_BUILDER)
    workspace = Path(os.environ['RUNNER_TEMP']) / name
    if workspace.exists():
        raise ValueError('手持工作区已存在，拒绝混入旧构建')
    return builder, builder.prepare_workspace(ROOT, workspace, key_properties)


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


def verify_android_signature(apk=None):
    global SUBSTAGE
    SUBSTAGE = 'android.signing_tools'
    tools = Path(os.environ['ANDROID_HOME']) / 'build-tools'
    versions = sorted((path for path in tools.iterdir() if re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', path.name) and (path / 'apksigner').is_file()),
                      key=lambda path: tuple(int(part) for part in path.name.split('.')))
    if not versions:
        raise ValueError('缺少安卓签名验证工具')
    apk = apk or ROOT / 'apps/pos/build/app/outputs/flutter-apk/app-release.apk'
    signature = run([str(versions[-1] / 'apksigner'), 'verify', '--verbose', '--print-certs', str(apk)]).decode()
    SUBSTAGE = 'android.signer_fingerprint'
    try:
        verify_signer(signature)
    except ValueError:
        save_failure_log(signature.encode('utf-8'), b'')
        raise
    return versions[-1], apk


def write_android_env(package=None):
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
        stream.write('POS_ANDROID_SIGNER_CERT_SHA256=' + ANDROID_SIGNER_CERT_SHA256 + '\n')
        if package:
            stream.write('POS_ANDROID_PACKAGE_NAME=' + package + '\n')


def android():
    global SUBSTAGE
    SUBSTAGE = 'android.compile'
    # 兼容尚未包含升级打包入口的旧固定源码，先合并 CI 不会破坏旧版构建。
    helper = ROOT / 'dev/packaging/build_android_upgrade.py'
    upgrade = module('build_android_upgrade', helper) if helper.is_file() else None
    trust = upgrade.trust_defines(os.environ) if upgrade else {}
    # Gradle 项目属性与 Dart define 使用同一组公钥和正式签名身份。
    for key, value in trust.items():
        os.environ['ORG_GRADLE_PROJECT_' + key] = value
    flutter('build', 'apk', '--release', '--no-pub', '--build-name', os.environ['POS_VERSION'],
            '--build-number', os.environ['POS_BUILD_NUMBER'], '--dart-define=POS_SOURCE_SHA=' + os.environ['POS_SOURCE_SHA'],
            *['--dart-define=' + key + '=' + value for key, value in trust.items()], gradle=True)
    tools, apk = verify_android_signature()
    write_android_env()
    SUBSTAGE = 'android.version'
    badging = run([str(tools / 'aapt'), 'dump', 'badging', str(apk)]).decode()
    verifier = module('verify_android_version', ROOT / 'dev/packaging/verify_android_version.py')
    verifier.verify(badging, os.environ['POS_VERSION'], int(os.environ['POS_BUILD_NUMBER']))
    if not upgrade:
        return
    SUBSTAGE = 'android.upgrade_manifest'
    upgrade.package(apk, tools, badging, run=lambda args, **kwargs: run(args, cwd=kwargs['cwd']))


def terminal_upgrade_helper():
    """固定源码的 build_android_upgrade.py 声明 terminal 产品时启用手持自动升级附件；
    旧固定源码没有该声明，继续只交付安装包，不注入信任公钥、不生成 OTA 附件。"""
    helper = ROOT / 'dev/packaging/build_android_upgrade.py'
    if not helper.is_file():
        return None
    upgrade = module('build_android_upgrade', helper)
    products = getattr(upgrade, 'PRODUCTS', None)
    return upgrade if isinstance(products, dict) and 'terminal' in products else None


def terminal_android():
    global SUBSTAGE
    chain = terminal_toolchain()
    SUBSTAGE = 'android.compile'
    # 只由私库构建脚本按需注入升级信任配置；不继承任何外部 Gradle 项目属性。
    for key in [name for name in os.environ if name.startswith('ORG_GRADLE_PROJECT_')]:
        del os.environ[key]
    if chain is not None:
        upgrade = terminal_upgrade_helper()
        if upgrade is not None:
            # 与横屏同一组公钥与正式签名身份；缺任何一项即停止，不产出缺信任配置的正式包。
            upgrade.trust_defines(os.environ)
        return terminal_android_api22(chain, upgrade)
    os.environ['ORG_GRADLE_PROJECT_CLP_APP_VARIANT'] = 'terminal'
    flutter('build', 'apk', '--release', '--no-pub', '-t', TERMINAL_ENTRY,
            '--build-name', os.environ['POS_VERSION'], '--build-number', os.environ['POS_BUILD_NUMBER'],
            '--dart-define=POS_SOURCE_SHA=' + os.environ['POS_SOURCE_SHA'], gradle=True)
    tools, apk = verify_android_signature()
    SUBSTAGE = 'android.version'
    badging = run([str(tools / 'aapt'), 'dump', 'badging', str(apk)]).decode()
    verify_package(badging, TERMINAL_PACKAGE)
    verifier = module('verify_android_version', ROOT / 'dev/packaging/verify_android_version.py')
    verifier.verify(badging, os.environ['POS_VERSION'], int(os.environ['POS_BUILD_NUMBER']), TERMINAL_PACKAGE)
    write_android_env(TERMINAL_PACKAGE)


def terminal_android_api22(chain, upgrade=None):
    """调用私库 build_terminal_android.py build，传入本轮版本、完整源码 SHA（--source-sha）与既有正式签名；
    构建后调用其 verify 核验 API 22／armv7／v1 签名／原生符号，再由公开 CI 复核证书、包名、版本与源码身份。"""
    global SUBSTAGE
    SUBSTAGE = 'android.compile'
    key_properties = ROOT / 'apps/pos/android/key.properties'
    if not key_properties.is_file() or key_properties.is_symlink():
        raise ValueError('缺少既有正式签名配置')
    source_sha = os.environ['POS_SOURCE_SHA']
    if not re.fullmatch('[0-9a-f]{40}', source_sha) or source_sha != os.environ.get('SOURCE_SHA', source_sha):
        raise ValueError('源码身份与本轮固定提交不一致')
    executable = flutter_executable()
    check_flutter_revision(run([executable, '--version', '--machine', '--suppress-analytics']).decode(), chain)
    temporary = Path(os.environ['RUNNER_TEMP'])
    workspace = temporary / TERMINAL_BUILD_WORKSPACE
    if workspace.exists():
        raise ValueError('手持工作区已存在，拒绝混入旧构建')
    # 私库脚本自行核对 Flutter 修订、复制隔离工作区并 --enforce-lockfile；输出全部由 run 截留在私有失败日志。
    run_gradle([sys.executable, str(ROOT / TERMINAL_BUILDER), 'build', '--workspace', str(workspace), '--flutter', executable,
                '--key-properties', str(key_properties), '--build-name', os.environ['POS_VERSION'],
                '--build-number', os.environ['POS_BUILD_NUMBER'], '--source-sha', source_sha,
                *(['--upgrade-trust'] if upgrade is not None else [])], reset=reset_terminal_build_workspace)
    app = workspace / 'apps/pos'
    apk = app / 'build/app/outputs/flutter-apk/app-release.apk'
    tools, apk = verify_android_signature(apk)
    SUBSTAGE = 'android.api22'
    evidence_path = temporary / TERMINAL_EVIDENCE
    run([sys.executable, str(ROOT / TERMINAL_BUILDER), 'verify', '--apk', str(apk), '--build-tools', str(tools),
         '--ndk', os.environ['POS_TERMINAL_NDK'], '--workdir', str(temporary / 'cloudlinkpos-terminal-symbols'),
         '--evidence', str(evidence_path)])
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    try:
        check_terminal_evidence(evidence, apk, chain)
        check_embedded_source(apk, source_sha)
    except ValueError:
        save_failure_log(json.dumps(evidence, ensure_ascii=False).encode('utf-8'), b'')
        raise
    SUBSTAGE = 'android.version'
    badging = run([str(tools / 'aapt'), 'dump', 'badging', str(apk)]).decode()
    verify_package(badging, TERMINAL_PACKAGE)
    verifier = module('verify_android_version', ROOT / 'dev/packaging/verify_android_version.py')
    verifier.verify(badging, os.environ['POS_VERSION'], int(os.environ['POS_BUILD_NUMBER']), TERMINAL_PACKAGE)
    write_android_env(TERMINAL_PACKAGE)
    if upgrade is not None:
        # 同一正式签名 APK 生成 product=terminal、arch=arm 的离线签名清单与升级附件。
        SUBSTAGE = 'android.upgrade_manifest'
        upgrade.package(apk, tools, badging, run=lambda args, **kwargs: run(args, cwd=kwargs['cwd']), product='terminal')
    # 仅输出公开安装包本身即可得到的白名单摘要，作为联迪真机验收核对来源。
    print('手持 API 22 安装包核验：' + json.dumps({key: evidence[key] for key in (
        'apkSHA256', 'packageName', 'versionName', 'versionCode', 'minSdk', 'signerCertSHA256', 'v1Signature')}, ensure_ascii=False)
        + '；abi=' + TERMINAL_ABI + '；flutter=' + chain['version'])


def check_terminal_evidence(evidence, apk, chain):
    # 私库 verify 产出的证据逐项与本轮计划、固定正式证书和 API 22 目标比对。
    libs = evidence.get('nativeLibs') or []
    checks = [
        evidence.get('schema') == 'cloudlinkpos.terminal-android-evidence/v1',
        evidence.get('apkSHA256') == file_sha256(apk),
        evidence.get('packageName') == TERMINAL_PACKAGE,
        evidence.get('versionName') == os.environ['POS_VERSION'],
        evidence.get('versionCode') == str(os.environ['POS_BUILD_NUMBER']),
        type(evidence.get('minSdk')) is int and evidence['minSdk'] == TERMINAL_MIN_SDK,
        evidence.get('v1Signature') is True,
        evidence.get('signerCertSHA256') == ANDROID_SIGNER_CERT_SHA256,
        all(f'lib/{TERMINAL_ABI}/{name}' in libs for name in ('libflutter.so', 'libapp.so')),
        bool(evidence.get('symbolCheckedLibs')),
        (evidence.get('flutter') or {}).get('frameworkRevision') == chain['frameworkRevision'],
    ]
    if not all(checks):
        raise ValueError('手持 API 22 核验证据与本轮构建不一致')


def check_embedded_source(apk, source_sha):
    # POS_SOURCE_SHA 以 --dart-define 编入 AOT 快照，软件内“版本信息”显示该值；在 armv7 libapp.so 中逐字核对。
    with zipfile.ZipFile(apk) as archive:
        snapshot = archive.read(f'lib/{TERMINAL_ABI}/libapp.so')
    if source_sha.encode('ascii') not in snapshot:
        raise ValueError('手持 API 22 核验证据缺少本轮源码身份')


def cleanup_terminal_workspace():
    # 私库脚本复制进隔离工作区的 key.properties 只按签名所有权标记的摘要删除，变更或链接即拒绝。
    temporary = os.environ.get('RUNNER_TEMP')
    if not temporary:
        return
    copy = Path(temporary) / TERMINAL_BUILD_WORKSPACE / 'apps/pos/android/key.properties'
    if not copy.exists() and not copy.is_symlink():
        return
    marker = Path(temporary) / SIGNING_MARKER
    digest = json.loads(marker.read_text(encoding='utf-8')).get(str(ROOT / 'apps/pos/android/key.properties')) if marker.is_file() else None
    if copy.is_symlink() or digest is None or file_sha256(copy) != digest:
        raise ValueError('手持工作区签名副本已变更，拒绝清理其他内容')
    copy.unlink()


def verify_package(badging, expected):
    # 公开 CI 自行核对 APK 实际包名，不只依赖私库校验脚本。
    line = next((item for item in badging.splitlines() if item.startswith('package: ')), '')
    match = re.match(r"package: name='([^']+)'", line)
    if not match or match.group(1) != expected:
        raise ValueError('APK 包名与构建目标不一致')


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
        chain = terminal_toolchain()
        SUBSTAGE = 'test.flutter_pub_get'
        if chain is not None:
            # 隔离工具链只能按手持专用锁文件解析，测试在单独工作区进行，不改 apps/pos。
            _, app = terminal_workspace(TERMINAL_TEST_WORKSPACE)
            flutter('pub', 'get', '--enforce-lockfile', cwd=app)
            SUBSTAGE = 'test.flutter_cases'
            flutter('test', '--no-pub', *TERMINAL_TEST_CASES, cwd=app)
            return
        flutter('pub', 'get', '--enforce-lockfile')
        SUBSTAGE = 'test.flutter_cases'
        if os.environ.get('RELEASE_PLATFORM') == TERMINAL_PLATFORM:
            flutter('test', '--no-pub', *TERMINAL_TEST_CASES)
        else:
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
    elif command == 'toolchain':
        # 只输出固定模式，不输出私库清单内容。
        mode = 'api22' if terminal_toolchain() is not None else 'default'
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('mode=' + mode + '\n')
        print('安卓工具链模式：' + mode)
    elif command == 'terminal-toolchain':
        install_terminal_toolchain()
    elif command == 'android':
        android()
    elif command == TERMINAL_PLATFORM:
        terminal_android()
    elif command == 'package':
        platform = os.environ['RELEASE_PLATFORM']
        if platform not in TARGETS:
            raise ValueError('构建平台无效')
        source = 'windows-output' if platform == 'windows' else 'apps/pos/build/app/outputs/flutter-apk'
        if terminal_toolchain() is not None:
            source = str(Path(os.environ['RUNNER_TEMP']) / TERMINAL_BUILD_WORKSPACE / 'apps/pos/build/app/outputs/flutter-apk')
        run([sys.executable, 'dev/packaging/pos_release.py', 'package', '--platform', platform, '--input', source])
    elif command == 'publish':
        run([sys.executable, 'dev/packaging/pos_release.py', 'publish', '--platform', os.environ['RELEASE_PLATFORM']])
        print('安装包和 SHA-256 已核验并发布至 https://github.com/sdbcbt/CloudLinkPOS-CI/releases')
    elif command == 'cleanup':
        # 先删手持工作区签名副本，无论成败都继续执行既有签名清理，再报告首个错误。
        try:
            cleanup_terminal_workspace()
        finally:
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
