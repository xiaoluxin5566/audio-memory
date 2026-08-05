# Audio Memory 转写预计剩余时间 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本地 Whisper 转写卡片中展示基于最近三个真实分块速度动态计算的预计剩余时间。

**Architecture:** 新增进程内 `TranscriptionEtaTracker`，由 Whisper 引擎在有效分块完成后记录音频时长与单调时钟耗时。`UploadService` 在读取任务时结合数据库真实进度计算 ETA，并通过现有 Job API 返回；React 仅负责中文格式化，不自行估速。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、MLX Whisper、pytest；React 19、JavaScript、Playwright。

## Global Constraints

- 首个有效分块完成前展示“正在估算剩余时间…”。
- 只使用最近最多 3 个有效分块样本，预计值不持久化到 SQLite。
- 中断、失败、取消、完成或进入模型分析时清除 ETA 样本。
- 恢复任务保留转写进度，但 ETA 必须重新采样。
- 前端复用现有 1.2 秒任务轮询，不新增请求。
- 预计不足 60 秒展示“预计不到 1 分钟”，其余按整数分钟向上取整。

---

### Task 1: 进程内 ETA 估算器

**Files:**
- Create: `backend/src/audio_memory/transcription/eta.py`
- Create: `backend/tests/unit/transcription/test_eta.py`

**Interfaces:**
- Produces: `TranscriptionEtaTracker.record(job_id: str, audio_ms: int, elapsed_seconds: float) -> None`。
- Produces: `TranscriptionEtaTracker.estimate_seconds(job_id: str, remaining_ms: int) -> int | None`。
- Produces: `TranscriptionEtaTracker.clear(job_id: str) -> None`。

- [ ] **Step 1: Write failing rolling-window tests**

```python
def test_eta_uses_only_three_latest_valid_samples():
    tracker = TranscriptionEtaTracker()
    tracker.record("job", 300_000, 30)
    tracker.record("job", 300_000, 20)
    tracker.record("job", 300_000, 10)
    tracker.record("job", 300_000, 5)
    assert tracker.estimate_seconds("job", 600_000) == 24

def test_eta_rejects_invalid_samples_and_clears():
    tracker = TranscriptionEtaTracker()
    tracker.record("job", 0, 10)
    tracker.record("job", 300_000, 0)
    assert tracker.estimate_seconds("job", 600_000) is None
    tracker.record("job", 300_000, 30)
    tracker.clear("job")
    assert tracker.estimate_seconds("job", 600_000) is None
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/transcription/test_eta.py -v`

Expected: FAIL because `TranscriptionEtaTracker` does not exist.

- [ ] **Step 3: Implement bounded sample tracking**

```python
from collections import defaultdict, deque

class TranscriptionEtaTracker:
    def __init__(self) -> None:
        self._samples = defaultdict(lambda: deque(maxlen=3))

    def record(self, job_id: str, audio_ms: int, elapsed_seconds: float) -> None:
        if audio_ms > 0 and elapsed_seconds > 0:
            self._samples[job_id].append((audio_ms, elapsed_seconds))

    def estimate_seconds(self, job_id: str, remaining_ms: int) -> int | None:
        samples = self._samples.get(job_id)
        if not samples or remaining_ms < 0:
            return None
        audio_ms = sum(item[0] for item in samples)
        elapsed = sum(item[1] for item in samples)
        return max(0, round((remaining_ms / audio_ms) * elapsed))

    def clear(self, job_id: str) -> None:
        self._samples.pop(job_id, None)
```

- [ ] **Step 4: Run unit tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/transcription/test_eta.py -v`

Expected: PASS.

Commit: `git commit -m "feat: add transcription ETA estimator"`

---

### Task 2: 分块计时、生命周期和 Job API

**Files:**
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Modify: `backend/src/audio_memory/uploads/service.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `backend/tests/integration/test_transcription_recovery.py`
- Modify: `backend/tests/integration/test_upload_jobs.py`

**Interfaces:**
- Consumes: `TranscriptionEtaTracker` from Task 1.
- Produces Job fields: `eta_state: Literal["estimating", "ready", "unavailable"]` and `eta_seconds: int | None`.

- [ ] **Step 1: Write failing API and lifecycle tests**

```python
async def test_transcribing_job_exposes_dynamic_eta(job_client):
    tracker.record(job_id, audio_ms=300_000, elapsed_seconds=30)
    response = await client.get(f"/api/jobs/{job_id}")
    assert response.json()["eta_state"] == "ready"
    assert response.json()["eta_seconds"] == 70

async def test_resume_keeps_progress_but_resets_eta(database, tracker, service):
    tracker.record(job_id, 300_000, 30)
    await service.resume_job(job_id, engine)
    assert tracker.estimate_seconds(job_id, 600_000) is None
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_upload_jobs.py tests/integration/test_transcription_recovery.py -v`

Expected: FAIL because Job views do not expose ETA and services do not share a tracker.

- [ ] **Step 3: Wire one shared tracker through application services**

In `main.py`, create one tracker per application lifespan and pass it to both services:

```python
eta_tracker = TranscriptionEtaTracker()
app.state.upload_service = UploadService(database, paths, job_events, eta_tracker=eta_tracker)
whisper_engine = MLXWhisperEngine(database, paths, eta_tracker=eta_tracker)
app.state.transcription_service = TranscriptionService(database, eta_tracker=eta_tracker)
```

- [ ] **Step 4: Record valid chunk timing outside database transactions**

In `MLXWhisperEngine.transcribe_file`, use `time.monotonic()` immediately before each worker request. After all valid segments from that chunk have been yielded and therefore committed by `TranscriptionService`, record:

```python
elapsed = time.monotonic() - started
audio_ms = min(
    WHISPER_CHUNK_SECONDS * 1000,
    max(0, int(file.duration_ms or 0) - chunk_index * WHISPER_CHUNK_SECONDS * 1000),
)
if valid_count > 0:
    self.eta_tracker.record(file.job_id, audio_ms, elapsed)
```

- [ ] **Step 5: Expose estimating, ready and unavailable states**

`UploadService.get_job` computes `remaining_ms = max(0, total_ms - processed_ms)` and returns:

```python
if job.stage == JobStage.TRANSCRIBING.value:
    eta_seconds = self.eta_tracker.estimate_seconds(job.id, remaining_ms)
    eta_state = "ready" if eta_seconds is not None else "estimating"
else:
    eta_seconds = None
    eta_state = "unavailable"
```

Add the same fields with defaults to `UploadJobView` and API `JobView`.

- [ ] **Step 6: Clear samples at terminal boundaries**

Clear at resume start, transcription exception/cancellation, transition to analysis and job cancellation. Do not clear the database transcript checkpoints.

- [ ] **Step 7: Run backend tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/transcription tests/integration/test_upload_jobs.py tests/integration/test_transcription_recovery.py -v`

Expected: PASS, including preserved progress and reset ETA after recovery.

Commit: `git commit -m "feat: expose dynamic transcription ETA"`

---

### Task 3: 转写卡片文案和浏览器验收

**Files:**
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/tests/e2e/recovery.spec.js`
- Modify: `prototype/tests/e2e/upload-analysis.spec.js`

**Interfaces:**
- Consumes: Job `eta_state` and `eta_seconds` from Task 2.
- Produces: `formatEta(job) -> string` for the transcription card.

- [ ] **Step 1: Write failing formatting and recovery browser tests**

```javascript
test('transcription estimates until the first sample then shows remaining minutes', async ({ page }) => {
  // First poll: eta_state=estimating; second poll: ready with eta_seconds=901.
  await expect(page.getByText('正在估算剩余时间…')).toBeVisible()
  await expect(page.getByText('预计还需约 16 分钟')).toBeVisible()
})

test('resumed transcription discards stale ETA', async ({ page }) => {
  await page.getByRole('button', { name: '继续分析' }).click()
  await expect(page.getByText('正在估算剩余时间…')).toBeVisible()
})
```

- [ ] **Step 2: Run browser tests and verify failure**

Run: `cd prototype && npx playwright test tests/e2e/recovery.spec.js tests/e2e/upload-analysis.spec.js --reporter=line`

Expected: FAIL because ETA copy is absent.

- [ ] **Step 3: Implement presentation-only formatting**

```javascript
export function formatEta(job) {
  if (job.stage === 'analyzing') return '正在生成分析结果…'
  if (job.eta_state !== 'ready' || job.eta_seconds == null) {
    return '正在估算剩余时间…'
  }
  if (job.eta_seconds < 60) return '预计不到 1 分钟'
  return `预计还需约 ${Math.ceil(job.eta_seconds / 60)} 分钟`
}
```

Render this line below the progress bar in `JobPanel`. Do not run client-side timers or recompute speed.

- [ ] **Step 4: Run frontend and full quality gate**

Run:

```bash
cd prototype
node --test tests/*.test.mjs
npm run build
npm run test:e2e -- --reporter=line
cd ../backend
UV_CACHE_DIR=../.uv-cache uv run pytest -q
```

Expected: all tests PASS; production build exits 0.

- [ ] **Step 5: Sync desktop runtime and verify live task response**

Copy changed backend/frontend files to `/Users/liujinxin/Desktop/音频Always on`, restart with `./scripts/start.sh`, then verify `/api/jobs/active` returns the new ETA fields. Existing audio, transcripts, Keychain, Prompt files and history must remain unchanged.

- [ ] **Step 6: Commit**

Commit: `git commit -m "feat: show transcription remaining time"`
