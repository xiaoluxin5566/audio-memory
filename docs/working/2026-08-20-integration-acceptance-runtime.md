# 版本集成验收环境验收记录

## 实现结果

- 新增 `integration-start.sh`、`integration-status.sh`、`integration-stop.sh`。
- 启动时强制使用干净且与 `refs/heads/main` 完全一致的 main worktree。
- 当 `5173/8766` 属于其他功能时，先查询活动任务；有任务拒绝切换，空闲时才验证并停止旧所有者。
- 验收运行时绑定版本和 commit，页面显示如 `v0.1.0-beta.3 集成验收` 的标签。
- 保留原功能运行时所有者格式，不会使已运行的旧环境记录失效。

## 自动化证据

- 后端全量：`1165 passed, 28 skipped`。
- 前端全量：`98 passed`，生产构建成功。
- 浏览器端到端：`29 passed`。
- 运行时门禁：`18 passed`。
- 开发/正式环境隔离聚焦回归：`2 passed`。
- Python 语法、Shell 语法和 `git diff --check` 通过。

## 待合并后完成

- 从最新 main 执行 `./scripts/integration-start.sh v0.1.0-beta.3`。
- 检查 `/api/health` 的 `environment_label`、状态命令的 commit 与页面左上角标签。

