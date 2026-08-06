# 快速转写风险门 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保持全量句段级快速转写，仅对高风险片段做一次词级精转写，并确保不可信文本不进入任何用户结果或 AI 输入。

**Architecture:** 新建纯函数风险分类模块；快速句段级转写先按现有检查点机制落库，再由 `TranscriptionRiskGateService` 批量分类并通过有界队列调用现有 `SelectiveRefiner`，完成后才将任务推进到分析。风险状态持久化到 Transcript；分析查询只读取可靠片段，内部指标仅记录数量、状态和时间范围。

**Tech Stack:** Python 3.12、SQLAlchemy/Alembic、mlx-whisper、pytest、现有 VAD speech mapping 与 SelectiveRefiner。

## Global Constraints

- 全量路径硬锁 `word_timestamps=False`；只有高风险精转写调用允许 `True`。
- 状态只允许 `REJECTED`、`HIGH_RISK_PENDING`、`POST_EDIT_PASSED`、`POST_EDIT_FAILED`。
- 队列容量为 `max(10, ceil(total_segments * 0.05))`，每段最多精转写一次，总耗时增幅不超过 20%。
- 不可信文本不进入 C 端、分析、检索、卡片、待办或画像；内部记录不保存不可信文本。
- `no_speech_prob` 首版只记录，不作单一硬拒绝依据。

---

### Task 1: 风险状态持久化与分析隔离

**Files:**
- Create: `backend/migrations/versions/0008_transcript_risk_state.py`
- Modify: `backend/src/audio_memory/models.py`
- Modify: `backend/src/audio_memory/transcription/segments.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Modify: `backend/src/audio_memory/analysis/runner.py`
- Test: `backend/tests/integration/test_transcription_recovery.py`
- Test: `backend/tests/integration/test_analysis_pipeline.py`

**Interfaces:**
- Produces: `Transcript.risk_state: str | None`, `Transcript.is_reliable: bool`, `Transcript.reliability_weight: float`, `Transcript.risk_reason: str | None`；`JobFile.vad_energy_json: str` 保存不含音频内容的分桶能量范围。
- Produces: `TranscriptSegment` 同名字段，默认可靠、权重 1.0。

- [ ] **Step 1: 写失败测试**：保存 `POST_EDIT_FAILED/is_reliable=False` 后，数据库保留时间范围和原因，但 `text=""`、`words_json="[]"`；`AnalysisRunner._transcript()` 只返回 `is_reliable=True`，中风险返回 `reliability_weight=0.6`。

```python
assert stored.risk_state == "POST_EDIT_FAILED"
assert stored.is_reliable is False
assert stored.text == "" and stored.words_json == "[]"
assert [item["text"] for item in await runner._transcript("job-1")] == ["可信文本"]
```

- [ ] **Step 2: 运行测试确认因字段不存在而失败**

Run: `cd backend && uv run pytest tests/integration/test_transcription_recovery.py tests/integration/test_analysis_pipeline.py -q`

- [ ] **Step 3: 新增 0008 迁移和字段**：默认 `risk_state=NULL`、`is_reliable=1`、`reliability_weight=1.0`、`vad_energy_json=[]`；保存段落时复制风险字段；状态转为 `REJECTED` 或 `POST_EDIT_FAILED` 的同一事务中清空 `text` 与 `words_json`；分析 SQL 增加 `Transcript.is_reliable.is_(True)` 并透传权重。

- [ ] **Step 4: 运行测试并提交**

```bash
git add backend/migrations/versions/0008_transcript_risk_state.py backend/src/audio_memory/models.py backend/src/audio_memory/transcription/segments.py backend/src/audio_memory/transcription/checkpoints.py backend/src/audio_memory/analysis/runner.py backend/tests/integration/test_transcription_recovery.py backend/tests/integration/test_analysis_pipeline.py
git commit -m "feat: isolate unreliable transcript segments"
```

### Task 2: 确定性文本标准化与风险分类

**Files:**
- Create: `backend/src/audio_memory/transcription/risk_gate.py`
- Create: `backend/tests/unit/transcription/test_risk_gate.py`

**Interfaces:**
- Produces: `normalize_transcript_text(text: str) -> str`。
- Produces: `normalized_similarity(first: str, second: str) -> float`。
- Produces: `classify_segments(segments, speech_intervals, energy_intervals) -> list[RiskDecision]`。

- [ ] **Step 1: 写失败测试**：覆盖 NFKC、标点空格、大小写、中文/阿拉伯数字统一；相似度 0.90；30 秒内三次重复；非首段 10 秒间隙；1.5 秒与有效语音时长；0.3 秒边界容差及“重叠 0.5 秒或覆盖 30%”。

```python
assert normalize_transcript_text("会议三点。") == normalize_transcript_text("会议 3 点")
assert classify_segments(repeated_three_times, speech, energy)[2].state == "HIGH_RISK_PENDING"
assert classify_segments(first_after_15s_silence, speech, energy)[0].state is None
```

- [ ] **Step 2: 运行测试确认模块不存在**

Run: `cd backend && uv run pytest tests/unit/transcription/test_risk_gate.py -q`

- [ ] **Step 3: 实现最小纯函数**：使用 `unicodedata.normalize("NFKC")`、固定中文数字解析、归一化 Levenshtein；定义 `RiskDecision(state, reason, is_reliable, reliability_weight)`，硬拒绝文本不写入 decision 日志对象。

- [ ] **Step 4: 运行测试并提交**

```bash
git add backend/src/audio_memory/transcription/risk_gate.py backend/tests/unit/transcription/test_risk_gate.py
git commit -m "feat: classify risky transcript segments"
```

### Task 3: 有界精转写队列与状态机

**Files:**
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Create: `backend/src/audio_memory/transcription/risk_service.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/tests/integration/test_diarization_pipeline.py`

**Interfaces:**
- Consumes: Task 2 `RiskDecision`；现有 `SelectiveRefiner.refine(segment_uids)`；已落库 Transcript 和 JobFile speech/energy mapping。
- Produces: `TranscriptionRiskGateService.apply(job_id: str, refiner: SelectiveRefiner) -> RiskGateMetrics`；只有该调用成功后 `TranscriptionService` 才进入 `ANALYZING`。

- [ ] **Step 1: 写失败测试**：全量调用始终 `word_timestamps=False`；三次重复只精写一次；精写通过替换文本；仍重复进入 `POST_EDIT_FAILED`；第四次状态迁移不可发生；超过 `max(10, ceil(n*0.05))` 的片段降为中风险权重 0.6。

```python
assert all(call.word_timestamps is False for call in bulk_calls)
assert refine_calls == [risk_segment.segment_uid]
assert failed.risk_state == "POST_EDIT_FAILED" and failed.is_reliable is False
```

- [ ] **Step 2: 运行测试确认缺少风险门集成**

Run: `cd backend && uv run pytest tests/integration/test_diarization_pipeline.py -q`

- [ ] **Step 3: 最小实现**：VAD 解码时按固定时间桶计算归一化波形能量并持久化；快速句段全部保存后由 `TranscriptionRiskGateService` 统一分类；稳定排序高风险队列；仅入队段调用 `SelectiveRefiner`；复检只调用文本规则；不得再次运行硬性时间戳规则。风险门完成前不得提交分析任务。

- [ ] **Step 4: 运行测试并提交**

```bash
git add backend/src/audio_memory/transcription/engine.py backend/src/audio_memory/transcription/risk_service.py backend/src/audio_memory/transcription/checkpoints.py backend/src/audio_memory/main.py backend/tests/integration/test_diarization_pipeline.py
git commit -m "feat: selectively refine risky transcription"
```

### Task 4: 脱敏指标与耗时门禁

**Files:**
- Create: `backend/src/audio_memory/transcription/risk_metrics.py`
- Create: `backend/tests/unit/transcription/test_risk_metrics.py`
- Modify: `backend/src/audio_memory/transcription/risk_service.py`

**Interfaces:**
- Produces: `RiskGateMetrics(total_segments, rejected, queued, overflowed, passed, failed, elapsed_seconds)`。
- Produces: 单行结构化日志 `risk_gate_metrics`，不含文本、文件名或音频路径。

- [ ] **Step 1: 写失败测试**：日志只包含计数和耗时；构造精写耗时超过快速路径 20% 时停止继续入队，剩余高风险降为中风险。

```python
assert "秘密文本" not in caplog.text
assert metrics.overflowed == 2
assert queued_elapsed <= bulk_elapsed * 0.20
```

- [ ] **Step 2: 运行失败测试**

Run: `cd backend && uv run pytest tests/unit/transcription/test_risk_metrics.py -q`

- [ ] **Step 3: 实现指标聚合与墙钟预算检查**，仅在窗口边界停止新增精写，已开始的单段允许完成。

- [ ] **Step 4: 运行测试并提交**

```bash
git add backend/src/audio_memory/transcription/risk_metrics.py backend/src/audio_memory/transcription/risk_service.py backend/tests/unit/transcription/test_risk_metrics.py
git commit -m "feat: bound transcription risk gate cost"
```

### Task 5: 参数调优证据与全量验证

**Files:**
- Create: `scripts/evaluate-transcription-risk-gate.py`
- Create: `backend/tests/unit/transcription/test_risk_gate_evaluation.py`
- Create: `docs/benchmark-evidence/2026-08-07-risk-gate-calibration.md`

**Interfaces:**
- Consumes: 人工标注 JSONL，仅含匿名片段 ID、期望风险标签和已脱敏特征。
- Produces: 每组阈值的 precision、recall、FPR、TPR；不输出音频或文本。

- [ ] **Step 1: 写失败测试**：固定混淆矩阵断言 `precision=0.75`、`recall=0.60`，并拒绝包含 `text` 或 `audio_path` 的输入。

- [ ] **Step 2: 运行失败测试**

Run: `cd backend && uv run pytest tests/unit/transcription/test_risk_gate_evaluation.py -q`

- [ ] **Step 3: 实现离线评测脚本**，扫描相似度 0.85/0.90/0.95、字符速率 12/14/16；概率指标仅在样本稳定存在时进入候选，不自动改生产阈值。

- [ ] **Step 4: 运行专项与全量验证**

Run: `cd backend && uv run pytest -q`

Run: `cd prototype && node --test tests/*.test.mjs && npm run build`

Run: `./scripts/doctor.sh`

Expected: 所有测试通过；医生检查除未启动服务外无模型或安装错误。

- [ ] **Step 5: 提交**

```bash
git add scripts/evaluate-transcription-risk-gate.py backend/tests/unit/transcription/test_risk_gate_evaluation.py docs/benchmark-evidence/2026-08-07-risk-gate-calibration.md
git commit -m "test: calibrate transcription risk thresholds"
```
