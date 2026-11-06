
### 主线构建与全局发布模式

`private-main.yml` 调用 `full-version-build.yml`，按私库推送前后提交识别受影响服务。构建只推送版本号和 latest，不生成 SHA 标签。每个成功服务的私有访问证据与整批计划核对后，由 `ci/publish-receipts.py` 将白名单元数据写入 `source-build-receipts` 分支；源码、日志和凭据不进入该分支。收据文件存在不代表工作流已结束，生产发布服务还必须核对具体 run/attempt 的最终成功结果。

生产后台的全局发布模式保存在私库 Fleet 清单的 `releasePolicy`。关闭时，生产服务核验本批构建成功、策略代次一致、正式版本就绪及源码未倒退后，使用生产专用 GitHub App 更新私库清单，由 Fleet 发布；开启时保留待灰度版本，公开 CI 不能绕过。进行中的任务、候选基础设施及灰度页面不会因关闭开关被删除。公开 CI 不持有私库写入凭据。

### 构建失败与重跑

当前发布收据严格限定同一 run/attempt 的完整服务批次。失败后使用 GitHub 的 **Re-run all jobs**，不支持 **Re-run failed jobs**：后者缺少当前 attempt 的完整计划和收据，会明确拒绝生成发布目录。计划和每个服务制品名包含 attempt，整批重跑不会覆盖旧制品，也不会把上次成功服务伪装成本次执行。跨 attempt 合并凭证暂不在本轮范围；普通修复代码后的新 main 推送照常自动构建。

连续合并的构建使用 `queue: max` 保留排队任务（上限 100），避免 GitHub 默认只保留一个 pending 运行而丢掉中间推送范围。工作流仍串行进入版本发布阶段，不取消在途构建。


### 收银客户端永久下载

Windows / Android 在本公开仓库的 [收银客户端永久版本构建](https://github.com/sdbcbt/CloudLinkPOS-CI/actions/workflows/build-pos-release.yml) 工作流执行，并将安装包、`release-manifest.json` 和 `SHA256SUMS.txt` 发布到 [GitHub Releases](https://github.com/sdbcbt/CloudLinkPOS-CI/releases)。Windows 同时保留中文安装向导与便携 ZIP；下载附件不依赖 Actions Artifact 保留期限。历史私库 Releases 不删除、不覆盖。

私库 main 推送由现有 Cloudflare Worker 单独派发客户端任务，工作流按 before/after 识别 POS 与打包路径；无客户端改动不编译。手动执行输入已合并私库 main 第一父链的完整 `source_sha`，选择 `all`、`windows` 或 `android`，`before_sha` 留空；自动范围重放选择 `changed`，同时填写已核对的 `before_sha`。源码由既有 App 短期只读令牌静默读取，不上传源码或私库构建日志。

Release 标签指向公开 CI 的构建配置提交；安装包版本、附件名称、清单与软件内源码 SHA 对应私库固定提交。公开源码 ZIP 只包含 CI 配置，不包含客户端源码。附件上传草稿后核对远端 SHA-256，再公开发布；已有附件不同摘要即失败，绝不覆盖。

Android 需要在本公开仓库配置固定正式签名的 `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_PASSWORD`、`ANDROID_KEY_ALIAS`。缺失时该平台明确失败，不生成临时签名或使用 debug 签名；Windows 独立继续。客户端使用单独的工作流运行，避免 Android 签名失败改变后端镜像收据的最终状态。构建成功不代表真机 UI、OTA 或外设验收。

离线边界检查：安装固定 `PyYAML==6.0.2` 后运行 `python3 ci/pos-build_test.py`。测试覆盖触发范围、main 限制、版本元数据白名单、私库日志隔离及工作流权限。
