# Local Fast V0.1 技术设计

**状态：** 已确认，待实施  
**日期：** 2026-08-10  
**基线：** `main@24e0867` 之上的验收分支 `8cd1383`  
**目标分支：** `codex/local-fast-v0-1`

## 1. 目标

V0.1 分两阶段交付：

1. 先修复 DeepSeek 事件地图链路，直接复用真实音频已经冻结的 3,442 个可靠句段，不重新执行本地 Whisper；完成后在产品页面展示事件卡片和待办结果。
2. 再用有界 compact 合批替代 793 个短窗口调用，关闭本地说话人识别和全部二次 Whisper 复核，将同一份 3 小时 31 分钟音频的本地转写控制在 35–45 分钟。

所有首轮参数以 `docs/LOCAL-FAST-V0.1-PARAMETER-BASELINE.md` 为唯一来源。实施代码不得复制出第二套不同默认值；每次运行必须能记录参数指纹。

## 2. 非目标

- 不直接合并 `codex/streaming-local-transcription` 的 74 文件实验实现。
- 不引入 streaming、coverage audit、全量词级时间戳或二次复核。
- 不切换 Paraformer、SenseVoice 或其他本地 ASR 模型。
- 不在首轮调整 VAD 数值。
- 不把 DeepSeek 的语义角色判断描述成声纹身份识别。
- 不通过删除正文、扩大硬拒绝或降低证据覆盖换取速度。

## 3. 完整链路

```text
上传与探测
  → 16 kHz 单声道解码
  → Silero VAD
  → 补白、排序、重叠区间取并集
  → 有界 compact 合批与可逆映射
  → 单 worker Whisper bulk
  → 映射、边界校验、强制边界去重
  → 无二次模型调用的风险分类
  → 可靠证据冻结
  → DeepSeek compact event map
  → 本地补全未归类句段并验证覆盖
  → 六场景串行分析
  → 画像提取
  → 原子发布卡片与待办
  → 页面展示
```

V0.1 不运行本地 diarization。所有本地句段的 speaker 均为 `unknown`，DeepSeek 只允许根据文字语义判断事件角色；身份置信度不足 0.85 时保持未知。

## 4. 阶段一：DeepSeek 最小修复

### 4.1 输入投影

事件地图请求不再为每个句段重复发送文件 ID、文件名、日期、时区和可靠性字段。输入分为：

- 文件级元数据：每个文件只发送一次。
- 句段级数据：`id`、`start_ms`、`end_ms`、`text`。
- 画像快照：维持当前结构。

真实 3,442 段的句段 JSON 目标从约 1,014,131 bytes 降至约 277,543 bytes，不删除转写正文。

### 4.2 本地补全覆盖

模型仍输出事件及每个事件引用的证据 ID，但不负责抄写全部未归类 ID。服务端执行：

```text
known_ids = 所有可靠句段 ID
assigned_ids = 所有事件 evidence_segment_ids 的并集
unknown_ids = assigned_ids - known_ids
unassigned_ids = known_ids - assigned_ids
```

如果 `unknown_ids` 非空，事件地图失败并记录 `event_map_unknown_segment`。如果为空，服务端用 `unassigned_ids` 覆盖模型返回值，再执行完整 `EventMap` 校验并持久化。模型不能通过省略 ID 造成覆盖失败，也不能引用不存在的证据。

### 4.3 DeepSeek 请求参数

DeepSeek V4 Flash 的正式分析请求显式发送：

- `thinking={"type":"disabled"}`；
- `temperature=0`；
- `response_format={"type":"json_object"}`；
- event map `max_tokens=32768`；
- scene `max_tokens=16384`；
- profile `max_tokens=8192`。

事件地图超时 180 秒，场景和画像超时 120 秒。只对网络超时、429 和 5xx 做最多一次额外重试。Schema/JSON 只允许一次修复。

### 4.4 安全诊断

不得保存请求正文、响应正文、原音频路径或 API Key。每次请求只持久化或聚合记录：

- provider/model；
- scene ID；
- request bytes；
- transcript segment count；
- response bytes；
- input/output tokens；
- elapsed seconds；
- HTTP status category；
- finish reason；
- repair attempted；
- known/assigned/unassigned/unknown counts；
- normalized error code。

错误码至少区分：

- `network_timeout`
- `authentication_failed`
- `insufficient_balance`
- `rate_limited`
- `provider_unavailable`
- `content_rejected`
- `model_response_invalid`
- `model_output_truncated`
- `event_map_schema_invalid`
- `event_map_unknown_segment`
- `event_map_coverage_invalid`

协调器不得把以上错误重新覆盖成通用 `model_analysis_failed`。

### 4.5 复用现有转写并展示页面

阶段一完成后对现有失败任务重新分析：

- source job：`d29475e4-f148-4b99-9b7e-1e5751da1e48`
- 可靠句段：3,442
- 不重新运行 VAD、Whisper、风险门或说话人识别。
- 生成新的 analysis version，不覆盖失败版本的证据。
- 成功后打开产品页面，验收卡片、待办、证据回听入口和错误提示状态。

页面验收门槛：

- 事件地图成功持久化；
- 六场景和画像完成或返回具体可诊断错误；
- 不再显示 `model_analysis_failed` 通用错误；
- 发布只在全部必需场景通过后原子发生；
- 页面结果引用的每个证据 ID 都属于 3,442 个可靠句段。

## 5. 阶段二：Local Fast V0.1

### 5.1 区间规范化

保留当前 VAD 参数。VAD 结果进入一个统一的区间规范化函数：排序、两侧 500ms 补白、裁到文件范围、重叠或相接区间取并集。该函数不额外吞入远距离静音。

VAD 合并不再作为独立产品阶段；它成为 compact 构建器的输入合同，确保 source ranges 严格递增且互不重叠。

### 5.2 有界 compact 批次

实现独立、可测试的 compact batch builder：

- 首批目标 3 分钟、最少 2 分钟；
- 后续目标 15 分钟、最少 10 分钟；
- 单批最多 20 分钟候选语音；
- 不连续 entry 插入 500ms separator；
- 超长连续 entry 使用 1.5 秒重叠强制切分；
- 同时最多保留 2 个准备好的临时 WAV；
- 每个 entry 保存 compact 起点、source 起点、时长和边界标记。

不得把 separator 映射为原始音频。批次和映射必须支持序列化、恢复和参数指纹校验。

### 5.3 Whisper bulk

使用一个持久 worker，模型为 `mlx-community/whisper-large-v3-turbo`，显式设置：

- `word_timestamps=False`
- `condition_on_previous_text=False`
- `temperature=0`

首批自动检测语言。如果首批所有语言检测均为中文且置信度不低于 0.90，后续固定 `language="zh"`；否则继续自动检测。

### 5.4 关闭本地说话人与二次复核

- 不初始化 diarization segmentation 或 embedding 模型。
- 不运行声纹聚类和词级 speaker 对齐。
- speaker 字段统一为 `unknown`。
- 风险门不产生 `HIGH_RISK_PENDING`。
- 不调用 `SelectiveRefiner`。
- 软风险保留第一遍文字、可靠性权重 0.6。
- 只有空文本、无效时间、严重映射越界、跨 separator/entry 等结构错误可以硬拒绝。

### 5.5 时间映射与冲突

一个 Whisper 句段只有落在单个 source entry 内才可直接映射。

- 在 entry 末端进入 separator 不超过 300ms：裁回当前 entry。
- 到达下一 source entry：拒绝，不能跨不连续原音频拼句。
- 超出批次或文件范围超过 300ms：拒绝。
- 强制切分产生的预期重复使用 ownership 与文本去重处理。
- 剩余冲突不再清空双方，按映射完整性、`avg_logprob`、`no_speech_prob`、文本完整度和稳定顺序选择一个代表；无法安全选择时才拒绝冲突簇。

### 5.6 性能与质量门槛

同一真实音频的本地链路必须记录：

- VAD 秒数；
- 区间规范化秒数；
- compact WAV 准备秒数；
- Whisper 秒数和调用数；
- 映射/去重秒数；
- 风险分类秒数；
- 本地总秒数；
- 首批进度秒数；
- 候选语音时长；
- separator 时长；
- 映射拒绝数量和时长；
- 每种风险原因的数量和时长；
- 峰值 RSS、临时磁盘和同时 WAV 数；
- 二次 Whisper 调用数。

首轮 Go 条件：

- 本地总时间 35–45 分钟；
- 二次 Whisper 调用数为 0；
- 目标正常批次 14–18；
- 时间戳冲突低于总句段 2%；
- 硬丢弃候选语音低于 1% 时长；
- 第一条真实进度在 5 分钟内出现；
- 头部、中部和尾部都有可回听可靠句段；
- 页面完成完整分析并可回听证据。

低于 35 分钟不自动代表 Go，仍须通过质量门。高于 45 分钟为性能失败，不通过临时抬高 VAD threshold 重写结果。

## 6. 页面进度

页面使用真实阶段和批次，不固定显示“分析 80%”：

```text
检测语音
整理语音批次
本地转写 3/16
校验时间轴
构建事件地图
生成分析 2/6
更新画像
发布结果
```

ETA 使用最近 3 个完整 compact 批次的候选语音时长和墙钟时间估计，不使用当前“最大句段结束时间 / 文件总时长”的算法。

## 7. 测试策略

所有行为修改遵循测试先行：

1. DeepSeek 请求投影、thinking/max tokens、finish reason 和错误码单元测试。
2. 事件地图本地补全、未知 ID、重复 ID 和完整覆盖测试。
3. 现有 analysis pipeline 集成测试，证明失败不会发布半成品。
4. compact builder 的排序、补白、并集、separator、批次边界和映射测试。
5. Whisper 参数、语言锁定和 worker 复用测试。
6. 300ms 边界容忍、跨 separator 拒绝、强制重叠去重和冲突保留测试。
7. 无 diarization、无二次复核、软风险降权的集成测试。
8. 后端全量回归、前端单元测试、生产构建和浏览器 E2E。
9. 真实音频先复用转写验收分析页面，再执行一次完整 V0.1 全链路验收。

## 8. 失败与回退

- 阶段一失败不触发本地转写；保留失败 analysis version 和具体错误码，可从事件地图重试。
- 阶段二 compact 单批失败不得静默跳过；任务进入 interrupted，并从最后完整批次恢复。
- 参数指纹不一致时禁止在旧 checkpoint 上恢复。
- V0.1 不覆盖 V0 分支；出现质量或恢复缺陷时可返回 `codex/local-fast-v0`。
- 真实音频报告不得写入源路径、转写正文、音频片段、API Key 或供应商响应正文。

## 9. 实施顺序

1. DeepSeek 安全诊断和请求参数。
2. Event map 输入投影和本地覆盖补全。
3. 复用现有 3,442 段重新分析并展示页面。
4. Compact 参数模型、区间规范化与 batch builder。
5. Compact Whisper、映射和恢复。
6. 关闭 diarization 和二次复核，更新风险分类。
7. 页面进度与阶段遥测。
8. 同一真实音频完整验收。
