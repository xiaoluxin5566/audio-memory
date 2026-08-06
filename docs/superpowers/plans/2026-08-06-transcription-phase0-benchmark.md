# 本地转写 Phase 0 基准验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在任何 V2 转写实现开始前，用约 3.5 小时真实代表性音频测得预处理与单窗口 Whisper 的耗时比例，并据此锁定 Plan A 或 Plan B。

**Architecture:** 基准工具只调用本机 Silero VAD、说话人分段模型和 `mlx-community/whisper-large-v3-turbo`，逐项记录单调时钟耗时。比例为 `(VAD + 全部窗口说话人分段) / 单窗口 Whisper`；大于 0.30 选择 Plan B，否则 Plan A。结果写应用诊断目录，工具不访问 Keychain、分析提供商、业务数据库，不保存音频路径或转写文本。

**Tech Stack:** Python 3.12、sherpa-onnx、ffmpeg、mlx-whisper、pytest、`AppPaths`。

## Global Constraints

- 仅 macOS Apple Silicon；保持 `mlx-community/whisper-large-v3-turbo` 和本地说话人分段。
- 仅使用用户明确授权的测试音频；不得上传、提交、复制到仓库或写入业务数据库。
- 先验证生产 manifest；模型缺失或损坏则停止，不走全音频兜底。
- 报告不含 API Key、Keychain、音频路径/内容或转写文本。
- 后续 V2 固定 `DEFAULT_PEAK_DELTA = 4 GiB`，准入公式为 `steady + max(4 GiB, measured_delta) < physical_memory * 0.85`。
- Plan B 的 VAD 必须按时间顺序 `yield` 窗口并经 IPC 发送，禁止全量缓存。

---

### Task 1: 建立可重复的脱敏基准工具

**Files:**
- Create: `scripts/benchmark-local-transcription.py`
- Create: `backend/tests/unit/transcription/test_benchmark_local_transcription.py`

**Interfaces:**
- Produces: `select_plan(vad_seconds: float, diarization_seconds: float, whisper_seconds: float) -> str`，只返回 `plan_a` 或 `plan_b`。
- Produces: `BenchmarkReport.to_json() -> dict[str, float | str | bool]`。

- [ ] **Step 1: 写出失败测试**

```python
def test_select_plan_uses_the_30_percent_gate():
    assert select_plan(vad_seconds=9, diarization_seconds=3, whisper_seconds=40) == "plan_a"
    assert select_plan(vad_seconds=9, diarization_seconds=4, whisper_seconds=40) == "plan_b"

def test_report_excludes_source_and_transcript():
    keys = BenchmarkReport(1, 2, 10, 0.3, "plan_a").to_json().keys()
    assert "audio_path" not in keys and "transcript" not in keys
```

- [ ] **Step 2: 运行失败测试**

Run: `cd backend && uv run pytest tests/unit/transcription/test_benchmark_local_transcription.py -q`

Expected: FAIL，模块或 `select_plan` 不存在。

- [ ] **Step 3: 实现最小工具**

```python
def select_plan(*, vad_seconds: float, diarization_seconds: float, whisper_seconds: float) -> str:
    if whisper_seconds <= 0:
        raise ValueError("Whisper benchmark must be positive")
    return "plan_b" if (vad_seconds + diarization_seconds) / whisper_seconds > 0.30 else "plan_a"
```

CLI 只接收一个音频路径，先复用生产 manifest 校验，再顺序计时 VAD、每个窗口说话人分段和单窗口 Whisper。报告写至 `AppPaths.runtime / "diagnostics" / "phase0-benchmark.json"`，目录 0700、文件 0600；仅含时长、比例、裁决、生成时间、模型校验结果和物理内存。

- [ ] **Step 4: 验证并提交**

Run: `cd backend && uv run pytest tests/unit/transcription/test_benchmark_local_transcription.py tests/unit/diarization/test_alignment.py -q`

Expected: PASS。

```bash
git add scripts/benchmark-local-transcription.py backend/tests/unit/transcription/test_benchmark_local_transcription.py
git commit -m "test: add local transcription phase0 benchmark"
```

### Task 2: 执行基准并锁定架构

**Files:**
- Create: `docs/benchmark-evidence/2026-08-06-transcription-phase0.md`
- Runtime output: `~/Library/Application Support/AudioMemory/runtime/diagnostics/phase0-benchmark.json`（不提交）

**Interfaces:**
- Consumes: Task 1 CLI 的脱敏 JSON。
- Produces: `plan_a` 或 `plan_b` 的唯一裁决和三项耗时证据。

- [ ] **Step 1: 验证模型就绪**

Run: `./scripts/doctor.sh`

Expected: Whisper 和说话人分段模型都通过；否则停止，不运行基准。

- [ ] **Step 2: 对获授权音频执行基准**

Run: `PYTHONPATH=backend/src uv run --project backend python scripts/benchmark-local-transcription.py --audio "$BENCHMARK_AUDIO"`，其中 `BENCHMARK_AUDIO` 是用户在运行前明确授权的绝对路径。

Expected: 退出码 0，生成脱敏 JSON，无网络、无数据库写入。

- [ ] **Step 3: 写入不可变的架构裁决**

证据文档固定包含五行：VAD 秒数、说话人分段秒数、单窗口 Whisper 秒数、由前三项计算的预处理比例、以及 Plan A 或 Plan B 裁决。每一项必须直接填入 JSON 中的实际值；依据固定为“比例 <= 0.30 为 Plan A；比例 > 0.30 为 Plan B”。

- [ ] **Step 4: 复核并提交证据**

重新计算比例，确认与 JSON 一致；不得提交音频或运行时 JSON。

```bash
git add docs/benchmark-evidence/2026-08-06-transcription-phase0.md
git commit -m "docs: record transcription phase0 decision"
```

### Task 3: 基准门禁后的实施计划

**Files:**
- Create: `docs/superpowers/plans/2026-08-06-transcription-v2-plan-a.md` 或 `docs/superpowers/plans/2026-08-06-transcription-v2-plan-b.md`

**Interfaces:**
- Consumes: Task 2 的唯一裁决。
- Produces: 不混合架构的 V2 实施计划。

- [ ] **Step 1: 只创建与裁决匹配的计划文件**

`plan_a` 时仅写 Plan A；`plan_b` 时仅写 Plan B。另一计划不得创建。

- [ ] **Step 2: 写入全部硬门槛和测试**

所选计划必须覆盖：readiness、VAD 子进程三次重试/8 分钟超时/取消无残留、最多三次原地重试、0600 脱敏诊断、4 GiB 与 85% 内存准入、加载后 5 秒稳定采样、第二窗口过载限流。Plan B 还必须有“VAD IPC 增量 yield”和“首批 Whisper 与末批分段时间重叠”的测试。

- [ ] **Step 3: 提交实施计划**

```bash
git add docs/superpowers/plans/2026-08-06-transcription-v2-plan-*.md
git commit -m "docs: plan transcription v2 implementation"
```
