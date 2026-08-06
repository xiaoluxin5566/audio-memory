# Audio Memory PRD V1.1 Remaining Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重写已经跑通的安装、Keychain、上传和 Whisper 主链路的前提下，完成 PRD V1.1 尚未落地的逾期待办、结构化事件分析、版本化发布和历史批量重分析。

**Architecture:** 保留 `AnalysisJob → JobFile → Transcript` 作为不可变原始上传层。`AnalysisVersion` 从创建起关联 `source_job_id`；首次分析发布成功时创建正式 `Batch` 并挂接版本，之后同一 Batch 可拥有多个版本，信息流通过 `current_analysis_version_id` 读取当前结果。所有远程模型工作由一个 `AnalysisTaskCoordinator` 串行调度；普通新上传优先，历史重分析复用结构化转写与兼容事件地图，并按原始上传批次原子切换分析版本。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、SQLite、httpx、MLX Whisper、sherpa-onnx、React 19、Node test、Playwright、pytest。

## Global Constraints

- 产品事实来源是 `docs/PRD.md` V1.1；旧规格、旧验收清单和现有代码与它冲突时必须同步修正。
- 第一阶段只支持 macOS Apple Silicon、MP3/AAC 和用户主动上传已有音频。
- Kimi、DeepSeek、OpenAI 使用同一本地转写；原始音频不得发送给模型厂商。
- 截止时间已过只表示逾期：红色强调并在未完成区优先展示，绝不自动完成。
- 用户每次完整打开网页校验一次已配置厂商；站内路由切换不得重复校验。
- 六个场景全部成功并通过证据校验后才发布当前版本，不展示场景半成品。
- 原始音频和结构化转写只保存一份；历史重分析不得复制或重新运行 Whisper。
- QA、反馈和旧卡片永久关联产生它们的分析版本；新卡片不显示旧版本 QA。
- 用户编辑、手动完成或删除过的待办不得被历史重分析覆盖或复活。
- 第一阶段不向用户展示个人标签，但必须从第一批有效音频建立隐藏画像。
- 同一时刻只执行一个远程模型任务；Whisper 可并行，新上传模型分析优先于历史队列。
- 所有会产生费用或修改数据的 API 必须同源、具有本地会话令牌和幂等语义。
- 使用测试先行；每个生产行为必须先看到对应测试因行为缺失而失败。

## Existing Foundation Not Reimplemented

The current branch already has tested implementations for the local FastAPI runtime, APFS instance lock, SQLite migration 0001, Keychain-backed Kimi/DeepSeek/OpenAI configuration, real provider validation, MP3/AAC upload, recoverable MLX Whisper transcription, transcription ETA, base Prompt file versioning, atomic first-batch publication, feed/history/QA/feedback/clear APIs, React routes and macOS install/start/doctor scripts. This plan modifies those modules only where PRD V1.1 requires new behavior; it does not rebuild them from scratch.

---

### Task 1: 修正逾期待办与网页打开校验

**Files:**
- Modify: `backend/src/audio_memory/content/service.py`
- Modify: `backend/src/audio_memory/api/content.py`
- Modify: `backend/src/audio_memory/api/providers.py`
- Modify: `prototype/src/api/client.js`
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/hooks/useProviders.js`
- Modify: `prototype/src/App.jsx`
- Test: `backend/tests/integration/test_content_api.py`
- Test: `backend/tests/integration/test_provider_api.py`
- Test: `prototype/tests/api-state.test.mjs`
- Test: `prototype/tests/e2e/first-run.spec.js`

**Interfaces:**
- Produces: todo view `{id, text, due_at, completed, overdue}`.
- Produces: `PATCH /api/todos/{id}` accepting `text`, `due_at`, and `completed`.
- Produces: `POST /api/providers/validate-configured` returning current non-sensitive provider states.
- Consumes: existing `ProviderStateCoordinator.validate_saved(provider_id)` task de-duplication.

- [ ] **Step 1: Write failing overdue tests**

```python
async def test_expired_todo_stays_incomplete_and_is_marked_overdue(client, seeded_todo):
    seeded_todo.due_at = "2026-08-04T08:00:00+00:00"
    payload = (await client.get("/api/feed")).json()
    todo = next(item for item in payload["todos"] if item["id"] == seeded_todo.id)
    assert todo["completed"] is False
    assert todo["overdue"] is True

async def test_future_or_missing_due_date_is_not_overdue(client, seeded_todo):
    response = await client.patch(
        f"/api/todos/{seeded_todo.id}",
        json={"due_at": "2099-08-05T09:00:00+08:00"},
    )
    assert response.json()["overdue"] is False
```

- [ ] **Step 2: Run tests and verify the old auto-completion behavior fails**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_content_api.py -q`

Expected: FAIL because `_complete_expired_todos` mutates `completed=True` and the API lacks `overdue`/`due_at` editing.

- [ ] **Step 3: Implement computed overdue state without database mutation**

Delete the feed-time call to `_complete_expired_todos`. Add one pure helper and use it both after update and in feed views:

```python
def is_overdue(*, due_at: str | None, completed: bool, now: datetime) -> bool:
    if completed or not due_at:
        return False
    try:
        due = datetime.fromisoformat(due_at)
    except ValueError:
        return False
    if due.tzinfo is None:
        return False
    return due.astimezone(UTC) < now
```

Order todos by: incomplete overdue, incomplete non-overdue, completed; keep newest first within a group. `due_at=""` from the UI normalizes to `None`; non-empty values must parse as timezone-aware ISO 8601 or return HTTP 422.

- [ ] **Step 4: Write failing one-page-load validation tests**

```python
async def test_validate_configured_only_calls_keychain_configured_providers(client, validators):
    response = await client.post("/api/providers/validate-configured")
    assert response.status_code == 200
    assert validators["deepseek"].calls == 1
    assert validators["kimi"].calls == 0
```

Frontend test spies on `validateConfiguredProviders`: one initial `App` mount calls it once; navigating feed → history → prompts does not call it again.

- [ ] **Step 5: Implement initial-page validation and todo editing UI**

Add `api.validateConfiguredProviders()`. `useProviders` calls it once after the initial provider state load and then refreshes provider state. Keep the backend startup validation unchanged; coordinator in-flight de-duplication prevents duplicate network calls when page open overlaps startup.

Todo edit mode exposes text plus `datetime-local`; convert a non-empty local value to ISO before PATCH. Render `已逾期` in red when `overdue=true` and leave the checkbox unchecked.

- [ ] **Step 6: Run focused regression tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_content_api.py tests/integration/test_provider_api.py -q && cd ../prototype && node --test tests/api-state.test.mjs && npm run test:e2e -- --grep "provider|todo" --reporter=line`

Expected: all focused tests PASS; no feed request writes todo completion state.

Commit: `git commit -m "fix: keep overdue todos incomplete"`

---

### Task 2: VAD 优先的结构化转写与本地说话人分段

**Files:**
- Create: `backend/migrations/versions/0002_structured_transcript.py`
- Create: `backend/src/audio_memory/diarization/__init__.py`
- Create: `backend/src/audio_memory/diarization/engine.py`
- Create: `backend/src/audio_memory/diarization/alignment.py`
- Modify: `backend/src/audio_memory/models.py`
- Modify: `backend/src/audio_memory/config.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Modify: `backend/src/audio_memory/uploads/probe.py`
- Modify: `backend/src/audio_memory/uploads/service.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `scripts/install.sh`
- Test: `backend/tests/unit/diarization/test_alignment.py`
- Test: `backend/tests/integration/test_diarization_pipeline.py`
- Test: `backend/tests/integration/test_database_schema.py`
- Test: `tests/install-smoke.sh`

**Interfaces:**
- Produces: `Transcript.segment_uid: str`, `speaker_id: str | None`, absolute segment timestamps, and optional word timestamps for selectively refined evidence segments.
- Produces: `JobFile.recording_started_at`, `recording_time_source=embedded|file_modified|unknown`, and `timezone`.
- Produces: `SpeechInterval(start_ms, end_ms)` and a persisted speech-only-to-source time mapping; analysis always cites the original audio timeline.
- Produces: `VoiceActivityDetector.detect(path) -> list[SpeechInterval]` and `SelectiveRefiner.refine(segment_uids) -> list[AlignedTranscriptSegment]`.
- Produces: `OfflineDiarizationEngine.diarize(path) -> list[SpeakerTurn]`.
- Produces: `assign_speakers(words, turns) -> list[AlignedTranscriptSegment]`.

- [ ] **Step 1: Write failing migration and alignment tests**

```python
assert {"segment_uid", "speaker_id"} <= transcript_columns
assert {"recording_started_at", "recording_time_source", "timezone"} <= job_file_columns

def test_word_uses_turn_with_largest_overlap():
    words = [Word("你好", 900, 1300)]
    turns = [SpeakerTurn(0, 1000, "speaker_00"), SpeakerTurn(1000, 2000, "speaker_01")]
    assert assign_speakers(words, turns)[0].speaker_id == "speaker_01"
```

- [ ] **Step 2: Verify schema and module tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_database_schema.py tests/unit/diarization/test_alignment.py -q`

Expected: FAIL because columns and diarization modules do not exist.

- [ ] **Step 3: Add SQLite-compatible migration and media-time policy**

Backfill `segment_uid = job_file_id || ':' || segment_index` and create a unique index. Embedded `creation_time` overrides browser `File.lastModified`; browser time is stored as `file_modified` but may not resolve relative deadlines. Never substitute upload or analysis time.

- [ ] **Step 4: Implement bounded VAD-first decoding and sherpa-onnx diarization**

Add `sherpa-onnx>=1.10.28,<2`; install local VAD, official pyannote INT8 segmentation and Chinese 3D-Speaker embedding models. Run VAD first with configurable padding around detected speech. Decode and diarize speech-bearing intervals only, in bounded windows no longer than 30 minutes with 30-second overlap. Preserve a lossless mapping from every compacted speech interval back to its original source offsets. Reconcile adjacent labels only when overlap speech exceeds 2 seconds; otherwise allocate a new global speaker label instead of guessing.

- [ ] **Step 5: Default to segment timestamps and selectively refine evidence**

The default fast path calls MLX Whisper only for VAD-confirmed speech and uses sentence/segment timestamps. Convert every result back to file-relative milliseconds before persistence and event analysis. Do not enable full-file word timestamps by default. Only evidence segments selected for high-value conclusions or ambiguous boundaries may be re-decoded with `word_timestamps=True`; refined words must retain the same source-timeline mapping. Diarization runs only on speech-bearing intervals. If VAD fails, fall back to bounded full-audio segment decoding; if diarization or selective refinement fails, preserve all default text with `speaker_id=null` or segment-level timestamps, record a normalized diagnostic and continue analysis.

- [ ] **Step 6: Run five-hour bounded-memory and installer tests**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/diarization tests/integration/test_diarization_pipeline.py -q && cd .. && bash tests/install-smoke.sh`

Expected: PASS; synthetic five-hour sparse-audio test sends only detected speech plus padding to Whisper, never allocates five hours of PCM at once, preserves original offsets across compacted intervals, and installer is idempotent.

Commit: `git commit -m "feat: add structured speaker-aware transcripts"`

---

### Task 3: 分析版本、待办来源与旧数据迁移

**Files:**
- Create: `backend/migrations/versions/0003_analysis_versions.py`
- Create: `backend/src/audio_memory/analysis/versions.py`
- Modify: `backend/src/audio_memory/models.py`
- Modify: `backend/src/audio_memory/repositories.py`
- Test: `backend/tests/integration/test_analysis_version_migration.py`
- Test: `backend/tests/integration/test_database_schema.py`

**Interfaces:**
- Produces: `AnalysisSnapshot(provider_id: str, model_id: str, credential_generation: int, prompt_snapshot: dict[str, object], profile_snapshot: list[dict[str, object]], fixed_rules_hash: str)`.
- Produces: `AnalysisVersion(id, source_job_id, batch_id, provider_id, model_id, credential_generation, prompt_snapshot_json, profile_snapshot_json, event_map_json, event_map_hash, staged_results_json, status, error_code, reanalysis_batch_id, created_at, completed_at)`; `batch_id` is null until the first successful publication.
- Produces: `Batch.current_analysis_version_id`.
- Produces: `Card.analysis_version_id` and `TodoCandidate.analysis_version_id`.
- Produces: `TodoTombstone(source_fingerprint, deleted_at)`.
- Produces: `AnalysisVersionRepository.create_attempt(*, source_job_id: str, batch_id: str | None, snapshot: AnalysisSnapshot, reanalysis_batch_id: str | None) -> AnalysisVersion`.
- Produces: `AnalysisVersionRepository.mark_current(*, batch_id: str, version_id: str) -> None`.
- Produces: `AnalysisVersionRepository.current_for_batch(batch_id: str) -> AnalysisVersion | None`.

- [ ] **Step 1: Write failing legacy-migration tests**

Seed a version-0002 database with one completed batch, two cards, QA and todos, run migration 0003, then assert:

```python
assert batch.current_analysis_version_id is not None
assert all(card.analysis_version_id == batch.current_analysis_version_id for card in cards)
assert qa_messages[0].card_id == old_card_id
assert legacy_todo.user_edited is True
assert legacy_todo.source_fingerprint.startswith("legacy:")
```

- [ ] **Step 2: Verify migration test fails before the new schema**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_analysis_version_migration.py -q`

Expected: FAIL because analysis-version tables and columns do not exist.

- [ ] **Step 3: Implement schema without copying audio or transcripts**

Create `analysis_versions`, `todo_candidates`, `todo_tombstones`, `profile_candidates`, `reanalysis_batches`, and `reanalysis_items`. Every version has non-null `source_job_id`; `batch_id` stays nullable until first publication. Add nullable version FKs, backfill one initial completed version per existing Batch using its `job_id`, provider/model and job Prompt snapshot, attach existing cards, set current pointer, then enforce non-null card-version writes using application validation. Do not duplicate `JobFile`, `Transcript`, audio paths or QA rows.

Existing todos lack reliable provenance; mark them `user_edited=true`, set `source_fingerprint=legacy:{todo_id}`, and protect them from automatic replacement.

- [ ] **Step 4: Implement repository invariants**

`mark_current(batch_id=batch_id, version_id=version_id)` must verify the version belongs to the target batch and is `completed`, then update `Batch.current_analysis_version_id` in the same transaction. At most one `running` AnalysisVersion per `source_job_id`; add a partial unique index.

- [ ] **Step 5: Run migration upgrade/rollback safety tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run alembic upgrade head && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_analysis_version_migration.py tests/integration/test_database_schema.py -q`

Expected: current feed data, card IDs and QA survive migration; no new audio file appears.

Commit: `git commit -m "feat: add versioned analysis storage"`

---

### Task 4: 冻结四层 Prompt、事件地图与严格场景 Schema

**Files:**
- Create: `backend/src/audio_memory/prompts/system.md`
- Create: `backend/src/audio_memory/prompts/common-scene.md`
- Create: `backend/src/audio_memory/prompts/event-map.md`
- Create: `backend/src/audio_memory/prompts/event_schema.py`
- Create: `backend/src/audio_memory/prompts/evidence.py`
- Replace: `backend/src/audio_memory/prompts/schemas.py`
- Replace: `backend/src/audio_memory/prompts/defaults/todo.md`
- Replace: `backend/src/audio_memory/prompts/defaults/meeting.md`
- Replace: `backend/src/audio_memory/prompts/defaults/parenting.md`
- Replace: `backend/src/audio_memory/prompts/defaults/content.md`
- Replace: `backend/src/audio_memory/prompts/defaults/growth.md`
- Replace: `backend/src/audio_memory/prompts/defaults/inspiration.md`
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Modify: `backend/src/audio_memory/prompts/store.py`
- Test: `backend/tests/unit/prompts/test_event_schema.py`
- Test: `backend/tests/unit/prompts/test_scene_schemas.py`
- Test: `backend/tests/unit/prompts/test_evidence_integrity.py`
- Test: `backend/tests/unit/prompts/test_composer.py`
- Test: `backend/tests/integration/test_prompt_api.py`

**Interfaces:**
- Produces: `EventMap`, `Event`, `UserSpeaker`, and six strict `SceneResult` variants.
- Produces: `PromptComposer.compose_event_map(*, transcript: list[dict[str, object]], profile: list[dict[str, object]], schema: dict[str, object]) -> ModelRequest`.
- Produces: `PromptComposer.compose_scene(scene_id: str, *, transcript: list[dict[str, object]], event_map: EventMap, profile: list[dict[str, object]], prompt: PromptDocument, schema: dict[str, object]) -> ModelRequest`.
- Produces: `validate_evidence_integrity(result, event_map, segment_ids)`.

- [ ] **Step 1: Write failing threshold and cross-reference tests**

```python
with pytest.raises(ValidationError):
    ParentingIssue(
        finding_id="finding_parenting_event_003_01",
        event_id="event_003",
        content="可能存在问题",
        reasoning="依据不足",
        evidence_segment_ids=["seg_003"],
        confidence=0.59,
    )

with pytest.raises(EvidenceIntegrityError):
    validate_evidence_integrity(result_with_unknown_segment, event_map, {"seg_001"})
```

Also test user identity `<0.70`, single-event growth `<0.80`, nonexistent basis IDs, cross-event evidence, vague titles and media action calls misclassified as todos.

- [ ] **Step 2: Verify strict-schema tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts -q`

Expected: FAIL because current generic Schema lacks event and evidence invariants.

- [ ] **Step 3: Implement strict Pydantic contracts from the frozen specification**

Use `extra="forbid"`, literal enums, length/range constraints and model validators. Todo emits no card; meetings allow multiple cards by independent event; parenting/content/growth/inspiration allow at most one aggregate card and keep event-separated details.

- [ ] **Step 4: Implement immutable Prompt layers and safe upgrades**

Composition order: system rules → event-map or common-scene rules → editable scene Prompt → JSON Schema. Wrap transcript/event/profile as untrusted data. Preserve user-edited `current.md`; replace an old default only when its SHA-256 matches a known packaged legacy default, archive the old default and update metadata idempotently.

- [ ] **Step 5: Run Prompt and API suites and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts tests/integration/test_prompt_api.py -q`

Expected: all schema, evidence, injection and legacy Prompt upgrade tests PASS.

Commit: `git commit -m "feat: freeze evidence-backed scene prompts"`

---

### Task 5: 统一远程模型调度、事件地图和版本检查点

**Files:**
- Create: `backend/src/audio_memory/analysis/task_coordinator.py`
- Create: `backend/src/audio_memory/analysis/events.py`
- Create: `backend/src/audio_memory/analysis/runner.py`
- Modify: `backend/src/audio_memory/analysis/provider.py`
- Modify: `backend/src/audio_memory/analysis/orchestrator.py`
- Modify: `backend/src/audio_memory/providers/coordinator.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Test: `backend/tests/unit/analysis/test_task_coordinator.py`
- Test: `backend/tests/integration/test_event_map_pipeline.py`
- Test: `backend/tests/integration/test_analysis_pipeline.py`

**Interfaces:**
- Produces: `AnalysisRequest(source_job_id: str, source_batch_id: str | None, provider_id: str, model_id: str, credential_generation: int, prompt_snapshot: dict[str, object], profile_snapshot: list[dict[str, object]], priority: int)`; `source_batch_id` is null for a not-yet-published upload.
- Produces: `AnalysisTaskCoordinator.submit_new_upload(request)` and `submit_reanalysis(request)`.
- Produces: internal `AnalysisTaskCoordinator.next_request() -> AnalysisRequest` for the single worker.
- Produces: `AnalysisRunner.run(version_id) -> AnalysisOutcome`.
- Produces: `ProviderStateCoordinator.snapshot_active_with_generation() -> tuple[ProviderState, int]` while holding the existing activation/state locks.
- Priority: new upload `0`, history reanalysis `10`; one active remote task globally.

- [ ] **Step 1: Write failing priority, exclusion and recovery tests**

```python
await coordinator.submit_reanalysis(old_request)
await coordinator.submit_new_upload(new_request)
assert await coordinator.next_request() == new_request

with pytest.raises(AlreadyRunningError):
    await coordinator.submit_reanalysis(same_source_job_request)
```

Test service restart converts running requests to pending, and a stopped history batch does not yield a new item.

- [ ] **Step 2: Verify coordinator tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_task_coordinator.py -q`

Expected: FAIL because remote analysis is currently launched directly with `asyncio.create_task`.

- [ ] **Step 3: Implement one-worker priority coordinator**

Persist queue authority in SQLite; use one in-process async condition only to wake the worker. Never hold a database or coordinator lock during network calls. New upload model analysis enters priority 0 after Whisper; history items enter priority 10 one at a time.

- [ ] **Step 4: Implement event-map-first runner and checkpoints**

Order: load structured transcript → reuse/generate EventMap → six scene calls → profile candidates → cross-reference validation → publisher. Save event map and each scene to `AnalysisVersion.staged_results_json` immediately. On invalid JSON/Schema, make exactly one repair request.

- [ ] **Step 5: Bind snapshots and credential generation**

Every provider call uses the AnalysisVersion snapshot, not the current UI selection. If `credential_generation` changes, finish/cancel the current HTTP request, mark the version `credential_changed`, discard its unpublished scene checkpoints and pause the owning history batch. Never mix two generations in one version.

- [ ] **Step 6: Run priority, restart and provider-switch tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_task_coordinator.py tests/integration/test_event_map_pipeline.py tests/integration/test_analysis_pipeline.py -q`

Expected: one remote call at a time; new upload preempts only before the next history item; no Whisper call on model retry.

Commit: `git commit -m "feat: coordinate versioned model analysis"`

---

### Task 6: 版本化发布、待办合并、QA 和确定性画像

**Files:**
- Create: `backend/src/audio_memory/analysis/todos.py`
- Create: `backend/src/audio_memory/analysis/profile_rebuild.py`
- Modify: `backend/src/audio_memory/analysis/publisher.py`
- Modify: `backend/src/audio_memory/content/service.py`
- Modify: `backend/src/audio_memory/content/feedback.py`
- Modify: `backend/src/audio_memory/content/clear.py`
- Test: `backend/tests/unit/analysis/test_todo_reconciliation.py`
- Test: `backend/tests/unit/analysis/test_profile_rebuild.py`
- Test: `backend/tests/integration/test_atomic_batch_commit.py`
- Test: `backend/tests/integration/test_content_api.py`
- Test: `backend/tests/integration/test_feedback_and_clear.py`

**Interfaces:**
- Produces: `VersionPublisher.publish(version_id, results, profile_candidates)`.
- Produces: `reconcile_todos(batch_id, candidates, existing, tombstones)`.
- Produces: `ProfileRebuilder.rebuild(current_versions) -> list[ProfileFact]`.
- Feed only reads cards whose version equals `Batch.current_analysis_version_id`.

- [ ] **Step 1: Write failing atomic-version and QA preservation tests**

```python
assert feed_card_ids == new_version_card_ids
assert old_qa_messages_are_still_queryable(database, old_card_id)
assert no_partial_new_cards_exist_after_scene_failure(database, source_batch_id)
```

Test a new version with `should_generate=false` removes that scene from current feed without deleting the old card.

- [ ] **Step 2: Write failing todo protection tests**

Cover stable-source update, user-edited preservation, manually completed preservation, overdue-still-incomplete preservation, tombstone non-resurrection and ambiguous candidates staying separate.

- [ ] **Step 3: Implement one-transaction version publication**

For first publication, move staging audio once, create the original Batch, attach the version and insert cards/candidates in one recoverable publication operation. For reanalysis, insert new cards/candidates, reconcile global todos, mark the version completed and switch `current_analysis_version_id` in the same database transaction; never move or copy source audio.

- [ ] **Step 4: Make feed, QA and feedback version-aware**

Feed reads current cards only. An already-open old card ID remains queryable and can receive old-version QA. New cards begin with empty QA. Feedback context reads provider/model/Prompt snapshot from the card's AnalysisVersion, not mutable AnalysisJob fields.

- [ ] **Step 5: Implement two-pass profile rebuilding**

Scene calls in a historical batch use the frozen pre-batch profile. After items finish, rebuild from current AnalysisVersions into a candidate set and atomically swap active profile facts. Failure keeps the old profile and yields `content_completed_profile_failed`.

- [ ] **Step 6: Run version/content regression and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_todo_reconciliation.py tests/unit/analysis/test_profile_rebuild.py tests/integration/test_atomic_batch_commit.py tests/integration/test_content_api.py tests/integration/test_feedback_and_clear.py -q`

Expected: current feed changes atomically; old QA/feedback context survives; no todo user state regresses.

Commit: `git commit -m "feat: publish versioned analysis safely"`

---

### Task 7: 本地会话、同源校验和幂等写接口

**Files:**
- Create: `backend/src/audio_memory/security/local_session.py`
- Create: `backend/src/audio_memory/security/middleware.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `prototype/src/api/client.js`
- Modify: `prototype/src/api/upload.js`
- Test: `backend/tests/integration/test_local_web_security.py`
- Test: `prototype/tests/api-client.test.mjs`

**Interfaces:**
- Produces: `GET /api/session -> {token}` for an allowed local origin.
- Requires: `X-Audio-Memory-Session` and `Idempotency-Key` on paid/destructive POST, PUT, PATCH and DELETE routes.
- Allows hosts: `127.0.0.1:<configured-port>` and `localhost:<configured-port>` only.

- [ ] **Step 1: Write failing cross-origin and replay tests**

```python
assert (await client.post("/api/history/reanalysis-batches", headers={"Origin": "https://evil.example"})).status_code == 403
assert first.json() == replay.json()
assert provider_client.call_count == 1
```

- [ ] **Step 2: Verify tests fail against the current unrestricted local API**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_local_web_security.py -q`

Expected: FAIL because there is no local session or Origin/Host enforcement.

- [ ] **Step 3: Implement in-memory session and bounded idempotency cache**

Generate 256-bit random tokens per frontend page session, store only token hashes in process memory and expire after 24 hours or process exit. Cache mutation responses by `(session_hash, route, idempotency_key)` for 24 hours with a maximum of 1000 entries; reject a reused key with a different request-body hash.

- [ ] **Step 4: Update the browser client**

Fetch `/api/session` before other mutating requests, keep the token only in JS memory, attach a new UUID idempotency key per user action and reuse it only when retrying that same action. Expose `getLocalSessionHeaders(idempotencyKey)` from `api/client.js` and use it in both fetch requests and the XHR upload path. Read-only GET routes do not require the token.

- [ ] **Step 5: Run security and existing API suites and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_local_web_security.py tests/integration/test_provider_api.py tests/integration/test_content_api.py -q && cd ../prototype && node --test tests/api-client.test.mjs`

Expected: same-origin product works; cross-origin mutations, missing tokens and mismatched replays fail.

Commit: `git commit -m "feat: protect local paid actions"`

---

### Task 8: 历史重分析预览、批次 API 和后台执行

**Files:**
- Create: `backend/src/audio_memory/reanalysis/__init__.py`
- Create: `backend/src/audio_memory/reanalysis/types.py`
- Create: `backend/src/audio_memory/reanalysis/preview.py`
- Create: `backend/src/audio_memory/reanalysis/service.py`
- Create: `backend/src/audio_memory/reanalysis/worker.py`
- Create: `backend/src/audio_memory/api/reanalysis.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/content/clear.py`
- Test: `backend/tests/unit/reanalysis/test_preview.py`
- Test: `backend/tests/integration/test_reanalysis_api.py`
- Test: `backend/tests/integration/test_reanalysis_worker.py`

**Interfaces:**
- `GET /api/history/reanalysis-batches/preview` returns counts, character total, provider/model, Prompt summary, call range, blockers, `preview_token`, and snapshot hash.
- `POST /api/history/reanalysis-batches` creates or returns the active batch.
- `GET /api/history/reanalysis-batches/current` returns batch/item progress.
- `POST /api/history/reanalysis-batches/{id}/stop|resume|retry-profile` controls safe transitions.

- [ ] **Step 1: Write failing preview and immutable-snapshot tests**

```python
assert preview.source_batch_count == 3
assert preview.audio_file_count == 7
assert preview.estimated_calls_min == 18
assert preview.whisper_calls == 0

with pytest.raises(SnapshotChangedError):
    await service.create_batch(expired_preview_token)
```

Change a Prompt, provider generation, current history set and profile hash after preview; each must force a fresh confirmation.

- [ ] **Step 2: Verify API tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/reanalysis/test_preview.py tests/integration/test_reanalysis_api.py -q`

Expected: FAIL because reanalysis routes and service do not exist.

- [ ] **Step 3: Implement signed five-minute preview tokens**

Use a per-process random HMAC secret. Token payload includes source Batch IDs, provider/model, credential generation, Prompt hashes, fixed-rule hashes, profile hash, counts and expiry. The create endpoint recomputes the snapshot and rejects any mismatch.

- [ ] **Step 4: Implement batch/item state machine**

Create items newest-first for completed source Batches only. Persist states exactly as the design: batch `pending|running|paused|stopping|completed|completed_with_failures|content_completed_profile_failed|stopped`; item `pending|running|succeeded|failed|stopped`.

- [ ] **Step 5: Implement worker reuse and stop semantics**

For each item create a new AnalysisVersion referencing the source Batch. Reuse EventMap only when hashes, Schema, transcript version and evidence IDs match; otherwise regenerate EventMap. Never call Whisper/diarization. Stop waits for the current source Batch to finish or fail, then marks remaining items stopped.

- [ ] **Step 6: Implement provider pause/resume and profile retry**

Auth, balance, Keychain, cooldown or credential-generation errors pause the batch. New-Key resume clears unpublished scene checkpoints for the current item. Network timeout retries at most twice; schema repair once; ordinary single-item model failure records failure and continues.

- [ ] **Step 7: Coordinate clear history and restart recovery**

History clear returns HTTP 409 while a reanalysis batch is running or paused. On app startup, change `running` items to `pending`, restart the owning batch with its original snapshot and resume after current provider validation succeeds.

- [ ] **Step 8: Run complete backend reanalysis tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/reanalysis tests/integration/test_reanalysis_api.py tests/integration/test_reanalysis_worker.py -q`

Expected: all success, partial failure, stop, restart, profile-failure, generation-change and zero-Whisper assertions PASS.

Commit: `git commit -m "feat: reanalyze all history from transcripts"`

---

### Task 9: 新版场景卡片、历史重分析前端与版本切换体验

**Files:**
- Create: `prototype/src/components/ReanalysisModal.jsx`
- Create: `prototype/src/hooks/useReanalysis.js`
- Modify: `prototype/src/api/client.js`
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css`
- Test: `prototype/tests/reanalysis-state.test.mjs`
- Test: `prototype/tests/api-state.test.mjs`
- Test: `prototype/tests/detail-layout.test.mjs`
- Test: `prototype/tests/e2e/reanalysis.spec.js`
- Test: `prototype/tests/e2e/content-actions.spec.js`

**Interfaces:**
- Consumes: preview/current/stop/resume/retry-profile APIs from Task 8.
- Produces: topbar button state `disabled|idle|running|paused|stopping|finished`.
- Existing selected card retains its immutable ID until the user closes detail.

- [ ] **Step 1: Write failing scene normalizer, reanalysis state and modal tests**

```javascript
assert.equal(contentCard.details.consumedItems.length, 2)
assert.equal(JSON.stringify(contentCard).includes('inferred_title_hint'), false)
assert.equal(view.buttonLabel, '重新分析中 3/18')
assert.equal(view.canClearHistory, false)
assert.match(view.costNotice, /会调用当前模型并产生 API 费用/)
```

Test two meetings produce two cards; parenting/content/growth/inspiration aggregate cards keep event-separated detail blocks; hidden profile signals, confidence, evidence IDs, internal inferred title hints and generation reasons never render. Also test no-history disabled, file/batch counts, character count, model/Prompt display, stop progress, partial failure and profile retry.

- [ ] **Step 2: Verify frontend tests fail**

Run: `cd prototype && node --test tests/api-state.test.mjs tests/detail-layout.test.mjs tests/reanalysis-state.test.mjs`

Expected: FAIL because current normalizers expect the old generic detail payload and no reanalysis UI state exists.

- [ ] **Step 3: Implement deterministic scene presentation adapters**

Convert each strict scene payload into the existing common `DetailBlock` view type inside `api/state.js`; JSX renders only the common view type. Meeting supports multiple cards. Parenting, content, growth and inspiration preserve event grouping. Do not render hidden interest/profile signals, confidence, evidence IDs, `finding_id`, `case_id`, `generation_reason` or `inferred_title_hint`.

- [ ] **Step 4: Implement topbar entry and confirmation modal**

Place the secondary button immediately left of “清除所有历史” on all routes. Confirmation shows upload batches, audio files, text volume, current model, six Prompt versions, estimated call range, “不会重新转写” and API cost notice.

- [ ] **Step 5: Implement progress, stop, resume and completion copy**

Poll current status every 1.2 seconds only while a batch is active. Display succeeded/failed/pending/stopped counts. Disable clear history for running/paused/stopping. Stop and resume actions use stable idempotency keys per click.

- [ ] **Step 6: Preserve open old details across publication**

Feed refresh updates current cards, but `selectedCard` keeps the old card snapshot and ID until close. New cards load empty QA. Closing detail and reopening reads the new current card.

- [ ] **Step 7: Run frontend and browser tests and commit**

Run: `cd prototype && node --test tests/*.test.mjs && npm run build && npm run test:e2e -- --reporter=line`

Expected: all unit/E2E tests PASS; all three routes show the same topbar state.

Commit: `git commit -m "feat: add history reanalysis experience"`

---

### Task 10: 全链路评测、旧文档校正与发布门禁

**Files:**
- Create: `backend/tests/fixtures/prompt-eval/multi-scene.json`
- Create: `backend/tests/fixtures/prompt-eval/negative-cases.json`
- Create: `backend/tests/fixtures/prompt-eval/injection.json`
- Create: `scripts/evaluate-prompts.py`
- Create: `backend/tests/e2e/test_prompt_eval_contract.py`
- Modify: `docs/qa/phase-1-acceptance.md`
- Modify: `docs/superpowers/specs/2026-08-05-history-reanalysis-design.md`
- Modify: `docs/superpowers/plans/2026-08-05-six-scene-prompt-system-implementation.md`
- Modify: `scripts/doctor.sh`
- Create: `README.md`

**Interfaces:**
- Offline evaluator verifies Schema, evidence, event separation, user attribution, todo rules and zero secret leakage.
- Task 10 is offline-only: the evaluator does not read Keychain, accept a provider execution mode, or call Kimi, DeepSeek or OpenAI.

- [ ] **Step 1: Add contract fixtures and failing assertions**

Fixtures cover two meetings, one event in multiple scenes, multiple unrelated content events, parenting interactions, other-person todo, media call-to-action, vague title, high-impact growth exception, lightweight inspiration phrase, Prompt injection, overdue todo and multi-file source Batch.

```python
assert report.schema_valid_rate == 1.0
assert report.unknown_evidence_ids == 0
assert report.cross_event_contamination == 0
assert report.false_user_todos == 0
assert report.whisper_calls_during_reanalysis == 0
assert report.overdue_auto_completions == 0
assert report.secret_leaks == 0
```

- [ ] **Step 2: Verify the evaluator contract fails before implementation**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/e2e/test_prompt_eval_contract.py -q`

Expected: FAIL because fixtures/evaluator are absent.

- [ ] **Step 3: Implement the strict offline evaluator**

Validate the complete fixture contract, production scene Schemas, evidence cross-references, event separation, speaker attribution, user todo rules, overdue state, reanalysis trace and secret patterns. Derive scenario coverage from fixture behavior rather than trusting declared labels. Reject `--provider` with an explicit offline-only error; do not import provider adapters or `KeychainRepository`.

- [ ] **Step 4: Reconcile all downstream documentation**

Correct every stale statement that says an overdue todo completes by itself: overdue items stay incomplete, are highlighted red and appear first in the incomplete section. Mark the older demo plan as historical/completed foundation and this plan as the remaining source. Update doctor checks for diarization models, analysis migrations, reanalysis recovery and local session security.

- [ ] **Step 5: Run the complete release gate**

Run:

```bash
bash tests/install-smoke.sh
cd backend
UV_CACHE_DIR=../.uv-cache uv run pytest -q
cd ../prototype
node --test tests/*.test.mjs
npm run build
npm run test:e2e -- --reporter=line
cd ..
./scripts/doctor.sh
```

Expected: all commands exit 0; no console errors; no API Key in database/log/feedback fixtures; current history contains only atomically completed versions.

- [ ] **Step 6: Run the offline Prompt gate and commit**

Run without any saved provider configuration:

`cd backend && UV_CACHE_DIR=../.uv-cache uv run python ../scripts/evaluate-prompts.py --fixture tests/fixtures/prompt-eval/multi-scene.json --fixture tests/fixtures/prompt-eval/negative-cases.json --fixture tests/fixtures/prompt-eval/injection.json`

Expected: the stdout JSON report is redacted, reports all required coverage derived from case behavior, and exits 0 with every release metric at zero. Real-provider evaluation remains explicitly not run and requires a separately authorized future task.

Commit: `git commit -m "feat: add offline prompt evaluation gate"`

---

## Execution Order and Review Gates

1. Task 1 is an immediate correctness patch and can ship independently.
2. Task 2 must complete before event-map Prompt work.
3. Task 3 must complete before any new six-scene or reanalysis result is published.
4. Tasks 4–6 form one analysis-system milestone; do not expose reanalysis before their integration gate passes.
5. Task 7 must complete before enabling Task 8 paid-action endpoints in the frontend.
6. Tasks 8–9 form the historical reanalysis milestone.
7. Task 10 is mandatory before declaring PRD V1.1 complete.

Each task receives a fresh code review after its focused tests pass. Do not batch unrelated task commits, do not mark checkboxes complete from documentation alone, and do not use a real provider Key until the corresponding offline/fake-adapter suite is green.

## PRD Coverage Map

| PRD V1.1 area | Delivery |
|---|---|
| Installation, Keychain, upload, base Whisper, ETA, base routes | Existing tested foundation; regression-covered by Task 10 |
| Overdue todo and page-open provider validation | Task 1 |
| Recording metadata, word timestamps and anonymous speakers | Task 2 |
| Analysis versions, old QA preservation and legacy migration | Task 3 |
| Event map, six scene Prompts, thresholds and evidence | Task 4 |
| One-worker scheduling, new-upload priority and checkpoints | Task 5 |
| Atomic current-version switch, todo tombstones and hidden profile | Task 6 |
| Localhost paid-action security | Task 7 |
| Preview, cost warning, reuse, stop/resume and history rebuild | Task 8 |
| Scene details, global topbar entry and user-visible progress | Task 9 |
| Cross-scene quality, documentation consistency and release acceptance | Task 10 |
