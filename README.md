
### 派发者限制

`build-pos-release.yml`、`build-terminal-release.yml`（两者经 `client-release.yml`）、`private-main.yml`（经 `full-version-build.yml`）、`full-version-build.yml`、`miniprogram-upload.yml`、`private-quality.yml` 只接受通知机器人 `cloudlinkpos-ci-notifier[bot]` 派发（2026-11-12 用户决定）：首个作业的第一步核对 `github.actor`，任何人在 Actions 页面手动 Run workflow 都会在该步失败，后续作业不会执行。

- 失败批次使用 **Re-run all jobs** 重跑：重跑时 `github.actor` 仍是原派发者，照常执行。
- 需要新的构建：合并私库 main，由 Webhook → Cloudflare Worker 自动派发；Worker 已接收但未派发的任务按 ID 在 Worker 重试。
- `pilot-image.yml` 是固定提交的试点工作流，不在 Worker 派发范围内，不受此限制。
- 离线检查：`python3 ci/dispatch-guard_test.py`。

### 主线构建与全局发布模式

`private-main.yml` 调用 `full-version-build.yml`，按私库推送前后提交识别受影响服务。`services` 默认 `changed`；`services` 填逗号分隔的服务名（如 `inventory-rpc,iot-rpc`）时只构建这些服务（每次构建都会生成新版本号），该入口只能由通知机器人派发，见下方「派发者限制」。补建批次只写入 `builds/` 与 `catalog.json`，不写 `sources/<提交>.json`，避免以部分服务冒充该提交的整批 PR 收据。构建只推送版本号和 latest，不生成 SHA 标签。每个成功服务的私有访问证据与整批计划核对后，由 `ci/publish-receipts.py` 将白名单元数据写入 `source-build-receipts` 分支；源码、日志和凭据不进入该分支。收据文件存在不代表工作流已结束，生产发布服务还必须核对具体 run/attempt 的最终成功结果。

生产后台的全局发布模式保存在私库 Fleet 清单的 `releasePolicy`。受管清单迁到 Fleet 专用部署分支 `fleet-deploy-do-not-delete`（该分支严禁删除）后，主线清单冻结；构建计划同时读取主线与部署分支两份清单，取 `generation` 较高的策略，迁移前后都无需额外开关。关闭时，生产服务核验本批构建成功、策略代次一致、正式版本就绪及源码未倒退后，使用生产专用 GitHub App 更新私库清单，由 Fleet 发布；开启时保留待灰度版本，公开 CI 不能绕过。进行中的任务、候选基础设施及灰度页面不会因关闭开关被删除。公开 CI 不持有私库源码或部署清单写入凭据。

### 构建失败与重跑

基础镜像从固定私库提交的官方引用读取，先使用 AWS ECR，入口因网络、认证、证书或 Docker 工具错误不可用时，仅切换到相同官方镜像、标签与父摘要的 Docker Hub 入口。每个候选都检查本地 RepoDigests 与 linux/amd64；containerd 返回空平台索引时继续沿用私库的原始父摘要、唯一 amd64 子镜像及配置核验。摘要或平台不匹配立即停止，不以更换来源绕过完整性检查。公共日志仅显示固定来源、阶段、次数、耗时及错误类别，不输出工具原文。

当前发布收据严格限定同一 run/attempt 的完整服务批次。失败后使用 GitHub 的 **Re-run all jobs**，不支持 **Re-run failed jobs**：后者缺少当前 attempt 的完整计划和收据，会明确拒绝生成发布目录。计划和每个服务制品名包含 attempt，整批重跑不会覆盖旧制品，也不会把上次成功服务伪装成本次执行。跨 attempt 合并凭证暂不在本轮范围；普通修复代码后的新 main 推送照常自动构建。

连续合并的构建使用 `queue: max` 保留排队任务（上限 100），避免 GitHub 默认只保留一个 pending 运行而丢掉中间推送范围。排队按服务进行（`full-version-publication-<服务>`）：同一服务的版本计算与推送仍串行、不取消在途构建，不同服务和无需构建的运行互不等待；收据发布单独串行（`full-version-receipts`）。镜像编译并行度保持不变；CF/R2 同时最多接纳 10 个待发布镜像，入口明确返回发布槽繁忙时随机等待 5–15 秒，最多 45 分钟且截止后不再发起新请求；认证、容量不足及其他错误不当作繁忙重试。明确可重放的网络请求最多尝试4次，退避1/3/6秒；仅瞬断、超时与502/503/504重试；兼容旧入口将 JWKS 获取瞬态故障误报为401时，仅明确的 KEY_FETCH_TIMEOUTERROR/ABORTERROR/TYPEERROR/ERROR 和 JWKS_HTTP_429/500/502/503/504 原因重试。普通401、声明、时间、密钥或签名拒绝与证书验证错误立即停止。每次尝试重新取得 OIDC，日志仅额外记录白名单 authReason，未知响应头和正文不输出。单层每次重开本地文件，服务端已收到的层按摘要跳过。CF 单次请求超时为 600 秒，日志仅记录固定请求类别、方法、耗时、HTTP 状态或异常类。正式镜像改为先推送私有 `ghcr.io/sdbcbt/cloudlinkpos`，再同步同一摘要到 `registry.cloudlinkpos.com/sdbcbt/cloudlinkpos`；不再等待 ACR。计划阶段只读检查正式 CF 身份与容量，不清理旧版本；构建完成后的上传准备阶段才清理更老且无引用的版本，再核验本批容量；原有最新两版不为腾空间提前删除，清理后仍不足才报错。版本下限同时读取固定私库提交中的 ACR 历史基线、最新 Fleet 与 GHCR。发布前核对全部层摘要和匿名拒绝，CI 为本次镜像与 Fleet 摘要增加最长 48 小时保护租约。新镜像构建、双端上传及版本/latest/匿名拒绝校验全部成功后才确认版本并安全回收；失败的新镜像不替换旧两版保留名额，上传容量不足时先安全清理额外无引用历史，仍不足则报错，不预删旧两版腾空间。正式 R2 预算为十进制 9 GB（9,000,000,000 字节），保留每服务最近两版及所有运行/发布保护引用；首次权威保护建立前禁止回收。单层超过 99 MB 或容量不足明确失败。详细部署、凭据与引导顺序见私库 `docs/06-部署运维/GHCR与Cloudflare正式镜像部署.md`。旧 ACR 上传脚本保留供显式历史兼容。


### 收银客户端永久下载

Windows / Android 在本公开仓库的 [收银客户端永久版本构建](https://github.com/sdbcbt/CloudLinkPOS-CI/actions/workflows/build-pos-release.yml) 工作流执行，并将安装包、`release-manifest.json` 和 `SHA256SUMS.txt` 发布到 [GitHub Releases](https://github.com/sdbcbt/CloudLinkPOS-CI/releases)。Windows 同时保留中文安装向导与便携 ZIP；下载附件不依赖 Actions Artifact 保留期限。历史私库 Releases 不删除、不覆盖。

横屏收银与手持 POS 是两个独立工作流、两个独立运行：`build-pos-release.yml` 只构建 Windows + Android 收银客户端（并发组 `pos-permanent-releases`），[`build-terminal-release.yml`](https://github.com/sdbcbt/CloudLinkPOS-CI/actions/workflows/build-terminal-release.yml) 只构建手持 POS Android（并发组 `terminal-permanent-releases`）。两者只是入口，规划、签名、构建、核验、发布与失败回写共用 `client-release.yml`（`product` 为 `pos` 或 `terminal`）。

私库 main 推送由现有 Cloudflare Worker 按私库 `dev/packaging/client-routes.json` 分别派发：手持专属改动只派发手持，收银专属改动只派发收银，共用改动两个都派发，推送范围不明确时两个都派发。每个运行再在固定源码上按 before/after 精确判断：新源码由私库 `dev/packaging/client_scope.py` 用同一路由表加 Dart 导入闭包判断本产品是否受影响（例如 `lib/data/` 中只被收银机导入的文件不构建手持）；没有该模块的旧固定源码沿用原路径规则，两个产品都视为受影响；不支持手持入口的更旧源码，手持运行只完成规划不构建。未受影响时只规划不编译。输入为已合并私库 main 第一父链的完整 `source_sha`；收银 `platform` 为 `all`、`windows`、`android` 或配合 `before_sha` 的 `changed`，手持为 `terminal-android` 或 `changed`。只接受通知机器人派发，见「派发者限制」。源码由既有 App 短期只读令牌静默读取，不向公开日志、仓库或 Artifact 上传源码及私库构建日志。

收银构建的首个失败输出仅独占暂存在 Runner 临时目录；POSIX 文件权限为 `0600`，Windows 使用任务隔离临时目录。失败时复用既有 CI App 的 `contents:read` 与 `checks:write` 短期令牌，仅向私库 `sdbcbt/CloudLinkPOS` 对应固定提交创建平台失败 CheckRun，不授予私库 contents 写入权限。报告会复核 plan、当前检出 SHA 和已读取 main 首父历史，替换四项签名 Secrets 与令牌原值后，仅保留最多 24KB 末尾；详情只在私库可见，不进入公开 Summary 或 Artifact。清理失败不会覆盖首个失败，任何回写错误也不输出日志、API 响应或凭据。

Release 标签指向公开 CI 的构建配置提交；安装包版本、附件名称、清单与软件内源码 SHA 对应私库固定提交。公开源码 ZIP 只包含 CI 配置，不包含客户端源码。附件上传草稿后核对远端 SHA-256，再公开发布；已有附件不同摘要即失败，绝不覆盖。

**手持 POS（terminal-android）**：只在 `build-terminal-release.yml` 的独立运行中构建，要求固定源码的 `dev/packaging/pos_release.py` 在 `PLATFORMS` 声明 `terminal-android` 且存在 `apps/pos/lib/main_terminal.dart`。该目标以 `-t lib/main_terminal.dart` 与 Gradle 属性 `CLP_APP_VARIANT=terminal` 构建 release APK，复用同一把正式签名，核验证书 SHA-256、`versionName`/`versionCode` 及包名 `com.cloudlinkpos.terminal`，发布为独立的永久 Release，标签 `terminal-android-v软件版本.源码SHA前12位`，附件为 `CloudLinkPOS-terminal-android-v版本-b构建号-SHA.apk`、`release-manifest.json`（含 `packageName` 与 `signerCertSHA256`）及 `SHA256SUMS.txt`。自动升级：固定源码的 `dev/packaging/build_android_upgrade.py` 在 `PRODUCTS` 声明 `terminal` 时，手持构建注入与横屏相同的 `POS_UPGRADE_PUBLIC_KEY`、`POS_UPGRADE_PERMIT_PUBLIC_KEY`（私库 `build_terminal_android.py build --upgrade-trust`），并用 `POS_RELEASE_SIGNING_KEY` 生成 `product=terminal`、`arch=arm` 的 `upgrade-release.json` 与 `upgrade-payload.apk`；未声明的旧固定源码仍只交付安装包。失败详情写入私库 `terminal-android-build-failure` 检查。联迪真机升级验收另行记录，构建成功不代表设备升级成功。

**手持 POS Android 5.1（API 22／armeabi-v7a）隔离工具链**：仅终端矩阵、且固定源码同时含有 `dev/packaging/terminal-android/toolchain.json` 与 `dev/packaging/build_terminal_android.py` 时启用（`pos-build.py toolchain` 输出 `api22`）；缺少新声明的旧固定源码继续走现有 Flutter 3.44 路径，Windows 与横屏 Android 不改工具链，两个入口工作流仍独立。

- 工具链：私库清单只能选择公开 CI 白名单中的 Flutter 3.32.8（框架/引擎修订、Dart 3.8.1 逐项一致），官方归档地址与 SHA-256（`c2c7d75f…ed8c`，来自 `releases_linux.json`）由公开 CI 持有；校验后解压到 Runner 临时目录的独立 SDK，再核对 `flutter --version --machine`。NDK `26.3.11579264` 只经 Android SDK 官方 `sdkmanager` 安装并核对 `Pkg.Revision`。包名、`minSdk=22`、`armeabi-v7a`、入口与 NDK 版本不符即停止。
- 测试：按手持专用锁文件在独立工作区 `pub get --enforce-lockfile` 后运行 `test/terminal`，不改 `apps/pos`。
- 构建：调用私库 `build_terminal_android.py build --workspace … --flutter <隔离 SDK> --key-properties <既有正式签名> --build-name <本轮版本> --build-number <本轮构建号> --source-sha <完整私库 SHA>`；源码 SHA 须为 40 位小写并等于本轮 `source_sha`。
- 核验：先用既有 apksigner 规则核对唯一正式证书，再调用私库 `verify --ndk`（包名、minSdk 22、armv7 原生库、v1 签名、API 22 原生符号），公开 CI 逐项复核证据中的 APK SHA-256、版本、证书与 v1，在 armv7 `libapp.so` 中核对本轮源码 SHA，并沿用 `verify_android_version.py` 与包名检查；之后照旧打包为 `terminal-android-v…` 永久 Release。公开日志只打印安装包本身可得的白名单摘要（APK SHA-256、包名、版本、minSdk、证书、v1）。
- 清理：隔离工作区里的 `key.properties` 副本按签名所有权标记的摘要删除，变更或链接即拒绝；无论成败都继续执行既有签名清理。失败输出仍只进 `0600` 首个失败日志并回写私库 `terminal-android-build-failure`。
- 发布顺序：先合并本公开 CI 变更（旧固定源码无工具链声明，行为不变），再合并私库带 `--source-sha` 的 #492；#492 合并后第一个手持运行即产出 API 22 正式包。`b4b6b96` 的构建脚本尚无 `--source-sha`，若以该提交构建会在 `android.compile` 明确失败，而不会产出缺少源码身份的安装包。
- 联迪真机验收：只使用本仓库 Releases 中 `terminal-android-v<版本>.<源码SHA前12位>` 的 `CloudLinkPOS-terminal-android-…apk`，以同一 Release 的 `SHA256SUMS.txt`、`release-manifest.json`（`sourceSHA`、`packageName`、`signerCertSHA256`）及运行日志中的「手持 API 22 安装包核验」摘要对照；在 A8（Android 5.1.1）上 `adb install`、冷启动、在“版本信息”核对完整源码 SHA 与构建号。不得使用本地或一次性密钥构建的 APK 作为验收证据。

离线测试：`python3 ci/pos-build_test.py` 中 `TerminalApi22Tests`、`Api22WorkflowTests` 覆盖模式判定、白名单、摘要/修订/NDK 不符、`--source-sha` 与版本参数、证据比对、源码身份嵌入、隔离测试/打包目录、签名副本清理与日志隔离。

Android 需要在本公开仓库配置固定正式签名的 `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_PASSWORD`、`ANDROID_KEY_ALIAS`。缺失时该平台明确失败，不生成临时签名或使用 debug 签名；Windows 独立继续。客户端使用单独的工作流运行，避免 Android 签名失败改变后端镜像收据的最终状态。构建成功不代表真机 UI、OTA 或外设验收。

**安卓 Gradle 依赖解析健壮性**（横屏收银 Android 与手持 POS 共用，只影响依赖解析与日志）：

- 缓存：安卓作业恢复 `~/.gradle/caches/modules-2` 的 `files-2.1`、`metadata-*` 与 `~/.gradle/wrapper/dists`。actions/cache 对目录整体归档、不能按文件名排除，因此在全部 Gradle 构建与发布之后、作业结束保存缓存之前，由「清理 Gradle 缓存中的锁与未完成下载」步骤删除这些目录下的 `*.lock`、`*.part`、`*.lck`。不缓存构建缓存、转换产物、守护进程等与本机路径相关的状态。key 为平台、工具链模式、`apps/pos/android` 下 Gradle 配置与 wrapper 版本哈希、`pubspec.lock` 哈希；只在同一配置哈希内回退，仓库/镜像顺序变化即不复用，镜像顺序始终由本轮固定源码的 `settings.gradle.kts` 决定。仅成功作业写回。
- 429 重试：三处 Gradle 构建命令（横屏 `flutter build apk`、手持默认路径、手持 API 22 私库构建脚本）仅在本次失败输出含 HTTP 429 时重试，最多 2 次，等待 `30s×2^n` 加 0–50% 抖动；重试前清理 `~/.gradle/.tmp/gradle_download*` 与 `*.part`，API 22 先按签名所有权标记清理隔离工作区。其他错误不重试。公开日志记录第几次重试、清理数量与等待秒数。
- 失败摘要：`android.compile` 失败时公开日志额外输出一行「Gradle 构建失败摘要（已脱敏）」，含公开仓库主机名（白名单外记为 `other`）、HTTP 状态码、失败任务/配置路径与重试次数。任务/配置路径每段须为以字母或下划线开头的 Gradle 标识符（点号、数字开头及 IP 形态丢弃），并排除含环境密钥值的项；形如合法标识符的名称仍会原样公开，依赖 Gradle 任务名与配置名本身不承载敏感信息。摘要不含令牌、账号、内网地址、私库地址与输出原文。私库 `*-build-failure` 检查的详细内容不变。
- 回滚（每项可单独回滚，同一项内先回滚后加的修正提交）：缓存 `git revert 167095d 01832af`；重试 `git revert 5ca4649 36a44e1`（单独回滚 `36a44e1` 会与 `5ca4649` 冲突）；摘要 `git revert a2613ac e3d08a9`。squash 合并后只能整体回滚。

离线边界检查：安装固定 `PyYAML==6.0.2` 后分别运行 `python3 ci/pos-build_test.py` 与 `python3 ci/report-pos-failure_test.py`。测试覆盖触发范围、main 限制、版本元数据白名单、私库日志隔离、失败详情脱敏及工作流权限。

### PR 质量检查的基准前移

私库 PR 检查绑定通知中的 head 提交：head 不同即拒绝，新推送会重新通知。main 前移（包括生产发布 bot 只改清单的提交）不再使整组检查失败：`ci/quality-context.py` 采用 PR 当前 base，并核对它由通知 base 前移而来、仍在 main 首父历史中（通知 base 同样须在首父链上），改动范围也按当前 base 计算；各作业按固定 base/head 在执行器本地生成合并提交（固定作者与时间，结果一致），不依赖 GitHub 异步计算的 `refs/pull/N/merge`。与当前 main 冲突时拒绝检查，需先合并 main。离线测试：`python3 ci/quality-context_test.py`。

### Go 安装源故障兜底

质量检查与主线构建先用 `actions/setup-go` 安装 Go；该步骤失败（例如 GitHub go-versions 发行包返回 504、回退的 storage.googleapis.com 返回 403）时，由 `ci/setup-go-fallback.sh` 使用 Runner 预装的 Go，以 `GOTOOLCHAIN=go<版本>` 经 `proxy.golang.org` 下载官方工具链，并由 `sum.golang.org` 校验摘要，然后核对实际版本。兜底不使用 direct 下载，也不关闭校验。

后台安装或编译失败的输出仅保存在权限为 `0600` 的执行器临时文件，随后使用独立短期检查令牌回写私库固定提交的 `admin-build-failure`；公开日志和 Artifact 不包含私库编译输出。回写前核对实际检出提交与 main 首父链，脱敏后最多保留 24KB。

### GHCR 与 CF/R2 单服务隔离试点

`registry-pilot.yml` 固定在 GitHub 托管 Runner 构建 `employee-api`，使用独立私有 GHCR 包 `sdbcbt/cloudlinkpos-registry-pilot` 和不可变试点标签。它不推送正式 ACR、`latest` 或生产收据，也不更新 Fleet。

派发参数 `source_sha` 必须是已合并私库主线的完整 SHA；`mirror_to_cf=false` 只验证 GHCR，`true` 时以短期 GitHub 令牌读取同一镜像并预热 CF/R2。后者使用 GitHub OIDC 短期身份，只允许固定仓库、main、指定工作流和通知机器人调用固定试点域名的受控发布接口，无需添加长期发布 Secret。公开 Artifact 仅保存版本、摘要、私有访问证明和分阶段耗时。

R2 使用同账号独立桶，初始预算为十进制 256 MB；Worker 的只读凭据与发布凭据分开。为避免长期保存 GHCR 凭据，CF 缓存未命中时不自动回源写入；需要通过授权流程重新同步。容量清理、当前版本及固定摘要保护由隔离协调对象执行，客户端响应禁止共享 HTTP 缓存，边缘缓存命中前仍鉴权。

离线检查：`python3 ci/registry-pilot_test.py`、`python3 ci/registry-pilot-mirror_test.py`。Workers 与 R2 的部署、国内测速、费用和验收记录维护在私库 `docs/06-部署运维/GHCR与Cloudflare镜像试点.md`；代码和离线测试通过不表示真实部署完成。

镜像同步与测速客户端发送明确应用 User-Agent，以兼容 Cloudflare Browser Integrity Check。
