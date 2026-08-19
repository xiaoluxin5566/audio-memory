# Audio Memory 开发与发布规则

本仓库的状态文件和统一脚本是流程权威，不依赖对话记忆。用户已安装版本在新版本正式发布前继续锁定为 `v0.1.0-beta.2`。

## 功能开发

- 新功能必须从干净的 `main` 运行 `./scripts/feature-start.sh <feature_id>`，由脚本创建 `codex/<feature_id>` 和独立 worktree。
- 跨对话继续同一功能时，先运行 `./scripts/feature-status.sh <feature_id>`，不新建分支。
- 开发页面固定为 `5173`，开发后端固定为 `8766`。两个端口是同一套开发环境的前后端，由功能运行所有权记录统一管理。
- 不得读写正式环境 `8765`的运行根、数据库或密钥。

## 完成与集成

- 功能仅能用 `./scripts/feature-finish.sh <feature_id>` 完成验收。统一门禁 `scripts/quality-gate.sh` 必须通过 `backend`、`frontend`、`browser` 和 `runtime-isolation`。
- 验收后的任何新提交都会使证据失效，必须重新验收。
- `./scripts/release-prepare.sh <version> <feature_id>...` 只生成候选清单，不合并。
- 只有用户明确确认候选摘要后，才可运行 `release-integrate.sh`。功能顺序合并，首个冲突或失败立即停止，不处理后续功能。

## 发布与清理

- 集成授权不等于发布授权。只有用户再次明确确认发布后，才可运行 `./scripts/release-build.sh <version> --approve <candidate_digest>`。
- 发布必须位于干净 `main`，且版本号、候选清单、集成回执和当前提交完全一致。已存在的版本标签不得覆盖。
- 没有用户逐项核对和明确授权，不得删除任何功能分支或 worktree。发布命令也不自动清理它们。
