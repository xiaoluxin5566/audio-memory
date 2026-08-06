# 快速且可靠的本地转写设计

## 目标

在不降低 `mlx-community/whisper-large-v3-turbo` 转写质量、不关闭说话人分段的前提下，消除 VAD 模型缺失时静默降级为全音频转写的问题，并为长音频增加受资源保护的双窗口并行。

## 已确认的根因

本机 208 MB 音频实际长度为 13,031,820 ms（约 3 小时 37 分）。当前机器缺少 `models/diarization/silero_vad.onnx` 及同组说话人模型，VAD 以 `RuntimeError` 失败，现有代码写入空 speech mapping 并改走整段音频的兜底路径。因此 ETA 约 36 分钟并非文件大小导致，而是 VAD-first 加速没有生效。

## 约束与非目标

- 保持 `whisper-large-v3-turbo`；不提供低质量模型切换。
- 保持本地说话人分段。
- 不向模型厂商发送音频；不改变分析厂商与 Keychain 边界。
- 不向普通用户显示“VAD / 并行 / GPU / 模型模式”等技术状态。
- 本次不修改历史重分析：历史重分析复用现有转写，不应触发 Whisper。
- 已停止的旧任务不恢复；修复后由用户重新上传测试。

## 设计

### 1. 本地模型准备与启动检查

安装器继续下载并校验三份本地语音预处理模型：Silero VAD、pyannote segmentation 和 3D-Speaker embedding。每个文件必须与安装器固定的 SHA-256/大小匹配，manifest 同时记录实际与期望值。

新增 `LocalModelReadiness`：

- 启动时只读验证 Whisper manifest、三份 diarization 文件和 manifest；不读取 Keychain、不调用网络。
- 应用仍可打开，方便用户配置 Key 或查看历史；但上传任务的“开始分析”接口必须先要求 readiness 为 ready。
- 若模型缺失或损坏，接口返回稳定码 `local_models_unavailable`。前端以非技术化文案提示“本地转写组件未准备好，请完成修复安装后再试”，不会启动慢速兜底。
- `scripts/doctor.sh` 和安装烟测复用同一校验逻辑，防止安装、启动、诊断三套标准漂移。

### 2. VAD 失败语义

一次任务的 VAD 检测最多尝试 3 次，间隔采用短暂的递增等待（0、0.5、1.0 秒）。每次尝试新建 VAD/ffmpeg 资源，不复用异常状态。

- 三次均失败：抛出 `VoiceActivityUnavailableError`，不生成空 mapping，不调用 Whisper，不调用说话人分段。
- `TranscriptionService` 将任务设为 `failed`，错误码为 `local_preprocessing_failed`，保留清理后的上传任务之外的任何隐性重试状态；用户必须重新上传后再开始。
- 浏览器只显示“本次解析失败，请重新上传分析。”并提供放弃/重新上传入口；不展示 VAD 名称或重试次数。
- 取消/应用关闭优先于重试；取消不会启动后续尝试。

### 3. 双窗口并行与 80% 动态上限

Whisper 音频窗口仍为现有 300 秒。默认先运行一个窗口；调度器在派发下一个窗口时评估是否允许第二个并行 worker。

Apple Silicon 没有可在普通权限下稳定读取的“GPU 利用率百分比”接口，且 GPU/CPU 共享统一内存。因此 80% 上限用可验证的安全代理实现：

- 系统已用内存比例必须低于 80%。
- MLX 的 active-memory / unified-memory 上限必须低于 80%；不可取得时只允许单窗口。
- 若任一指标达到或超过 80%，立即停止派发新的第二窗口；已开始的窗口完成，不强杀。
- 第二窗口只处理不重叠的、已经完成 VAD 与说话人分段准备的 Whisper chunk。结果按原始 chunk 顺序提交，边界去重和 transcript segment 索引保持确定性。
- 每个文件的最大 Whisper 并发固定为 2；多文件任务仍维持一个文件接一个文件，避免复合并行。
- 资源采样异常、MLX 内存不可读、系统进入 memory pressure 时均安全回退为单窗口。

这不是“把机器压到 80%”，而是“只有安全余量存在时才短暂使用第二窗口”。预期吞吐提升取决于音频的人声比例和机器内存，不能承诺固定倍数。

### 4. 用户体验与 ETA

正常界面保持现有的“本地 Whisper 转写中”和简洁 ETA。无需显示技术处理模式。

ETA 仅在至少有一个 Whisper 窗口样本后显示；并行时按实际完成的音频毫秒/墙钟时间重新估算。预处理失败只显示最终失败状态，不显示内部错误细节。

## 接口和状态

| 位置 | 新行为 |
| --- | --- |
| `POST /api/jobs/{id}/start` | readiness 不通过时返回 409 + `local_models_unavailable`；不创建转写任务。 |
| 转写 worker | VAD 三次失败后停止任务，`error_code=local_preprocessing_failed`。 |
| Job API / UI | 将 `local_preprocessing_failed` 映射为“本次解析失败，请重新上传分析”。 |
| 安装与 doctor | 同一受信任 manifest 校验；不接受测试 fixture 哈希作为生产模型。 |

## 测试与验收

1. 缺少、截断或哈希不匹配的 VAD/diarization 文件会阻止任务开始，且不会创建 Whisper worker。
2. VAD 前两次失败、第三次成功时，Whisper 只运行一次且 speech mapping 非空。
3. VAD 三次失败时，任务为 `failed/local_preprocessing_failed`，Whisper 和 diarization 调用数均为零，界面显示重新上传提示。
4. 资源采样低于阈值时至多两个 Whisper 窗口并行；达到 80% 或采样失败时始终单窗口；提交顺序和现有边界去重结果一致。
5. 完整后端、前端、浏览器、安装烟测、doctor fixture 和离线 Prompt 门禁均通过。

## 风险

- 真实音频 VAD 可能因编码损坏或 ffmpeg 解码失败而三次失败；此设计选择显式失败并要求重新上传，避免用慢速路径掩盖问题。
- 双进程 MLX 的实际吞吐因统一内存而可能低于理想值；动态准入保证它在没有收益或资源紧张时回退为单窗口。
- 若系统级 GPU 指标未来可无权限稳定读取，可作为额外信号加入，但不得取代统一内存安全阈值。
