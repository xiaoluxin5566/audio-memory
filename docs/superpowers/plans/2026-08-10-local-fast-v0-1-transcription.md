# Local Fast V0.1 Compact Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hundreds of short local Whisper calls with bounded, reversible compact batches and complete the same 3h31m source in 35–45 minutes with zero diarization and zero secondary Whisper calls while retaining reliable evidence timestamps.

**Architecture:** Extract compact interval/batch/mapping behavior from the current 1,331-line engine into focused immutable modules. Normalize VAD intervals once, build compact WAVs with explicit non-mappable separators and forced-split ownership, run one persistent Whisper worker, then map/validate/deduplicate before persistence. Simplify the risk service to classify the first-pass text without selective refinement, and add stage-level aggregate telemetry plus compact-batch ETA.

**Tech Stack:** Python 3.12, asyncio, ffmpeg, sherpa-onnx Silero VAD, mlx-whisper, SQLAlchemy async, Pydantic/dataclasses, pytest/pytest-asyncio, React/Vite, Playwright.

## Global Constraints

- Do not start this plan until the DeepSeek plan has reached its page acceptance checkpoint and the user has reviewed the result.
- Work only in `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1` on `codex/local-fast-v0-1`.
- Keep VAD threshold `0.2`, minimum speech `0.25s`, minimum silence `0.25s`, maximum speech `1800s`, padding `500ms`, extra merge gap `0ms`, 16kHz mono, and 60-second VAD buffer.
- Compact first batch target/minimum is 3m/2m; later target/minimum is 15m/10m; maximum speech per batch is 20m; separator is 500ms; forced-split overlap is 1500ms.
- At most two prepared WAVs may coexist; exactly one persistent Whisper worker runs.
- Whisper model is `mlx-community/whisper-large-v3-turbo` with `word_timestamps=False`, `condition_on_previous_text=False`, and `temperature=0`.
- Detect language on the first batch; lock later batches to `zh` only when every first-batch language result is Chinese with confidence at least `0.90`.
- Local diarization and word-speaker alignment are disabled; persisted `speaker_id` is `unknown`.
- Selective refinement is disabled; secondary Whisper call count and budget are both zero; `HIGH_RISK_PENDING` is never produced.
- Soft-risk first-pass text is retained with reliability weight `0.6`; only structurally invalid content may be hard-rejected.
- Mapping tolerance is 300ms; entering the next source entry is rejected; duplicate time-overlap threshold is 30%; numbers and negations must match; unresolved conflicts retain the safer representative.
- Parameter fingerprints must match before checkpoint recovery.
- The first full run must use the frozen values unchanged. Tuning is a later, single-variable experiment.
- Never persist or log original audio paths, transcript text, audio clips, API keys, or screenshots containing personal content.

---

## File Structure

- Create `backend/src/audio_memory/transcription/compact.py`: immutable normalized ranges, compact batches, entries, ownership, fingerprints, and serialization.
- Create `backend/src/audio_memory/transcription/mapping.py`: compact-to-source mapping, 300ms boundary rules, duplicate detection, and conflict representative selection.
- Create `backend/src/audio_memory/transcription/metrics.py`: aggregate stage timers/resource counters and privacy-safe report serialization.
- Modify `backend/src/audio_memory/transcription/engine.py`: orchestrate VAD, two-WAV preparation, one worker, language lock, mapping, and yielding; remove local diarization from the active path.
- Modify `backend/src/audio_memory/transcription/checkpoints.py`: recover by compact batch/fingerprint and pass no refiner.
- Modify `backend/src/audio_memory/transcription/risk_gate.py` and `risk_service.py`: distinguish structural rejection from soft risk and retain first-pass content.
- Modify `backend/src/audio_memory/transcription/eta.py`: estimate from the latest three completed compact batches.
- Modify `backend/src/audio_memory/main.py`: construct the V0.1 engine without diarization or `SelectiveRefiner`.
- Modify job/progress API and `prototype/src/App.jsx`: expose the real eight-stage/compact-batch progress.
- Add focused unit/integration/E2E tests adjacent to each behavior.

### Task 1: Frozen parameter model and fingerprint

**Files:**
- Create: `backend/src/audio_memory/transcription/compact.py`
- Create: `backend/tests/unit/transcription/test_compact.py`
- Modify: `backend/src/audio_memory/transcription/engine.py:36-42,932-960`

**Interfaces:**
- Produces: frozen `LocalFastParameters` with the exact Global Constraints values.
- Produces: `LocalFastParameters.fingerprint() -> str`, SHA-256 over canonical JSON.
- Produces: `SourceRange(start_ms: int, end_ms: int)` and validates positive, increasing timestamps.

- [ ] Write literal-value and canonical-fingerprint tests, including a test proving any single changed parameter changes the fingerprint.
- [ ] Run `python -m pytest -q backend/tests/unit/transcription/test_compact.py` and verify RED for missing types.
- [ ] Implement only the immutable parameter/range types and canonical serializer.
- [ ] Re-run the test and commit with `feat: define local fast transcription parameters`.

### Task 2: VAD interval normalization

**Files:**
- Modify: `backend/src/audio_memory/transcription/compact.py`
- Modify: `backend/tests/unit/transcription/test_compact.py`
- Modify: `backend/src/audio_memory/transcription/engine.py:270-311,982-999`

**Interfaces:**
- Produces: `normalize_source_ranges(intervals, *, duration_ms, padding_ms=500) -> tuple[SourceRange, ...]`.
- Guarantees: output is sorted, clipped to `[0,duration_ms]`, strictly increasing, non-overlapping, and merges overlap/touch only after padding.

- [ ] Add table tests for unsorted ranges, both-side padding, file clipping, overlap union, touching union, zero-gap behavior, and distant silence preservation.
- [ ] Verify RED, implement normalization without batch logic, and verify GREEN.
- [ ] Replace `build_speech_mapping` normalization use with the new function while retaining the frozen VAD detector values.
- [ ] Run existing sparse-audio and VAD-failure tests and commit with `feat: normalize compact source ranges`.

### Task 3: Bounded compact batch builder and reversible entries

**Files:**
- Modify: `backend/src/audio_memory/transcription/compact.py`
- Modify: `backend/tests/unit/transcription/test_compact.py`

**Interfaces:**
- Produces: `CompactEntry(compact_start_ms, compact_end_ms, source_start_ms, source_end_ms, kind, ownership_start_ms, ownership_end_ms)` where `kind` is `source` or `separator`.
- Produces: `CompactBatch(index, entries, speech_ms, compact_ms, forced_split, parameter_fingerprint)`.
- Produces: `build_compact_batches(ranges, parameters) -> tuple[CompactBatch, ...]`.
- Guarantees: first target/min 3m/2m, later 15m/10m, maximum 20m speech, 500ms separators, 1500ms forced-split overlap, deterministic serialization, and no separator-to-source mapping.

- [ ] Add tests for the first small batch, later target batching, final undersized merge, maximum bound, distant separators, contiguous ranges, a source range over 20m, 1500ms overlap, ownership midpoint, empty input, and serialization round-trip.
- [ ] Verify RED, implement the minimal pure builder, verify every batch invariant and GREEN.
- [ ] Commit with `feat: build bounded compact batches`.

### Task 4: Compact-to-source mapping and boundary rejection

**Files:**
- Create: `backend/src/audio_memory/transcription/mapping.py`
- Create: `backend/tests/unit/transcription/test_compact_mapping.py`
- Modify: `backend/src/audio_memory/transcription/segments.py:1-66`

**Interfaces:**
- Produces: `map_segment(batch, raw_segment, *, tolerance_ms=300) -> MappedSegment | MappingRejection`.
- Rejection reasons are literal safe codes: `empty_text`, `invalid_time`, `outside_batch`, `separator_only`, `cross_source_entry`, and `severe_boundary_overrun`.
- Guarantees: a segment contained in one source entry maps exactly; up to 300ms into the following separator clips to the current entry; touching the next source entry rejects.

- [ ] Write literal mapping fixtures for exact containment, 299/300/301ms separator overrun, separator-only, crossing into the next source entry, negative/reversed/out-of-batch timestamps, and source-file clipping.
- [ ] Verify RED, implement mapping as a pure function, and verify GREEN.
- [ ] Commit with `feat: map compact transcript timestamps`.

### Task 5: Forced-overlap deduplication and safe conflict retention

**Files:**
- Modify: `backend/src/audio_memory/transcription/mapping.py`
- Modify: `backend/tests/unit/transcription/test_compact_mapping.py`
- Modify: `backend/src/audio_memory/transcription/engine.py:379-589`

**Interfaces:**
- Produces: `reconcile_mapped_segments(segments) -> ReconciliationResult(kept, rejected, conflict_count)`.
- Duplicate equality requires at least 30% overlap of the shorter range, normalized-equal text, and identical protected numbers/negations.
- Conflict ranking is: wholly mapped, higher `avg_logprob`, lower `no_speech_prob`, more complete text, earlier stable order.

- [ ] Port behavior tests for safe text equivalence, protected number/date/time/negation disagreement, ownership, reverse order, and the 30% literal threshold.
- [ ] Add failing tests proving a non-duplicate conflict keeps one safer representative instead of clearing both, and only an unrankable structurally invalid cluster is rejected.
- [ ] Verify RED, implement the reconciliation result, remove the old “discard both” active behavior, and verify GREEN.
- [ ] Commit with `fix: retain safe compact timestamp conflicts`.

### Task 6: Two-WAV preparation and one persistent Whisper worker

**Files:**
- Modify: `backend/src/audio_memory/transcription/engine.py:604-621,932-1227`
- Modify: `backend/src/audio_memory/transcription/compact.py`
- Create: `backend/tests/integration/test_compact_transcription.py`
- Modify: `backend/tests/integration/test_diarization_pipeline.py:215-410,943-1284`

**Interfaces:**
- Produces: `prepare_compact_wav(source, batch, target) -> Path` using ffmpeg concat/filter input generated from source entries plus synthetic 500ms silence.
- Produces: a bounded producer/consumer pipeline with queue size 2 and one `ProcessPoolExecutor(max_workers=1)` reused for all batches.
- `_transcribe_worker` consumes `word_timestamps=False`, `condition_on_previous_text=False`, `temperature=0`, and optional language.

- [ ] Add fake-ffmpeg tests asserting separators are silence, entries preserve order, no distant source silence is copied, prepared-WAV concurrency never exceeds two, cleanup is manifest-scoped, and a failed batch enters interrupted without skipping.
- [ ] Add worker tests asserting one executor/worker, literal Whisper kwargs, one call per batch, and no diarization call.
- [ ] Verify RED, implement preparation/orchestration, then run focused integration tests GREEN.
- [ ] Commit with `feat: transcribe bounded compact batches`.

### Task 7: First-batch language detection and checkpoint recovery

**Files:**
- Modify: `backend/src/audio_memory/transcription/engine.py:1038-1139`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py:26-170`
- Modify: `backend/src/audio_memory/models.py:250-305` only if existing JSON fields cannot store the compact checkpoint without a migration.
- Add migration and migration tests only when a new column is required.
- Modify: `backend/tests/integration/test_compact_transcription.py`
- Modify: `backend/tests/integration/test_transcription_recovery.py:159-205,417-463`

**Interfaces:**
- Produces: first-batch language summary and later `language="zh"` only when every detection is `zh` with confidence `>=0.90`.
- Produces: checkpoint `{parameter_fingerprint,last_completed_batch,next_segment_index,language_lock}` written only after a complete mapped batch is persisted.
- Guarantees: mismatched fingerprint refuses recovery; matching recovery never repeats completed batches or skips the interrupted batch.

- [ ] Add tests for all-high-confidence Chinese, one low-confidence result, one non-Chinese result, crash before checkpoint, crash after checkpoint, restart with same fingerprint, restart with changed fingerprint, and no duplicate transcript IDs/timestamps.
- [ ] Verify RED, implement the smallest checkpoint/language state, and verify GREEN.
- [ ] Commit with `feat: recover compact whisper batches`.

### Task 8: Disable diarization and all secondary Whisper calls

**Files:**
- Modify: `backend/src/audio_memory/main.py:85-107`
- Modify: `backend/src/audio_memory/transcription/engine.py:23-28,591-799,932-1056`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py:30-63`
- Modify: `backend/tests/integration/test_compact_transcription.py`
- Modify: `backend/tests/integration/test_transcription_recovery.py:206-321`

**Interfaces:**
- Produces: active pipeline `speaker_id="unknown"` for every valid first-pass segment.
- Guarantees: diarization model construction/calls, word-speaker alignment, `SelectiveRefiner`, word-timestamp refinement, secondary call count, and refinement budget are all zero.

- [ ] Write constructor and end-to-end fake tests that fail if any diarization/refiner/secondary worker is created or called; assert `speaker_id == "unknown"` and `word_timestamps is False`.
- [ ] Verify RED, remove these components from the active V0.1 construction and service contract without deleting unrelated legacy modules, and verify GREEN.
- [ ] Commit with `perf: disable local speaker and refinement passes`.

### Task 9: Soft-risk retention and structural-only hard rejection

**Files:**
- Modify: `backend/src/audio_memory/transcription/risk_gate.py:83-177,308-485`
- Modify: `backend/src/audio_memory/transcription/risk_service.py:168-373,503-565`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py:129-159`
- Modify: `backend/tests/unit/transcription/test_risk_gate.py`
- Modify: `backend/tests/integration/test_diarization_pipeline.py:515-942,1357-1986`

**Interfaces:**
- Produces: `REJECTED` only for empty text, invalid/reversed timestamps, severe mapping overflow, cross-entry/separator mapping, or otherwise structurally invalid content.
- Produces: soft-risk segments as reliable first-pass text with `reliability_weight=0.6`, reason retained, and no `HIGH_RISK_PENDING`.

- [ ] Rewrite expected behaviors as failing tests for low VAD overlap, repetition, post-silence repeat, suspicious speech rate, and incomplete non-structural context; each must retain text at weight 0.6 and never queue refinement.
- [ ] Retain RED tests for genuine structural rejection and forbidden content persistence on rejected rows.
- [ ] Verify RED, separate structural and soft decisions, simplify service storage to one classification pass, and verify GREEN.
- [ ] Commit with `fix: retain first pass soft risk evidence`.

### Task 10: Stage metrics, compact ETA, and real progress UI

**Files:**
- Create: `backend/src/audio_memory/transcription/metrics.py`
- Create: `backend/tests/unit/transcription/test_metrics.py`
- Modify: `backend/src/audio_memory/transcription/eta.py:1-27`
- Modify: `backend/src/audio_memory/transcription/engine.py:962-1134`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py:30-92`
- Modify: `backend/src/audio_memory/api/jobs.py` and job event payloads at their current progress serializers.
- Modify: `prototype/src/App.jsx` at the upload-progress rendering branch.
- Modify: `prototype/tests/e2e/recovery.spec.js` or add `prototype/tests/e2e/local-fast-progress.spec.js`.

**Interfaces:**
- Produces aggregate timings for VAD, normalization, WAV preparation, Whisper, mapping/deduplication, risk classification, and local total.
- Produces counts/durations for calls, candidate speech, separators, mapping rejects by reason, risk reasons, peak RSS, temporary disk, prepared-WAV high-water mark, and secondary calls.
- Produces progress labels: 检测语音, 整理语音批次, 本地转写 N/M, 校验时间轴, 构建事件地图, 生成分析 N/6, 更新画像, 发布结果.
- ETA uses only the latest three completed compact batches.

- [ ] Add privacy tests that serialize/log metrics and prove source path, transcript text, and audio bytes cannot appear.
- [ ] Add deterministic ETA tests using three literal `(speech_ms, elapsed_seconds)` samples and rejecting zero/non-finite samples.
- [ ] Add API/E2E progress tests for all eight stages and ensure analysis no longer sits at a fixed 80%.
- [ ] Verify RED, implement metrics/events/UI, and verify GREEN.
- [ ] Commit with `feat: report local fast stage progress`.

### Task 11: Full automated regression

**Files:**
- Modify only scoped defects revealed by verification.

- [ ] Run all compact/risk/recovery tests with the original workspace virtual environment.
- [ ] Run the full backend suite and require every baseline plus new test to pass.
- [ ] Run `cd prototype && node --test tests/*.test.mjs`, `npm run build`, `npm run test:sites`, and the relevant Playwright suites.
- [ ] Run `git diff --check`; inspect all changed files; prove no parameter drift, transcript content, original audio path, API secret, prepared WAV, model request/response, or personal screenshot is tracked.
- [ ] Use `superpowers:verification-before-completion` before claiming the implementation is complete.

### Task 12: Same-audio full-chain acceptance

**Files:**
- Create: `docs/benchmark-evidence/2026-08-10-local-fast-v0-1-acceptance.md`

**Interfaces:**
- Consumes: the same audio SHA-256 `e3061b4ba464e5b2b5830e00fdf0ad5ac2dde28d8e4f3021537beb06b3778a0c` on the same hardware/model.
- Produces: an aggregate-only Go/No-Go report and a product page ready for user review.

- [ ] Record pre-run model, commit, audio SHA-256/duration/size, hardware, cold/warm marker, and parameter fingerprint without the source path.
- [ ] Run one fresh complete job from VAD through page publication; do not tune parameters during the run.
- [ ] Record stage timings, first real progress time, normal compact calls, secondary calls, candidate/separator durations, mapping conflicts/rejections, hard-discard duration, risk counts, peak RSS, temp disk, and prepared-WAV high-water mark.
- [ ] Require local time 35–45 minutes, normal calls 14–18, secondary calls 0, first progress <=5 minutes, timestamp conflicts <2%, hard-discard candidate speech <1%, and reliable evidence in head/middle/tail.
- [ ] Verify the page completes analysis and evidence playback works; sample critical facts, short answers, timestamps, and attribution manually. A timing pass with visible quality regression is No-Go.
- [ ] Open the completed product page for the user and leave the server running; do not store personal screenshots.
- [ ] Commit the safe report with `test: record local fast v0.1 acceptance`.

## Plan Self-Review

- Spec coverage: every frozen VAD/compact/Whisper/language/mapping/risk/metrics/UI/acceptance requirement maps to one task.
- Boundaries: pure compact building and mapping live outside the large engine; the active engine remains the orchestrator; legacy diarization/refiner code is not opportunistically deleted.
- Type consistency: `LocalFastParameters` supplies the fingerprint consumed by batches and checkpoints; `CompactBatch` supplies entries consumed by preparation and mapping; mapped segments feed reconciliation and persistence.
- Recovery: checkpoint writes happen only after complete batch persistence and are fenced by the exact parameter fingerprint.
- Privacy: tests use synthetic audio/text and the real report contains aggregates only.
- Scope order: this plan is written now but cannot execute before the DeepSeek page acceptance checkpoint.
