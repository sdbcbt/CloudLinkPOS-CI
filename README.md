
### 主线构建与全局发布模式

`private-main.yml` 调用 `full-version-build.yml`，按私库推送前后提交识别受影响服务。构建只推送版本号和 latest，不生成 SHA 标签。每个成功服务的私有访问证据与整批计划核对后，由 `ci/publish-receipts.py` 将白名单元数据写入 `source-build-receipts` 分支；源码、日志和凭据不进入该分支。收据文件存在不代表工作流已结束，生产发布服务还必须核对具体 run/attempt 的最终成功结果。

生产后台的全局发布模式保存在私库 Fleet 清单的 `releasePolicy`。关闭时，生产服务核验本批构建成功、策略代次一致、正式版本就绪及源码未倒退后，使用生产专用 GitHub App 更新私库清单，由 Fleet 发布；开启时保留待灰度版本，公开 CI 不能绕过。进行中的任务、候选基础设施及灰度页面不会因关闭开关被删除。公开 CI 不持有私库写入凭据。

### 构建失败与重跑

当前发布收据严格限定同一 run/attempt 的完整服务批次。失败后使用 GitHub 的 **Re-run all jobs**，不支持 **Re-run failed jobs**：后者缺少当前 attempt 的完整计划和收据，会明确拒绝生成发布目录。计划和每个服务制品名包含 attempt，整批重跑不会覆盖旧制品，也不会把上次成功服务伪装成本次执行。跨 attempt 合并凭证暂不在本轮范围；普通修复代码后的新 main 推送照常自动构建。
