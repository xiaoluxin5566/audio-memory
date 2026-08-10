# 本地快速转写 V0 验收证据

- 验收日期：2026-08-10
- 验收分支：`codex/local-fast-v0`
- 冻结基线：`main@24e086786896f48f7dc0bcd0630376ee3d0054f8`
- 冻结提交说明：`docs: plan streaming local transcription`
- V0 转写模型：`mlx-community/whisper-large-v3-turbo`
- 操作系统：macOS 26.5.1 (Build 25F80)
- 机器：MacBook Air (Mac16,12), Apple M4 10-core, 16 GB
- Python：3.12.13（`uv run python`）
- Node.js：v22.22.1
- ffmpeg：8.1.1

## 冻结与范围证据

- `codex/local-fast-v0` 从冻结提交直接创建，未合并其他工作树。
- `backend/src/audio_memory/transcription` 在创建分支时无工作区差异。
- `codex/streaming-local-transcription` 不是当前 HEAD 的祖先；compact、streaming、coverage audit 与 96.2% 二次复核实验均未纳入 V0。
- 未再次合并 `codex/transcription-phase0`。
- 受保护的未跟踪文件在创建分支后仍存在，未删除、覆盖、暂存或提交。

## 音频输入元数据

> 已授权一个真实长音频用于测试。根据交接约束，所有缺失值记为 `null`，且不记录音频路径或转写文本。

| 样本 | SHA-256 | 时长 | 文件大小 | 声道 | 采样率 |
|---|---|---:|---:|---:|---:|
| 10～15 分钟代表性样本 | `null` | `null` | `null` | `null` | `null` |
| 真实 3.5 小时长音频 | `e3061b4ba464e5b2b5830e00fdf0ad5ac2dde28d8e4f3021537beb06b3778a0c` | 12,685.248s | 202,963,968 bytes | 1 | 16,000 Hz |

## 自动化回归

| 验证项 | 命令 | 改动前基线 | 改动后验收 |
|---|---|---|---|
| 后端完整回归 | `cd backend && env UV_CACHE_DIR='../.uv-cache' uv run pytest -q` | PASS：543 passed，19.53s | PASS：543 passed，18.31s，退出码 0 |
| 前端单元测试 | `cd prototype && node --test tests/*.test.mjs` | PASS：47 passed，0 failed，1.17s | PASS：47 passed，0 failed，1.23s，退出码 0 |
| 前端生产构建 | `cd prototype && npm run build` | PASS：退出码 0 | PASS：Vite 38 modules，394ms，Sites 产物已生成，退出码 0 |
| 风险提示 E2E | `cd prototype && npx playwright test tests/e2e/recovery.spec.js` | RED：1 failed/2 passed，失败原因为 Beta 文案不存在 | PASS：3 passed，3.7s，退出码 0 |

## 真实音频用例

| 用例 | 状态 | 正确 | 错误 | 遗漏 | 误归因 | 证据/备注 |
|---|---|---:|---:|---:|---:|---|
| LF-001 近场单人短样本 | 未运行 | `null` | `null` | `null` | `null` | 缺少明确授权的输入 |
| LF-002 真实长音频 | FAIL | `null` | `null` | `null` | `null` | 本地转写完成，但 DeepSeek 事件地图阶段失败；零卡片、零待办，未伪装成功 |
| LF-003 转写中取消 | 未运行 | `null` | `null` | `null` | `null` | 待人工验收 |
| LF-004 Whisper 子进程失败 | 部分通过 | `null` | `null` | `null` | `null` | 受限启动环境无法访问 Metal，任务进入 `interrupted/transcription_failed`；远端分析未启动，UI 提供继续入口 |
| LF-005 电视背景 + 用户近场说话 | 未运行 | `null` | `null` | `null` | `null` | 待人工验收 |
| LF-006 人名、金额、日期、待办 | 未运行 | `null` | `null` | `null` | `null` | 待人工回听 |
| LF-007 远场低音量短句 | 未运行 | `null` | `null` | `null` | `null` | 待人工验收 |
| LF-008 两人重叠讲话 | 未运行 | `null` | `null` | `null` | `null` | 待人工验收 |
| LF-009 长静音/音乐/环境声 | 未运行 | `null` | `null` | `null` | `null` | 待人工验收 |
| LF-010 重启后恢复 | 部分通过 | `null` | `null` | `null` | `null` | 同一任务在 GPU 可用环境重启后可恢复；首次中断发生在首片段落库前，未覆盖“中途检查点不重复” |

### LF-002 运行事实

- 运行厂商/模型：DeepSeek / `deepseek-v4-flash`，用户已明确授权可靠转写文本发送范围。
- 首次启动在受限环境中无法访问 Metal，约 197 秒后进入 `interrupted/transcription_failed`；同一 60 秒本地片段在 GPU 可用环境中 9.878 秒成功完成，证明音频和 Whisper 模型本身可用。
- GPU 可用环境恢复后，VAD 生成 793 个语音处理窗口；本地阶段最终生成 4,117 个句段。
- 风险门结果：3,402 个直接可靠；656 个 `REJECTED`；59 个局部精转写中 40 个通过、19 个失败。最终可靠 3,442 个，丢弃 675 个，丢弃率 16.40%。
- 首、中、尾各 5 分钟分别存在 9、79、8 个可靠句段；可靠尾段结束于 12,665.854 秒，未发现转写层面的静默尾部截断。
- 不读取或记录正文的结构检查发现：0 个负开始时间、0 个反向区间；15 次开始时间倒退、52 次结束时间倒退，最大倒退分别为 22,572ms 与 28,004ms；1 个片段结束时间超过源音频时长，最大结束时间为 12,695.854 秒，比源音频多 10.606 秒。
- 页面 ETA 在约 16% 时一度显示“约 412 分钟”，与实际持续产出和最终墙钟明显不符，属于严重失真。
- 远端分析在事件地图阶段失败：分析版本没有持久化 event map，`staged_results_json` 为空对象，任务进入 `failed/model_analysis_failed`；未发布任何卡片或待办。
- 现有异常处理将事件地图的 Schema/覆盖校验具体异常归一为 `model_analysis_failed`，本次证据无法进一步区分“修复后 JSON 仍不合规”与“4,117 段证据 ID 覆盖不完整”。为避免额外付费调用，未重试。
- 未进行语义人工回听，因此正确、错误、遗漏、误归因和 CER 均保持 `null`，不得把结构检查解释为内容正确率。

## 完整链路墙钟

> 阶段边界来自轮询观测，约有一个轮询周期的误差。有效基线从 GPU 可用环境恢复时开始；另列用户实际等待墙钟，避免隐藏首次失败与恢复成本。

| 阶段 | 秒 | 备注 |
|---|---:|---|
| 预处理 | `null` | VAD/切窗与首个 Whisper 窗口没有独立埋点，不能拆分 |
| bulk Whisper（含预处理） | ≈6,348 | 恢复开始至观测到音频尾部，约 105分48秒 |
| 风险门复核 | ≈580 | 59 个局部精转写，约 9分40秒 |
| 本地阶段合计 | ≈6,928 | 约 115分28秒 |
| 模型分析 | ≈228 | 事件地图阶段失败，约 3分48秒 |
| 报告生成 | `null` | 未进入发布成功状态 |
| 有效恢复链路总墙钟 | ≈7,156 | 约 119分16秒，最终失败 |
| 用户实际等待总墙钟 | ≈7,505 | 约 125分05秒，包含首次 Metal 失败与诊断恢复 |

## 今日发布硬门槛

| 门槛 | 结果 | 证据 |
|---|---|---|
| G1 任务可终止 | PASS | 首次转写失败进入 interrupted；恢复链路最终进入 failed，没有无限运行 |
| G2 错误不伪装成功 | PASS | `model_analysis_failed` 后零卡片、零待办，UI 明确显示失败与重新分析入口 |
| G3 报告结构完整 | FAIL | 事件地图阶段失败，未生成或发布报告 |
| G4 证据可追溯 | FAIL | 没有报告可回溯；转写还存在时间顺序倒退和一个越过源时长的区间 |
| G5 严重误归因为 0 | 未测量 | 待 LF-005 |
| G6 P0 关键事实 | 未测量 | 待 LF-006 回听 |
| G7 完整链路墙钟 | FAIL | 有效恢复链路约 119分16秒但最终失败；用户实际等待约 125分05秒，超过 120 分钟不可演示线 |
| G8 回归测试 | PASS | 后端 543 passed；前端 47 passed；风险提示 E2E 3 passed；生产构建退出码 0 |

## Go / Conditional Go / No-Go

**当前判定：No-Go。**

理由：LF-002 最终为 `model_analysis_failed`，G3、G4、G7 至少三项 P0 门槛失败；没有报告可供人工回听确认。即使忽略首次受限环境故障，有效恢复链路也用时约 119分16秒后失败，几乎耗尽 120 分钟上限；用户实际等待已超过该上限。

V0 不应发布或演示为“可稳定完成长音频并生成报告”。若继续推进，只允许针对 P0 做独立计划：先让事件地图支持超长、数千句段输入并保留可诊断错误码，再修正 ETA 与时间戳边界；不得把 compact/streaming 实验并入本冻结分支。
