
### 主线构建与全局发布模式

`private-main.yml` 调用 `full-version-build.yml`，按私库推送前后提交识别受影响服务。构建只推送版本号和 latest，不生成 SHA 标签。每个成功服务的私有访问证据与整批计划核对后，由 `ci/publish-receipts.py` 将白名单元数据写入 `source-build-receipts` 分支；源码、日志和凭据不进入该分支。收据文件存在不代表工作流已结束，生产发布服务还必须核对具体 run/attempt 的最终成功结果。

生产后台的全局发布模式保存在私库 Fleet 清单的 `releasePolicy`。受管清单迁到 Fleet 专用部署分支 `fleet-deploy-do-not-delete`（该分支严禁删除）后，主线清单冻结；构建计划同时读取主线与部署分支两份清单，取 `generation` 较高的策略，迁移前后都无需额外开关。关闭时，生产服务核验本批构建成功、策略代次一致、正式版本就绪及源码未倒退后，使用生产专用 GitHub App 更新私库清单，由 Fleet 发布；开启时保留待灰度版本，公开 CI 不能绕过。进行中的任务、候选基础设施及灰度页面不会因关闭开关被删除。公开 CI 不持有私库源码或部署清单写入凭据。

### 构建失败与重跑

当前发布收据严格限定同一 run/attempt 的完整服务批次。失败后使用 GitHub 的 **Re-run all jobs**，不支持 **Re-run failed jobs**：后者缺少当前 attempt 的完整计划和收据，会明确拒绝生成发布目录。计划和每个服务制品名包含 attempt，整批重跑不会覆盖旧制品，也不会把上次成功服务伪装成本次执行。跨 attempt 合并凭证暂不在本轮范围；普通修复代码后的新 main 推送照常自动构建。

连续合并的构建使用 `queue: max` 保留排队任务（上限 100），避免 GitHub 默认只保留一个 pending 运行而丢掉中间推送范围。工作流仍串行进入版本发布阶段，不取消在途构建。


### 收银客户端永久下载

Windows / Android 在本公开仓库的 [收银客户端永久版本构建](https://github.com/sdbcbt/CloudLinkPOS-CI/actions/workflows/build-pos-release.yml) 工作流执行，并将安装包、`release-manifest.json` 和 `SHA256SUMS.txt` 发布到 [GitHub Releases](https://github.com/sdbcbt/CloudLinkPOS-CI/releases)。Windows 同时保留中文安装向导与便携 ZIP；下载附件不依赖 Actions Artifact 保留期限。历史私库 Releases 不删除、不覆盖。

私库 main 推送由现有 Cloudflare Worker 单独派发客户端任务，工作流按 before/after 识别 POS 与打包路径；无客户端改动不编译。手动执行输入已合并私库 main 第一父链的完整 `source_sha`，选择 `all`、`windows` 或 `android`，`before_sha` 留空；自动范围重放选择 `changed`，同时填写已核对的 `before_sha`。源码由既有 App 短期只读令牌静默读取，不向公开日志、仓库或 Artifact 上传源码及私库构建日志。

收银构建的首个失败输出仅独占暂存在 Runner 临时目录；POSIX 文件权限为 `0600`，Windows 使用任务隔离临时目录。失败时复用既有 CI App 的 `contents:read` 与 `checks:write` 短期令牌，仅向私库 `sdbcbt/CloudLinkPOS` 对应固定提交创建平台失败 CheckRun，不授予私库 contents 写入权限。报告会复核 plan、当前检出 SHA 和已读取 main 首父历史，替换四项签名 Secrets 与令牌原值后，仅保留最多 24KB 末尾；详情只在私库可见，不进入公开 Summary 或 Artifact。清理失败不会覆盖首个失败，任何回写错误也不输出日志、API 响应或凭据。

Release 标签指向公开 CI 的构建配置提交；安装包版本、附件名称、清单与软件内源码 SHA 对应私库固定提交。公开源码 ZIP 只包含 CI 配置，不包含客户端源码。附件上传草稿后核对远端 SHA-256，再公开发布；已有附件不同摘要即失败，绝不覆盖。

Android 需要在本公开仓库配置固定正式签名的 `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_PASSWORD`、`ANDROID_KEY_ALIAS`。缺失时该平台明确失败，不生成临时签名或使用 debug 签名；Windows 独立继续。客户端使用单独的工作流运行，避免 Android 签名失败改变后端镜像收据的最终状态。构建成功不代表真机 UI、OTA 或外设验收。

离线边界检查：安装固定 `PyYAML==6.0.2` 后分别运行 `python3 ci/pos-build_test.py` 与 `python3 ci/report-pos-failure_test.py`。测试覆盖触发范围、main 限制、版本元数据白名单、私库日志隔离、失败详情脱敏及工作流权限。

### PR 质量检查的基准前移

私库 PR 检查绑定通知中的 head 提交：head 不同即拒绝，新推送会重新通知。main 前移（包括生产发布 bot 只改清单的提交）不再使整组检查失败：`ci/quality-context.py` 采用 PR 当前 base，并核对它由通知 base 前移而来、仍在 main 首父历史中（通知 base 同样须在首父链上），改动范围也按当前 base 计算；各作业按固定 base/head 在执行器本地生成合并提交（固定作者与时间，结果一致），不依赖 GitHub 异步计算的 `refs/pull/N/merge`。与当前 main 冲突时拒绝检查，需先合并 main。离线测试：`python3 ci/quality-context_test.py`。

### Go 安装源故障兜底

质量检查与主线构建先用 `actions/setup-go` 安装 Go；该步骤失败（例如 GitHub go-versions 发行包返回 504、回退的 storage.googleapis.com 返回 403）时，由 `ci/setup-go-fallback.sh` 使用 Runner 预装的 Go，以 `GOTOOLCHAIN=go<版本>` 经 `proxy.golang.org` 下载官方工具链，并由 `sum.golang.org` 校验摘要，然后核对实际版本。兜底不使用 direct 下载，也不关闭校验。
