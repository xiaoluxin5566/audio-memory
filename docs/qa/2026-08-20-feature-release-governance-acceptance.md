# 功能轨道与版本发布治理验收记录

## 验收范围

本次只验收开发流程管控，不合并 `main`，不发布、不安装或替换用户正在使用的 `v0.1.0-beta.2`。

## 已完成的自动验收

- 临时 Git 仓库中可并行创建两个以上的功能分支和 worktree。
- 开发记录位于 Git 共享目录，新对话可由 `feature-status` 恢复，不依赖对话记忆。
- 完成门禁只记录干净 worktree 中经过测试的精确提交；新提交会自动使验收证据失效。
- 没有候选摘要精确授权时，集成对 `main` 零写入。
- 顺序集成在第二项门禁失败或冲突时立即停止：保留第一项已通过结果，撤回失败项，不处理第三项。
- 发布需要第二次独立授权，并拒绝脏 `main`、版本不一致、过期集成回执和已存在标签。
- 发布包白名单防止 `audio-memory-governance`、`.worktrees`、`.runtime` 及用户数据进入安装包。

## 完整回归与实际运行身份

2026-08-20 使用统一 `scripts/quality-gate.sh` 执行全量回归：

- `backend`：1137 项通过，28 项旧兼容流程按设计跳过。
- `frontend`：97 项通过，Vite 生产构建成功。
- `browser`：26 项真实浏览器流程通过。
- `runtime-isolation`：5 项通过。

首次在受限沙箱中运行后端和前端时，仅因沙箱禁止回环端口绑定而失败；获得仅限测试绑定权限后原样重跑全部通过，未修改测试期望。

## 当前 beta3 轨道纳管与开发身份

2026-08-20 先生成只读预览，用户核对后以摘要
`fece0681eac7fb7b0f88936770e2f699b0e3b8034df17366a548007fa00b551a`
明确确认纳管。写入后重新校验结果为：

- 功能：`beta3-stability`。
- 分支：`codex/beta3-stability`。
- worktree：`.worktrees/beta3-stability`。
- 目标版本：`v0.1.0-beta.3`。
- 状态：`in_progress`，记录、分支和 worktree 一致。

首次切换到新运行所有权管理器时，安全检查拒绝覆盖旧的未登记进程。只读核对确认旧 5173/8766 进程均来自同一 `beta3-stability` worktree 后，仅停止该组开发进程并由新管理器重启。

重启后实际身份验收：

- `http://127.0.0.1:5173/api/health` 和 `http://127.0.0.1:8766/api/health` 均返回 `status=ok` 与 `profile=development`。
- 运行所有者为 `beta3-stability`，记录的 worktree 与当前目录完全一致，阶段为 `ready`。
- 5173 仅作为用户打开的开发页面，8766 是其同一套 development 后端。
- 8765 检查时无监听进程；本次未启动、停止或修改 beta.2 正式服务。
