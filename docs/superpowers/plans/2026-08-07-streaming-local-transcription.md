# Streaming Local Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make long-file VAD, window preparation, speaker diarization, and Whisper overlap safely while preserving ordered, complete, resumable transcripts.

**Architecture:** A VAD subprocess emits typed IPC events into a bounded async pipeline. A single commit coordinator owns window state and atomic ordered persistence; a timeout may temporarily create a memory-gated catch-up worker, but speculative results remain bounded and invisible until the missing window succeeds.

**Tech Stack:** Python 3.12, asyncio, multiprocessing, SQLAlchemy/Alembic, sherpa-onnx Silero VAD, ffmpeg, mlx-whisper, pytest.

## Global Constraints

- Keep `mlx-community/whisper-large-v3-turbo`, `word_timestamps=False`, speaker diarization, 500 ms speech padding, the current risk gate, and source-timeline timestamps.
- VAD `max_speech_duration` is 300 seconds; processing overlap is 30 seconds and never appears in `vad_speech_json`.
- Both pipeline queues and the speculative commit buffer have capacity 2; at most 4 temporary WAV files may exist.
- Normal execution uses one Whisper worker. A catch-up worker requires `steady_state + max(4 GiB, measured_delta) < physical_memory * 0.85`.
- A window may be attempted at most 3 times and may never be permanently skipped.
- Do not stage, edit, or delete `.playwright-cli/`, `.superpowers/brainstorm/`, `prototype/src/mockEngine.js`, or `prototype/tests/mock-engine.test.mjs`.

---

### Task 1: File-level atomic window checkpoints

**Files:**
- Create: `backend/migrations/versions/0011_streaming_window_checkpoints.py`
- Modify: `backend/src/audio_memory/models.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Test: `backend/tests/integration/test_database_schema.py`
- Test: `backend/tests/integration/test_transcription_recovery.py`

**Interfaces:**
- Produces: `JobFile.last_committed_window_index: int`, `JobFile.last_committed_source_ms: int`, and `TranscriptionService.commit_window(file_id, window_index, source_end_ms, segments)`.
- Invariant: all rows for one window and its checkpoint commit in one transaction.

- [ ] **Step 1: Write the failing schema and rollback tests**

```python
assert job_file.last_committed_window_index == -1
assert job_file.last_committed_source_ms == 0

database.inject_commit_failure(RuntimeError("commit interrupted"))
with pytest.raises(RuntimeError, match="commit interrupted"):
    await service.commit_window("file-1", 0, 30_000, segments)
assert await transcript_count(database, "file-1") == 0
assert (await load_file(database, "file-1")).last_committed_window_index == -1
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `cd backend && uv run pytest tests/integration/test_database_schema.py tests/integration/test_transcription_recovery.py -q`

Expected: FAIL because migration 0011, checkpoint columns, and `commit_window` do not exist.

- [ ] **Step 3: Add migration, model fields, and atomic commit**

```python
op.add_column("job_files", sa.Column("last_committed_window_index", sa.Integer(), nullable=False, server_default="-1"))
op.add_column("job_files", sa.Column("last_committed_source_ms", sa.Integer(), nullable=False, server_default="0"))

async def commit_window(self, file_id, window_index, source_end_ms, segments):
    async with self.database.session() as session:
        file = await session.get(JobFile, file_id)
        if window_index != file.last_committed_window_index + 1:
            raise ValueError("Window commit is not contiguous")
        session.add_all(self._transcript_rows(segments))
        file.last_committed_window_index = window_index
        file.last_committed_source_ms = source_end_ms
        await session.commit()
```

Move persistence from per-segment `_save_segment` calls to one `commit_window` call. Keep segment UID uniqueness as defense in depth, not as the resume cursor.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/integration/test_database_schema.py tests/integration/test_transcription_recovery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0011_streaming_window_checkpoints.py backend/src/audio_memory/models.py backend/src/audio_memory/transcription/checkpoints.py backend/tests/integration/test_database_schema.py backend/tests/integration/test_transcription_recovery.py
git commit -m "feat: add atomic transcription window checkpoints"
```

### Task 2: Typed streaming VAD IPC

**Files:**
- Create: `backend/src/audio_memory/transcription/vad_stream.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Test: `backend/tests/unit/transcription/test_vad_stream.py`
- Test: `backend/tests/integration/test_diarization_pipeline.py`

**Interfaces:**
- Produces: frozen dataclasses `VadIntervalEvent`, `VadProgressEvent`, `VadCompletedEvent`, `VadFailedEvent` and async iterator `stream_vad(path, model, timeout_seconds=480)`.
- Consumes: ffmpeg PCM and sherpa-onnx `vad.front`; emits terminal completed or failed event exactly once.

- [ ] **Step 1: Write failing event-order and early-yield tests**

```python
events = [event async for event in stream_vad(source, model, worker=fake_worker)]
assert isinstance(events[0], VadProgressEvent)
assert isinstance(events[1], VadIntervalEvent)
assert events[1].end_ms <= 300_000
assert isinstance(events[-1], VadCompletedEvent)
assert worker.interval_sent_before_eof is True
assert max(progress.scanned_ms for progress in progress_events) == duration_ms
```

Also assert progress events are throttled to at most one per second, IPC close without a terminal event raises `VoiceActivityUnavailableError`, and cancellation terminates and joins the child.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_vad_stream.py tests/integration/test_diarization_pipeline.py -q`

Expected: FAIL because `vad_stream` and typed events are missing and `detect()` buffers until EOF.

- [ ] **Step 3: Implement the subprocess producer and parent iterator**

```python
config.silero_vad.max_speech_duration = WHISPER_CHUNK_SECONDS

while raw := stdout.read(window_bytes):
    vad.accept_waveform(samples)
    while not vad.empty():
        queue.put(VadIntervalEvent(start_ms, end_ms))
        vad.pop()
    if scanned_ms - last_reported_ms >= 1_000:
        queue.put(VadProgressEvent(scanned_ms))
queue.put(VadCompletedEvent(duration_ms=scanned_ms, energy_intervals=energy))
```

The parent async iterator polls IPC without blocking the event loop, enforces the existing 8-minute attempt timeout and three-attempt policy, and always terminates/joins on cancellation.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/unit/transcription/test_vad_stream.py tests/integration/test_diarization_pipeline.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/transcription/vad_stream.py backend/src/audio_memory/transcription/engine.py backend/tests/unit/transcription/test_vad_stream.py backend/tests/integration/test_diarization_pipeline.py
git commit -m "feat: stream VAD events from subprocess"
```

### Task 3: Incremental window builder and bounded preparation pipeline

**Files:**
- Create: `backend/src/audio_memory/transcription/window_pipeline.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Test: `backend/tests/unit/transcription/test_window_pipeline.py`
- Test: `backend/tests/integration/test_diarization_pipeline.py`

**Interfaces:**
- Produces: `StreamingWindow(index, source_start_ms, source_end_ms, ownership_start_ms, ownership_end_ms)` and `PreparedWindow(window, wav_path, speaker_turns)`.
- Produces: `iter_streaming_windows(events, duration_ms, padding_ms=500)` and `prepare_windows(source, window_queue, prepared_queue)`.

- [ ] **Step 1: Write failing boundary, backpressure, and early-preparation tests**

```python
windows = list(build_windows([
    SpeechInterval(10_000, 11_000),
    SpeechInterval(11_600, 13_000),
], duration_ms=20_000, padding_ms=500))
assert windows == [StreamingWindow(0, 9_500, 13_500, 9_500, 13_500)]

assert pipeline.window_queue.maxsize == 2
assert pipeline.prepared_queue.maxsize == 2
assert fake_whisper.first_call_at < fake_vad.eof_at
assert fake_whisper.first_call_at < fake_extractor.last_file_created_at
```

Add cases for gap greater than1 second, EOF flush, 300-second forced VAD boundary, 30-second processing overlap, source-time ownership, and producer blocking when the queue is full.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_window_pipeline.py tests/integration/test_diarization_pipeline.py -q`

Expected: FAIL because the bounded streaming builder and preparer do not exist.

- [ ] **Step 3: Implement bounded queues and per-window extraction**

```python
window_queue: asyncio.Queue[StreamingWindow | EndOfStream] = asyncio.Queue(maxsize=2)
prepared_queue: asyncio.Queue[PreparedWindow | EndOfStream] = asyncio.Queue(maxsize=2)

async for event in vad_events:
    if isinstance(event, VadIntervalEvent):
        for window in builder.push(event):
            await window_queue.put(window)

while (window := await window_queue.get()) is not END:
    wav = await extract_one(source, window)
    turns = await asyncio.to_thread(diarize_fail_open, diarizer, wav)
    await prepared_queue.put(PreparedWindow(window, wav, turns))
```

Delete `_extract_speech_intervals` from the VAD-success path. Preserve the full-audio error behavior defined by the current approved VAD failure policy; do not silently choose it when VAD fails.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/unit/transcription/test_window_pipeline.py tests/integration/test_diarization_pipeline.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/transcription/window_pipeline.py backend/src/audio_memory/transcription/engine.py backend/tests/unit/transcription/test_window_pipeline.py backend/tests/integration/test_diarization_pipeline.py
git commit -m "feat: prepare transcription windows as a bounded stream"
```

### Task 4: Ordered commit coordinator and recovery state machine

**Files:**
- Create: `backend/src/audio_memory/transcription/window_state.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Test: `backend/tests/unit/transcription/test_window_state.py`
- Test: `backend/tests/integration/test_transcription_recovery.py`

**Interfaces:**
- Produces: `WindowState` enum and `WindowCommitCoordinator` with `start`, `complete`, `retry`, `fail`, `drain_committable`.
- Consumes: `PreparedWindow` and window-level transcript results; calls Task 1 `commit_window` only for contiguous indices.

- [ ] **Step 1: Write failing state-transition and crossing-order tests**

```python
coordinator.start(0)
coordinator.retry(0)
coordinator.start(1, speculative=True)
coordinator.complete(1, result_one)
assert coordinator.state(1) is WindowState.COMPLETED_WAITING
assert coordinator.drain_committable() == []
coordinator.complete(0, result_zero)
assert [item.index for item in coordinator.drain_committable()] == [0, 1]

with pytest.raises(InvalidWindowTransition):
    coordinator.commit(2)
```

Cover recovery-first, catch-up-first, second-attempt success, third-attempt failure, full speculative buffer, cancellation with two workers, and memory admission denied.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_window_state.py tests/integration/test_transcription_recovery.py -q`

Expected: FAIL because the state machine and contiguous drain do not exist.

- [ ] **Step 3: Implement the explicit state machine**

```python
class WindowState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED_WAITING = "completed_waiting"
    RETRY_PENDING = "retry_pending"
    RETRY_RUNNING = "retry_running"
    COMMITTABLE = "committable"
    COMMITTED = "committed"
    FAILED = "failed"

ALLOWED = {
    READY: {RUNNING},
    RUNNING: {COMPLETED_WAITING, RETRY_PENDING},
    RETRY_PENDING: {RETRY_RUNNING},
    RETRY_RUNNING: {COMPLETED_WAITING, RETRY_PENDING, FAILED},
    COMPLETED_WAITING: {COMMITTABLE},
    COMMITTABLE: {COMMITTED},
}
```

Make the coordinator the only code allowed to mutate window state. Buffer at most two speculative results and never expose them to persistence, risk classification, or analysis before the gap closes.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/unit/transcription/test_window_state.py tests/integration/test_transcription_recovery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/transcription/window_state.py backend/src/audio_memory/transcription/engine.py backend/src/audio_memory/transcription/checkpoints.py backend/tests/unit/transcription/test_window_state.py backend/tests/integration/test_transcription_recovery.py
git commit -m "feat: coordinate ordered streaming window commits"
```

### Task 5: Whisper timeout, worker restart, and memory-gated catch-up

**Files:**
- Create: `backend/src/audio_memory/transcription/worker_supervisor.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Test: `backend/tests/unit/transcription/test_worker_supervisor.py`
- Test: `backend/tests/integration/test_transcription_recovery.py`

**Interfaces:**
- Produces: `window_timeout_seconds(audio_seconds, stable_samples)`, `WhisperWorkerSupervisor.transcribe`, `restart_recovery_worker`, `maybe_start_catchup_worker`.
- Uses: `timeout=max(120, p95*3, audio_seconds*0.5)`, maximum three attempts, and existing 4 GiB/85% memory admission logic.

- [ ] **Step 1: Write failing timeout and catch-up tests**

```python
assert window_timeout_seconds(300, []) == 150
assert window_timeout_seconds(60, [10, 12, 11, 13]) == 120

result = await supervisor.transcribe(window, worker=hanging_worker)
assert supervisor.terminated_pids == [hanging_worker.pid]
assert coordinator.state(0) is WindowState.RETRY_PENDING
assert supervisor.catchup_started is memory_admission_allowed
```

Assert a catch-up worker handles at most two later windows, missing-window recovery has priority, a third failure yields `transcription_window_failed`, and all worker processes are joined.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_worker_supervisor.py tests/integration/test_transcription_recovery.py -q`

Expected: FAIL because the supervisor and timeout policy do not exist.

- [ ] **Step 3: Implement process supervision and limited catch-up**

```python
try:
    return await asyncio.wait_for(worker.transcribe(window), timeout=timeout)
except TimeoutError:
    worker.terminate()
    worker.join(timeout=5)
    coordinator.retry(window.index)
    recovery = spawn_worker()
    catchup = spawn_worker() if memory_gate.allows_second_worker() else None
```

Never attempt to cancel a running MLX call inside the same process. On cancellation, terminate and join recovery and catch-up workers before returning. After the missing window succeeds, shut down catch-up mode and retain exactly one worker.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/unit/transcription/test_worker_supervisor.py tests/integration/test_transcription_recovery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/transcription/worker_supervisor.py backend/src/audio_memory/transcription/engine.py backend/tests/unit/transcription/test_worker_supervisor.py backend/tests/integration/test_transcription_recovery.py
git commit -m "feat: restart timed out Whisper windows safely"
```

### Task 6: Real event-driven progress and ETA

**Files:**
- Modify: `backend/src/audio_memory/transcription/eta.py`
- Modify: `backend/src/audio_memory/uploads/service.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Test: `backend/tests/unit/transcription/test_eta.py`
- Test: `backend/tests/integration/test_upload_jobs.py`

**Interfaces:**
- Produces: task-local `PreparationProgressTracker` with `vad_scanned`, `window_prepared`, `whisper_started`, `transcript_committed`, and `current_percent`.
- API remains `progress_percent: int`; no technical phase names are exposed.

- [ ] **Step 1: Write failing monotonic progress tests**

```python
tracker.vad_scanned(50_000, total_ms=100_000)
assert tracker.current_percent == 1
tracker.window_prepared()
assert tracker.current_percent == 4
tracker.whisper_started()
assert tracker.current_percent == 5
tracker.transcript_committed(end_ms=2_000, total_ms=100_000)
assert tracker.current_percent == 5
```

Assert nonzero progress appears after the first progress event, progress never exceeds5 before a transcript, never regresses, and active-job JSON contains no VAD/queue/worker mode.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_eta.py tests/integration/test_upload_jobs.py -q`

Expected: FAIL because preparation progress is not tracked.

- [ ] **Step 3: Implement truthful task-local progress**

```python
def vad_scanned(self, scanned_ms, total_ms):
    self._percent = max(self._percent, min(3, int(scanned_ms * 3 / total_ms)))

def window_prepared(self):
    self._percent = max(self._percent, 4)

def whisper_started(self):
    self._percent = max(self._percent, 5)
```

Merge this value with committed source-time progress using `max`; do not use elapsed-time animation. Clear the tracker on completion, terminal failure, and cancellation.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/unit/transcription/test_eta.py tests/integration/test_upload_jobs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/transcription/eta.py backend/src/audio_memory/uploads/service.py backend/src/audio_memory/api/jobs.py backend/src/audio_memory/transcription/engine.py backend/tests/unit/transcription/test_eta.py backend/tests/integration/test_upload_jobs.py
git commit -m "feat: report real streaming preparation progress"
```

### Task 7: Immediate temporary-file cleanup and cancellation closure

**Files:**
- Modify: `backend/src/audio_memory/transcription/window_pipeline.py`
- Modify: `backend/src/audio_memory/transcription/worker_supervisor.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Test: `backend/tests/unit/transcription/test_window_pipeline.py`
- Test: `backend/tests/integration/test_diarization_pipeline.py`

**Interfaces:**
- Produces: `WindowArtifactRegistry.register(path)`, `release_after_commit(path)`, and `cleanup_all()`.
- Invariant: no more than four registered WAVs; every terminal path removes all remaining artifacts and joins child processes within5 seconds.

- [ ] **Step 1: Write failing cleanup and cancellation tests**

```python
await pipeline.run(fake_windows(20), slow_whisper)
assert registry.peak_count <= 4
assert all(not path.exists() for path in registry.released_paths)

task.cancel()
with pytest.raises(asyncio.CancelledError):
    await task
assert registry.current_count == 0
assert process_probe.orphaned_children == []
assert cancel_elapsed < 5
```

Cover cancellation during VAD, ffmpeg, diarization, normal Whisper, recovery Whisper, and catch-up Whisper.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_window_pipeline.py tests/integration/test_diarization_pipeline.py -q`

Expected: FAIL because files survive until whole-job cleanup and the unified registry does not exist.

- [ ] **Step 3: Implement immediate release and terminal cleanup**

```python
result = await supervisor.transcribe(prepared)
await transcription_service.commit_window(...)
await registry.release_after_commit(prepared.wav_path)

async with pipeline_lifecycle() as lifecycle:
    ...
# __aexit__: set stop event, terminate children, await ffmpeg, cleanup_all
```

Only release a successful window after its atomic commit. On retry, retain or recreate the source window explicitly; never delete an artifact still required by recovery.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `cd backend && uv run pytest tests/unit/transcription/test_window_pipeline.py tests/integration/test_diarization_pipeline.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/transcription/window_pipeline.py backend/src/audio_memory/transcription/worker_supervisor.py backend/src/audio_memory/transcription/engine.py backend/tests/unit/transcription/test_window_pipeline.py backend/tests/integration/test_diarization_pipeline.py
git commit -m "fix: clean streaming transcription artifacts promptly"
```

### Task 8: Full regression and real-file SLA benchmark

**Files:**
- Modify: `scripts/benchmark-local-transcription.py`
- Modify: `backend/tests/unit/transcription/test_benchmark_local_transcription.py`
- Create: `docs/testing/streaming-transcription-benchmark-2026-08-07.md`

**Interfaces:**
- Produces: diagnostics JSON containing commit, audio SHA-256, cold/warm marker, first-progress seconds, first-transcript seconds, total seconds, Whisper RTF, diarization seconds, peak memory, peak temporary bytes/files, cancellation seconds, and overlap evidence; never path or transcript text.
- Acceptance: median total regression no worse than +5%; first progress ≤10s; first transcript ≤60s; memory <85%; temp WAV count ≤4; cancellation ≤5s; no missing windows or child processes.

- [ ] **Step 1: Extend the benchmark contract test first**

```python
assert report["first_progress_seconds"] <= 10
assert report["first_transcript_seconds"] <= 60
assert report["peak_temp_file_count"] <= 4
assert report["window_coverage"]["missing"] == []
assert "audio_path" not in report
assert "transcript" not in report
```

- [ ] **Step 2: Run benchmark unit test and verify RED**

Run: `cd backend && uv run pytest tests/unit/transcription/test_benchmark_local_transcription.py -q`

Expected: FAIL because the new measurements are absent.

- [ ] **Step 3: Add measurement and three-run comparison**

```python
median_total = statistics.median(run["total_seconds"] for run in warm_runs)
regression = (median_total - baseline_total) / baseline_total
passed = regression <= 0.05 and first_transcript <= 60 and peak_memory_ratio < 0.85
```

The benchmark must identify the pre-streaming baseline commit and candidate commit, run the same 208 MB file with identical configuration, record cold start separately, and prove first Whisper overlaps later VAD/window preparation timestamps.

- [ ] **Step 4: Run all automated verification**

Run: `cd backend && uv run pytest -q`

Run: `cd prototype && npm test`

Run: `cd prototype && npm run build`

Expected: all commands pass with no new warnings or failures.

- [ ] **Step 5: Run the real benchmark and inspect child processes**

Run: `backend/.venv/bin/python scripts/benchmark-local-transcription.py '/Users/liujinxin/Downloads/07月30日 21-07 Pokee SE-audio.mp3' --runs 3 --baseline-commit 066ba85`

Run: `ps -axo pid,ppid,state,command | rg 'ffmpeg|mlx|whisper|vad|diarization'`

Expected: report meets hard gates; no task-owned orphan or zombie process remains. If the median improvement is below10% but regression is no worse than+5%, record the outcome as first-round pass with the total-time target unmet.

- [ ] **Step 6: Write evidence and commit**

```bash
git add scripts/benchmark-local-transcription.py backend/tests/unit/transcription/test_benchmark_local_transcription.py docs/testing/streaming-transcription-benchmark-2026-08-07.md
git commit -m "test: verify streaming transcription SLA"
```
