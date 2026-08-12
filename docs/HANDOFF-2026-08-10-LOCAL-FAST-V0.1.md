# Local Fast V0.1 交接文档

**交接日期：** 2026-08-10  
**目标分支：** `codex/local-fast-v0-1`  
**worktree：** `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1`  
**当前 HEAD：** `57afe27c8276d54e0c7cdbbc7ee61873016448fc`  
**冻结 main：** `24e086786896f48f7dc0bcd0630376ee3d0054f8`

## 1. 新窗口目标

分两阶段完成 V0.1，顺序不可颠倒。

### 阶段一：先让现有真实任务在页面产出结果

1. 压缩 DeepSeek 事件地图输入。
2. 服务端本地补全 `unassigned_segment_ids`。
3. 保留具体、安全的错误码和 `finish_reason`。
4. 显式关闭 DeepSeek thinking，并设置有界输出参数。
5. 直接复用已经冻结的 3,442 个可靠句段重新分析。
6. 成功后在浏览器打开产品页面，让用户检查卡片、待办和证据回听入口。

阶段一不得重新执行 VAD、Whisper、风险门或说话人识别。

### 阶段二：完成本地 Local Fast V0.1

1. 用有界 compact 合批替代大量短 Whisper 调用。
2. 关闭本地说话人识别和全部二次 Whisper 复核。
3. 软风险保留第一遍文本并降权。
4. 修复 compact 时间映射、边界校验和冲突保留。
5. 记录每个阶段耗时。
6. 用同一份 3 小时 31 分钟音频运行一次完整全链路验收。

本地转写目标为 35–45 分钟。低于 35 分钟仍须通过质量门；高于 45 分钟判性能失败。

## 2. 必须先读

按顺序完整阅读：

1. `docs/LOCAL-FAST-V0.1-PARAMETER-BASELINE.md`
2. `docs/superpowers/specs/2026-08-10-local-fast-v0-1-design.md`
3. `docs/benchmark-evidence/2026-08-10-local-fast-v0-acceptance.md`
4. 本交接文档

参数基准文档是首轮参数唯一来源。不要复制出第二套默认值；真实运行必须记录参数指纹。

## 3. 已完成状态

- 已创建隔离 worktree 和分支。
- 已将 V0.1 参数单独冻结到参数基准文档。
- 已完成并提交两阶段技术设计：`57afe27 docs: define local fast v0.1 baseline`。
- 隔离 worktree 后端基线：`543 passed in 17.90s`。
- 没有修改生产代码、合并实验分支、调用 DeepSeek 或重跑真实音频。

## 4. 新窗口第一步

用户明确要求“先写实施计划，再改代码”。先用 writing-plans 流程写两个可独立验收的计划：

- `docs/superpowers/plans/2026-08-10-local-fast-v0-1-deepseek.md`
- `docs/superpowers/plans/2026-08-10-local-fast-v0-1-transcription.md`

阶段一计划完成后立即按测试驱动执行。阶段一成功后先停下来展示页面，再继续阶段二。

## 5. 当前真实任务证据

- source job ID：`d29475e4-f148-4b99-9b7e-1e5751da1e48`
- 失败 analysis version：`c65e86d7-5dc7-401f-90e0-96d92b01e866`
- 可靠句段：3,442
- 全部句段：4,117
- 丢弃：675
- 纯文本：28,470 字符
- 当前 event-map 结构化输入：约 1,014,131 bytes
- compact 句段投影估算：约 277,543 bytes
- 数据库：`/Users/liujinxin/Library/Application Support/AudioMemory/audio-memory.sqlite3`

允许只读聚合查询。禁止把原音频路径、转写正文、API Key、完整模型请求或完整响应写入文档、日志和 fixture。

## 6. DeepSeek 已知问题

- Provider 在运行前通过轻量配置校验。
- 事件地图未持久化，`staged_results_json` 为空。
- 六场景没有开始，页面最终零卡片、零待办。
- 协调器可能把底层异常归一为 `model_analysis_failed`，所以现有数据库不能区分网络超时、输出截断、Schema 错误或覆盖缺失。
- 每个句段重复发送文件元数据和可靠性字段。
- 模型必须输出全部 3,442 个已分配或未分配 ID。
- repair 会再次发送无效输出。
- 正式 DeepSeek V4 请求没有显式关闭默认 thinking。
- 没有显式 `max_tokens`，也没有读取 `finish_reason`。

不要用盲目增加超时或原样重试 1 MB 请求作为修复。

## 7. 阶段一冻结方案

### 7.1 输入与覆盖

文件元数据每个文件只发一次；句段只发 `id/start_ms/end_ms/text`，正文必须保留。

- `known_ids`：全部可靠句段 ID。
- `assigned_ids`：所有事件证据 ID 的并集。
- `unknown_ids = assigned_ids - known_ids`。
- `unassigned_ids = known_ids - assigned_ids`。
- `unknown_ids` 非空时失败为 `event_map_unknown_segment`。
- 否则服务端用 `unassigned_ids` 覆盖模型返回值，再做完整校验和持久化。

### 7.2 DeepSeek 参数

- model：`deepseek-v4-flash`
- thinking：`disabled`
- temperature：`0`
- response format：`json_object`
- event map：`max_tokens=32768`，timeout 180 秒
- scene：`max_tokens=16384`，timeout 120 秒
- profile：`max_tokens=8192`，timeout 120 秒
- 网络错误最多额外重试 1 次
- Schema/JSON 最多修复 1 次
- 六场景串行

### 7.3 错误码与安全诊断

至少区分：`network_timeout`、`authentication_failed`、`insufficient_balance`、`rate_limited`、`provider_unavailable`、`content_rejected`、`model_response_invalid`、`model_output_truncated`、`event_map_schema_invalid`、`event_map_unknown_segment`、`event_map_coverage_invalid`。

只记录 scene、request/response bytes、segment count、token usage、elapsed、status category、finish reason、repair attempted 和 coverage counts，不记录正文。

### 7.4 预计文件范围

- `backend/src/audio_memory/analysis/provider.py`
- `backend/src/audio_memory/analysis/events.py`
- `backend/src/audio_memory/analysis/runner.py`
- `backend/src/audio_memory/analysis/task_coordinator.py`
- `backend/src/audio_memory/analysis/parser.py`
- `backend/src/audio_memory/prompts/composer.py`
- `backend/src/audio_memory/prompts/event-map.md`
- `backend/src/audio_memory/prompts/event_schema.py`
- 对应 unit/integration tests

实施计划必须重新核对具体行号。坚持最小修改，不顺便重构六场景 Schema 或发布器。

## 8. 阶段一测试和页面验收

测试先行：

1. 正式请求包含 `thinking.disabled`、场景级 `max_tokens` 和正确超时。
2. adapter 读取 `finish_reason`，`length` 映射为 `model_output_truncated`。
3. event-map projection 不重复文件元数据、不删正文。
4. 模型省略未归类 ID 时服务端完整补全。
5. 模型引用未知 ID 时拒绝。
6. Provider、Schema、coverage 错误不会被协调器覆盖。
7. 失败不发布半成品，成功完成事件地图、六场景、画像和原子发布。

自动测试通过后，直接复用 source job 创建新 analysis version 或走产品“重新分析”入口。完成后用应用内浏览器打开产品页面，检查卡片、待办、证据回听、错误状态和阶段进度，并记录 token、调用次数和各分析阶段耗时。

当前 `127.0.0.1:8765` 有一个从原始 V0 workspace 启动的 Python 服务。展示前确认监听进程 cwd；若仍指向 `/Users/liujinxin/Documents/音频Always on Demo/backend`，则安全停止旧服务，再从 V0.1 worktree 启动，避免误验收旧代码。

## 9. 阶段二关键参数

- VAD：threshold `0.2`，min speech `0.25s`，min silence `0.25s`，padding `500ms`，extra merge gap `0ms`。
- compact：首批 target/min `3m/2m`，后续 `15m/10m`，最大 `20m`。
- separator `500ms`，forced split overlap `1500ms`，prepared WAV limit `2`，Whisper worker `1`。
- 本地 diarization 关闭，speaker 统一 `unknown`。
- `word_timestamps=false`，`condition_on_previous_text=false`，temperature `0`。
- 首批检测语言，仅中文置信度 `>=0.90` 时后续锁定 `zh`。
- 二次 Whisper 复核关闭，调用和预算均为 `0`。
- mapping tolerance `300ms`；跨 source entry 句段拒绝。
- 重复句时间重叠率 `30%`；软风险权重 `0.6`。
- 剩余冲突保留更安全的代表，不再清空双方。

## 10. 阶段二设计边界

- 在 compact 构建器入口执行 VAD 区间排序、补白和重叠并集。
- 规范化后的 source ranges 必须严格递增且互不重叠。
- 远距离区间使用 separator，不吞入中间静音；separator 不映射到原音频。
- entry 尾部进入 separator 不超过 300ms 时可裁回当前 entry；到达下一 source entry 时拒绝。
- 强制切分重叠用 ownership 和安全文本规则去重。
- DeepSeek 只做语义角色判断，身份可靠阈值 `0.85`。
- 风险分类保留，二次 ASR 取消；软风险保留降权，硬拒绝仅用于结构无效内容。
- 参数指纹不同不得从旧 checkpoint 恢复。

## 11. 阶段二遥测与门槛

必须分别记录 VAD、区间规范化、WAV 准备、Whisper、映射去重、风险分类和本地总时间，以及调用数、候选语音、separator、拒绝原因、峰值 RSS、临时磁盘、同时 WAV 和二次调用数。

首轮门槛：

- 本地转写 35–45 分钟。
- 正常 compact 调用 14–18。
- 二次 Whisper 调用 0。
- 首条真实进度不超过 5 分钟。
- 时间戳冲突低于总句段 2%。
- 硬丢弃候选语音低于 1% 时长。
- 头部、中部和尾部均有可靠证据。
- 页面完成报告并可回听证据。

时间达标但关键事实、短回答、时间戳或页面证据明显退化，仍然是 No-Go。

## 12. 第二轮才允许调参

首轮不得临场修改。之后按单变量比较：

1. VAD threshold `0.2` 对 `0.3`。
2. min silence `0.25s` 对 `0.5s`。
3. compact `15/20min` 对 `10/15min`。
4. mapping tolerance `300ms` 对 `500ms`。
5. 人物归因失败时，再比较关闭本地说话人与仅句段级说话人。

不要一次调整多个参数，也不要用不同音频结果计算加速比。

## 13. Git 与环境

- 只在 V0.1 worktree 工作。
- 原始 workspace 有用户未跟踪文件，不要清理、覆盖或提交。
- 不使用 `git reset --hard`、`git checkout --` 或递归删除。
- 不直接 cherry-pick/merge `codex/streaming-local-transcription`。
- 参考实验算法时逐文件阅读，用测试驱动重新实现最小内核。
- worktree 默认没有 `backend/.venv`。普通测试可调用原 workspace 虚拟环境；`doctor.sh` 需要 worktree 自己的 `.venv` 路径。必要时临时建 symlink，测试后用 `unlink backend/.venv` 移除，确保不进入 Git。

## 14. 隐私边界

允许记录 commit、branch、模型 ID、audio SHA-256、时长、大小、聚合数量、阶段耗时、token、调用次数、错误码、请求响应字节数、coverage counts 和资源峰值。

禁止记录原音频路径、转写正文、音频片段、API Key、完整模型输入输出，以及把包含个人内容的页面截图写入仓库。

## 15. 新窗口启动提示词

> 请按照交接文档继续完成 Local Fast V0.1：`/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1/docs/HANDOFF-2026-08-10-LOCAL-FAST-V0.1.md`。先完整阅读参数基准和技术设计，先写实施计划再改代码。第一阶段只修 DeepSeek，复用现有 3,442 个可靠句段重新分析；完成后直接打开页面让我验收，再继续 compact 本地链路。

## 16. 完成定义

1. 参数基准未被静默改变。
2. 阶段一在页面成功展示完整分析结果，失败时能看到具体错误码。
3. 3,442 个可靠句段无需重转写即可重新分析。
4. 阶段二同音频本地转写达到 35–45 分钟，二次 Whisper 调用为 0。
5. 本地说话人识别关闭，模型身份判断保持保守。
6. 时间戳和硬丢弃门槛通过。
7. 后端、前端、构建和浏览器 E2E 全部通过。
8. 真实音频报告只包含合规聚合指标。
9. 页面结果可回到可靠原始证据。
10. 用户确认页面效果可接受。
