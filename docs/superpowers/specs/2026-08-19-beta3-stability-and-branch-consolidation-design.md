# Audio Memory beta.3 稳定性与分支收敛设计

## 背景

`v0.1.0-beta.2` 是当前用户使用的本地版本，必须保持不变。后续开发以 `main` 为唯一集成基线，所有新功能从 `main` 创建短生命周期分支，完成验证后合并回 `main`，最终从 `main` 构建 `v0.1.0-beta.3`。

2026-08-19 的三分钟音频任务暴露了一个状态一致性问题：转写完成后，`analysis_jobs.stage` 先被写为 `analyzing`，但持久化的 `analysis_versions` 队列记录没有创建。页面因此长期显示 DeepSeek 正在分析，实际从未进入模型请求。旧版本没有记录分析入队各阶段，且 SQLite 使用 `DELETE` journal 模式、未设置 `busy_timeout`，历史日志中已出现真实的 `database is locked`。

## 目标

1. 建立唯一、可执行的 beta 开发与发布流程。
2. 保留正式与开发运行边界，但开发服务只在需要时启动。
3. 审计所有未合并分支，迁移仍有价值的成果，然后删除旧分支与工作树。
4. 从状态机、SQLite 锁策略、恢复机制和日志可观测性四个层面根治“转写完成但分析不开始”。

## 非目标

- 不修改或重新发布 `v0.1.0-beta.2`。
- 不在未授权时读取正式 Keychain 密钥或发起真实模型请求。
- 不把长期分叉分支整体合并回 `main`。
- 不为了代码整洁而删除未审计、未迁移的唯一成果。

## 统一开发与发布流程

1. 用户继续使用已安装的 `v0.1.0-beta.2`。
2. `main` 代表下一候选版本的唯一集成基线。
3. 每项工作从最新 `main` 创建 `codex/<feature>` 分支和独立工作树。
4. 开发过程只使用 development profile、端口 `8766` 和独立数据根。
5. 功能分支必须经过失败测试、最小实现、相关回归、完整测试与代码审查。
6. 验证通过后合并回 `main`，删除已合并功能分支及其工作树。
7. 从 `main` 构建 `v0.1.0-beta.3` 候选包，完成安装、升级、回滚、数据保留和正式路径验证。
8. 只有在用户明确批准发布后，才创建标签、推送并升级正式环境。

## 运行环境边界

### 正式环境

- profile: `production`
- 端口：`127.0.0.1:8765`
- 数据根：`~/Library/Application Support/AudioMemory`
- Keychain service：正式专用名称
- 版本：已安装、不可变的 Release 目录
- 用途：用户日常使用和发布后最终验收

### 开发环境

- profile: `development`
- 端口：`127.0.0.1:8766`
- 数据根：当前工作树内 `.runtime/dev`
- Keychain service：开发专用名称
- 版本：当前功能分支
- 用途：开发和人工验证；用完关闭，不需要常驻

两个环境可以同时运行，但必须在解析 macOS 符号链接、大小写和 `/System/Volumes/Data` 别名后，依然确保数据根不重叠。数据库、锁文件、暂存、音频、Prompt、反馈、日志、端口和 Keychain 必须全部分离。

## 旧分支审计与删除

当前已知未被 `main` 完全包含的分支包括：

- `codex/report-audit-revision-pipeline`
- `codex/analysis-sleep-prevention`
- `codex/smooth-progress`
- `codex/dev-prod-isolation`
- `codex/cloud-asr-evaluation`

每个分支必须输出一条审计结论：

1. **已在 main 等价实现**：无需迁移，可删除。
2. **beta.3 仍需要**：按功能从当前 `main` 重新迁移，独立测试和提交，不整分支合并。
3. **仅有评测或调研价值**：保留必要的结论、证据和复现方法，不保留过时生产代码。
4. **过时或与当前产品方向冲突**：记录删除理由后删除。

审计时同时检查已提交的独有提交和未提交工作树。在价值迁移、新基线测试与审查全部通过之前，不删除原分支。删除顺序为：确认迁移完成 → 移除工作树 → 删除本地分支 → 必要时删除远程分支。Release 标签不在清理范围内。

## 分析交接状态机

核心不变量：

> `analysis_jobs.stage = analyzing` 时，必须存在属于该任务的持久化 `analysis_versions` 记录，其状态为 `pending` 或 `running`。

正常交接顺序：

1. 转写和风险门完成，转写结果已持久化。
2. 在一个有界的数据库写事务中创建 `analysis_versions(pending)`，并同时把 `analysis_jobs.stage` 改为 `analyzing`。
3. 事务提交后通知分析工作线程。即使调用取消恰好发生在 commit 后，通知也必须执行。
4. 工作线程以原子更新领取任务，写入 owner 和 lease。
5. 模型请求开始后才显示实际模型阶段；界面文案不得只根据预先配置的 provider 推断。

入队失败时：

- 不删除或重写已完成的转写。
- 任务进入 `failed/model_analysis_failed` 或更具体的可重试错误状态。
- 解除防休眠引用。
- 页面明确显示“分析未开始，可重试”，不继续显示模型正在处理。

## SQLite 并发与锁策略

1. 启动迁移后显式设置并验证 `PRAGMA journal_mode=WAL`。
2. 每个连接设置 `PRAGMA busy_timeout=5000`，不允许使用默认的零等待。
3. 保持 `PRAGMA foreign_keys=ON`，并使用 `synchronous=NORMAL` 配合 WAL。
4. 分析入队使用短小的 `BEGIN IMMEDIATE` 事务，不在事务内读文件、构造 Prompt 或执行网络请求。
5. 仅对 SQLite `BUSY/LOCKED` 执行有界重试；重试总时间不超过交接超时预算，不吞掉结构、约束或程序错误。
6. 增加真实多连接竞争测试，验证转写写入、队列入队、前端轮询和历史重分析并发时不会产生孤儿状态。

## 结构化运行日志

使用一行一事件的结构化 JSON 日志。所有分析交接事件至少包含：

- `timestamp`
- `event`
- `job_id`
- `analysis_version_id`（创建后）
- `provider_id`
- `model_id`
- `elapsed_ms`
- `status`
- `error_type`（失败时）
- `queue_owner_id` 与 `lease_expires_at`（领取后）

标准事件包括：

- `transcription.completed`
- `analysis.enqueue.started`
- `analysis.enqueue.lock_acquired`
- `analysis.enqueue.transaction_started`
- `analysis.enqueue.committed`
- `analysis.enqueue.worker_notified`
- `analysis.worker.claimed`
- `analysis.provider.request_started`
- `analysis.provider.request_finished`
- `analysis.job.failed`
- `analysis.recovery.reconciled`

日志不得包含音频、逐字稿、Prompt 正文、用户画像、模型输入/输出、API Key 或 Keychain 内容。允许记录长度、计数、耗时、非敏感 ID 和归一化错误类型。

`audio-memory doctor` 增加只读的队列一致性检查，报告但不自动修改：

- `analyzing` 但无活跃分析版本；
- 过期 lease 的 `running` 分析版本；
- 存在 `pending` 但长时间未被领取；
- job/version 错误状态不一致；
- SQLite journal mode 或 busy timeout 不符合要求。

## 启动恢复

服务启动时在工作线程开始前执行有界一致性恢复：

- `analyzing` 且没有 `pending/running` 版本：改为可重试分析失败。
- `running` 且 lease 已过期：清理 owner，恢复为 `pending`。
- `pending` 版本：启动完成后主动通知工作线程。
- 已完成转写永久保留，不触发重新转写。
- 每次恢复都记录 `analysis.recovery.reconciled`，包含修复类型和影响行数。

## 测试与验收

### 自动化测试

- 入队成功时 job 和 version 在同一事务中进入一致状态。
- 入队异常、超时和 SQLite 锁竞争不会留下假 `analyzing`。
- commit 后取消仍会唤醒工作线程。
- 失败、超时和取消正确解除防休眠引用。
- 转写段在所有分析入队失败路径中保持不变。
- 启动恢复能修复孤儿 job、过期 lease 和未通知 pending version。
- 日志事件顺序、字段和脱敏规则有自动化测试。
- 正式和开发服务同时运行时，数据、锁、日志、Keychain 和停止操作互不影响。

### beta.3 候选版验收

1. 从干净 `main` 构建自包含安装包。
2. 在隔离 HOME 中完成全新安装。
3. 在隔离 HOME 中从 beta.2 升级到 beta.3，并验证数据库备份和历史保留。
4. 同时启动正式 `8765` 和开发 `8766`，完成跨环境隔离验收。
5. 用受控假 provider 完成转写 → 入队 → worker 领取 → provider 开始 → 报告发布的端到端测试。
6. 真实 DeepSeek 验证需要用户单独授权；默认不执行。
7. 手工检查 `/api/health`、任务状态、分析版本、结构化日志、最终 Feed 和实际报告。

## 完成标准

- 所有未合并分支有明确的保留、迁移或删除结论。
- 所有保留成果已基于当前 `main` 重新验证并合并。
- 已审计的过时工作树和分支已删除，Release 标签保留。
- 界面不能出现无持久化分析版本支撑的“分析中”。
- SQLite 锁竞争有明确等待、重试、失败和日志行为。
- 转写已完成时，任何入队故障都不会丢失转写或重新转写。
- beta.3 安装包、升级、环境隔离和端到端发布路径均有可重复证据。
