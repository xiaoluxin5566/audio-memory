# Audio Memory 六场景 Prompt 系统 Implementation Plan（历史归档）

> **执行状态：历史计划，不再单独执行。** `2026-08-05-audio-memory-demo-implementation.md` 已作为完成的基础 Demo 留档；本文件的六场景内容已并入 PRD V1.1。实际开发顺序、分析版本、历史重分析与剩余交付项的唯一执行来源是 `docs/superpowers/plans/2026-08-05-prd-v1-1-implementation.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将带时间与说话人的转写先转换为共享事件地图，再由同一厂商按六套固定 Schema 生成可直接发布、可追溯且不串事件的高质量卡片。

**Architecture:** 本地 Whisper 保留词级时间，sherpa-onnx 在 Apple Silicon 上离线生成 speaker turns，后端将两者对齐为结构化转写。当前厂商先生成并持久化事件地图，再独立执行六场景分析；所有结果通过证据完整性校验后一次性发布，失败场景保留检查点并单独重试。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy/Alembic、MLX Whisper、sherpa-onnx、ONNX、SQLite、React 19、Node test、Playwright、pytest。

## Global Constraints

- 第一阶段仅支持 macOS Apple Silicon，音频、模型和分析结果均保存在用户本地。
- Kimi、DeepSeek、OpenAI 对同一批音频使用同一套事件地图、Prompt 分层和 Schema。
- 每个关于音频、用户或事件的事实、推断和评价必须绑定真实 `event_id` 与 `evidence_segment_ids`。
- 待办不生成普通卡片；会议每个独立事件一张；家庭教育、内容推荐、成长建议和闲聊灵感每批最多一张。
- 汇总卡允许多个事件，但不得为统一标题制造不存在的共性或因果。
- 用户身份置信度低于 `0.70` 时，不得归属待办、评价用户或更新画像。
- 单事件问题型成长建议要求行为置信度不低于 `0.80`，且必须有明确负面反馈或可观察失败结果。
- `possible_issues.confidence` 必须不低于 `0.60`。
- 具体外部作品推荐的 `existence_confidence` 必须不低于 `0.90`，否则只输出搜索主题。
- 六场景全部成功且证据校验通过后才原子发布；右侧不展示部分结果。
- 用户只能编辑六个场景自然语言 Prompt；系统规则、事件 Prompt、公共规则和 Schema 固定。
- 使用测试先行；每项生产变更必须先看到对应测试因缺失行为而失败。

---

### Task 1: 录制时间、结构化转写与数据库迁移

**Files:**
- Create: `backend/migrations/versions/0002_structured_transcript.py`
- Modify: `backend/src/audio_memory/models.py`
- Modify: `backend/src/audio_memory/uploads/service.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `prototype/src/api/upload.js`
- Test: `backend/tests/integration/test_database_schema.py`
- Test: `backend/tests/integration/test_upload_jobs.py`
- Test: `prototype/tests/upload-client.test.mjs`

**Interfaces:**
- Produces: `JobFile.recording_started_at: str | None`, `JobFile.recording_time_source: embedded|file_modified|unknown`, and `JobFile.timezone: str | None`.
- Produces: `Transcript.segment_uid: str`, `Transcript.speaker_id: str | None`, and existing `words_json` with absolute word timestamps.
- Produces: multipart fields `recording_started_at` and `timezone` alongside each file.

- [ ] **Step 1: Write failing migration and upload metadata tests**

```python
assert columns["job_files"] >= {
    "recording_started_at", "recording_time_source", "timezone"
}
assert columns["transcripts"] >= {"segment_uid", "speaker_id"}

response = await client.post(
    f"/api/jobs/{job_id}/files",
    data={
        "recording_started_at": "2026-08-05T09:00:00+08:00",
        "recording_time_source": "file_modified",
        "timezone": "Asia/Shanghai",
    },
    files={"file": ("meeting.mp3", audio, "audio/mpeg")},
)
assert response.json()["recording_time_source"] == "file_modified"
```

- [ ] **Step 2: Verify the new tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_database_schema.py tests/integration/test_upload_jobs.py -q`

Expected: FAIL because metadata columns and API fields do not exist.

- [ ] **Step 3: Add migration and model fields**

Migration `0002_structured_transcript.py` must add nullable recording metadata, add non-null `segment_uid` for existing rows using `job_file_id || ':' || segment_index`, create a unique index on `segment_uid`, and add nullable `speaker_id`. Use SQLite-compatible batch/index operations rather than `ALTER TABLE ... ADD CONSTRAINT`. Do not delete or rewrite existing transcript text.

```python
op.add_column("job_files", sa.Column("recording_started_at", sa.String(40)))
op.add_column("job_files", sa.Column("recording_time_source", sa.String(20)))
op.add_column("job_files", sa.Column("timezone", sa.String(80)))
op.add_column("transcripts", sa.Column("segment_uid", sa.String(100)))
op.execute("UPDATE transcripts SET segment_uid = job_file_id || ':' || segment_index")
op.create_index("uq_transcript_segment_uid", "transcripts", ["segment_uid"], unique=True)
op.add_column("transcripts", sa.Column("speaker_id", sa.String(40)))
```

- [ ] **Step 4: Send and persist browser file time metadata**

`uploadFile` must convert `File.lastModified` to ISO 8601, mark it as `file_modified`, and send `Intl.DateTimeFormat().resolvedOptions().timeZone`. During ingestion, inspect embedded media `creation_time`; a valid embedded value overrides the browser value and becomes `recording_time_source=embedded`. Unknown or invalid values stay `null`; never substitute server upload time. Relative expressions may populate `due_at` only when the source is `embedded`; `file_modified` is provided as context but is not reliable enough to create an absolute deadline.

- [ ] **Step 5: Run focused tests and commit**

Run: `cd prototype && node --test tests/upload-client.test.mjs && cd ../backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_database_schema.py tests/integration/test_upload_jobs.py -q`

Expected: PASS.

Commit: `git commit -m "feat: persist structured transcript metadata"`

---

### Task 2: 本地说话人分段与 Whisper 词级对齐

**Files:**
- Create: `backend/src/audio_memory/diarization/__init__.py`
- Create: `backend/src/audio_memory/diarization/engine.py`
- Create: `backend/src/audio_memory/diarization/alignment.py`
- Modify: `backend/src/audio_memory/config.py`
- Modify: `backend/src/audio_memory/transcription/engine.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Modify: `backend/src/audio_memory/jobs/types.py`
- Modify: `backend/src/audio_memory/jobs/service.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `scripts/install.sh`
- Test: `backend/tests/unit/diarization/test_alignment.py`
- Test: `backend/tests/integration/test_diarization_pipeline.py`
- Test: `backend/tests/integration/test_job_recovery.py`
- Test: `tests/install-smoke.sh`

**Interfaces:**
- Produces: `SpeakerTurn(start_ms: int, end_ms: int, speaker_id: str)`.
- Produces: `OfflineDiarizationEngine.diarize(wav_path: Path) -> list[SpeakerTurn]` using bounded windows.
- Produces: `assign_speakers(words, turns) -> list[AlignedTranscriptSegment]`.
- Consumes: sherpa-onnx `model.int8.onnx` segmentation model and Chinese 3D-Speaker embedding model under `AppPaths.models / "diarization"`.

- [ ] **Step 1: Write failing overlap and fallback tests**

```python
def test_word_is_assigned_to_turn_with_largest_overlap():
    words = [Word("你好", 900, 1300)]
    turns = [SpeakerTurn(0, 1000, "speaker_00"), SpeakerTurn(1000, 2000, "speaker_01")]
    assert assign_speakers(words, turns)[0].speaker_id == "speaker_01"

def test_missing_diarization_keeps_text_with_unknown_speaker():
    assert assign_speakers([Word("继续", 0, 500)], [])[0].speaker_id is None
```

- [ ] **Step 2: Verify tests fail before production code**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/diarization/test_alignment.py -q`

Expected: FAIL because diarization modules do not exist.

- [ ] **Step 3: Implement sherpa-onnx engine behind a protocol**

Add optional dependency `sherpa-onnx>=1.10.28,<2`. Configure `OfflineSpeakerDiarizationConfig` with pyannote segmentation `model.int8.onnx`, Chinese 3D-Speaker embedding, `num_clusters=-1`, `threshold=0.5`, `min_duration_on=0.3`, and `min_duration_off=0.5`. Run it in the existing process pool, not the FastAPI event loop.

Long audio must not be loaded into one unbounded float array. Decode and diarize 30-minute windows with a 30-second overlap. Reconcile adjacent-window labels only when overlap speech for a pair exceeds 2 seconds; otherwise allocate a new global speaker label rather than guessing identity. Unit tests must cover stable label mapping, silent overlap and a five-hour synthetic duration without allocating five hours of PCM at once.

- [ ] **Step 4: Enable Whisper word timestamps and align speakers**

Call MLX Whisper with `word_timestamps=True`. Convert chunk-relative word timestamps to file-relative milliseconds, assign each word to the turn with maximum overlap, and group adjacent words by speaker into stable transcript segments. Add `JobStage.DIARIZING` between transcription and remote analysis, including progress text “正在区分说话人…” and interrupted-job recovery. Preserve text if diarization fails; set `speaker_id=null`, record a normalized diagnostic error and continue the batch.

- [ ] **Step 5: Download local models during installation**

Use the official sherpa-onnx release URLs:

```text
https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
```

Extract only the INT8 segmentation model, calculate SHA-256 after download, and save a local manifest next to the Whisper manifest. Installer retries must be idempotent and must not request Hugging Face or pyannote tokens.

- [ ] **Step 6: Run alignment, integration and installer tests**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/diarization tests/integration/test_diarization_pipeline.py -q && cd .. && bash tests/install-smoke.sh`

Expected: PASS, including fail-open `speaker_id=null` behavior.

Commit: `git commit -m "feat: add local speaker diarization"`

---

### Task 3: 事件地图与六场景严格 Schema

**Files:**
- Replace: `backend/src/audio_memory/prompts/schemas.py`
- Create: `backend/src/audio_memory/prompts/event_schema.py`
- Create: `backend/src/audio_memory/prompts/evidence.py`
- Test: `backend/tests/unit/prompts/test_event_schema.py`
- Test: `backend/tests/unit/prompts/test_scene_schemas.py`
- Test: `backend/tests/unit/prompts/test_evidence_integrity.py`

**Interfaces:**
- Produces: `EventMap`, `Event`, `UserSpeaker`, `StructuredTranscriptSegment`.
- Produces: `TodoSceneResult`, `MeetingSceneResult`, `ParentingSceneResult`, `ContentSceneResult`, `GrowthSceneResult`, `InspirationSceneResult` as a discriminated `SceneResult` union.
- Produces: `validate_evidence_integrity(result, event_map, segment_ids) -> None`.

- [ ] **Step 1: Write failing validation tests for frozen thresholds**

```python
def test_parenting_issue_below_point_six_is_rejected():
    with pytest.raises(ValidationError):
        ParentingIssue(
            finding_id="finding_parenting_event_003_01",
            event_id="event_003",
            content="可能存在问题",
            reasoning="依据",
            evidence_segment_ids=["seg_3"],
            confidence=0.59,
        )

def test_interest_modes_enforce_event_cardinality():
    with pytest.raises(ValidationError):
        InterestSignal(
            dimension="product_interest", value="端侧AI",
            evidence_mode="multi_event_pattern",
            supporting_event_ids=["event_1"], confidence=0.8,
        )
```

- [ ] **Step 2: Verify schema tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_event_schema.py tests/unit/prompts/test_scene_schemas.py -q`

Expected: FAIL because the specialized models do not exist.

- [ ] **Step 3: Implement strict Pydantic models**

Use `extra="forbid"`, literal enums, min/max lengths, `Field(ge=..., le=...)`, and model validators. Meeting cards allow one event; other visible scenes enforce at most one card; todo enforces `cards=[]`; content uses nullable `source_title`, non-empty `display_title`, `title_source=explicit|unknown`, and non-rendered `inferred_title_hint`.

- [ ] **Step 4: Implement cross-reference validation**

Reject unknown event IDs, unknown segment IDs, evidence segments outside the referenced event, duplicate finding/case IDs, cross-card basis references, nonexistent basis IDs, user attribution below `0.70`, and problem-type single-event growth results below `0.80` or without explicit negative evidence fields.

- [ ] **Step 5: Run schema suite and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts -q`

Expected: PASS.

Commit: `git commit -m "feat: define event and scene output contracts"`

---

### Task 4: Prompt 分层、默认文件与结构化输入

**Files:**
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Create: `backend/src/audio_memory/prompts/system.md`
- Create: `backend/src/audio_memory/prompts/common-scene.md`
- Create: `backend/src/audio_memory/prompts/event-map.md`
- Replace: `backend/src/audio_memory/prompts/defaults/todo.md`
- Replace: `backend/src/audio_memory/prompts/defaults/meeting.md`
- Replace: `backend/src/audio_memory/prompts/defaults/parenting.md`
- Replace: `backend/src/audio_memory/prompts/defaults/content.md`
- Replace: `backend/src/audio_memory/prompts/defaults/growth.md`
- Replace: `backend/src/audio_memory/prompts/defaults/inspiration.md`
- Test: `backend/tests/unit/prompts/test_composer.py`
- Test: `backend/tests/integration/test_prompt_api.py`

**Interfaces:**
- Produces: `PromptComposer.compose_event_map(...) -> ModelRequest`.
- Produces: `PromptComposer.compose_scene(scene_id, *, transcript, event_map, profile, prompt) -> ModelRequest`.
- Consumes: frozen text from `docs/superpowers/specs/2026-08-05-six-scene-prompt-system-design.md`.

- [ ] **Step 1: Write failing prompt-layer tests**

```python
request = composer.compose_scene(
    "meeting", transcript=segments, event_map=event_map,
    profile=[], prompt=PromptDocument("meeting", 3, "关注决策"),
)
assert "最高优先级铁律" in request.system_rules
assert "先提取 event_id" in request.common_rules
assert request.scene_prompt == "关注决策"
assert "seg_001" in request.user_data
assert "event_001" in request.user_data
```

- [ ] **Step 2: Verify tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_composer.py -q`

Expected: FAIL because event composition and fixed prompt resources are absent.

- [ ] **Step 3: Implement four-layer composition**

Load fixed files through `importlib.resources`, inject full JSON Schema, wrap transcript/event/profile in explicit untrusted-data tags, and keep user-edited natural language isolated as `scene_prompt`. Fixed layers always precede editable text and state that conflicts are ignored.

- [ ] **Step 4: Replace packaged defaults and migrate untouched legacy defaults**

New installs get frozen defaults. Record SHA-256 values for every known legacy packaged default. On startup, if an existing `current.md` hash matches a legacy packaged hash, archive it as a historical version and replace it with the new frozen default; if the hash does not match, treat it as a user edit and preserve it byte-for-byte. Add `packaged_default_version` and `current_source=packaged|user` to metadata. Tests must cover new install, untouched legacy upgrade, user-edited legacy preservation and repeated idempotent startup.

- [ ] **Step 5: Run Prompt store/API tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_composer.py tests/integration/test_prompt_api.py -q`

Expected: PASS.

Commit: `git commit -m "feat: install layered six-scene prompts"`

---

### Task 5: 事件地图模型调用与分析检查点

**Files:**
- Create: `backend/src/audio_memory/analysis/events.py`
- Modify: `backend/src/audio_memory/analysis/provider.py`
- Modify: `backend/src/audio_memory/analysis/orchestrator.py`
- Modify: `backend/src/audio_memory/models.py`
- Create: `backend/migrations/versions/0003_event_map_checkpoint.py`
- Test: `backend/tests/integration/test_event_map_pipeline.py`
- Test: `backend/tests/integration/test_analysis_pipeline.py`

**Interfaces:**
- Produces: `RemoteEventMapper.map(request, provider_snapshot) -> EventMap`.
- Persists: `AnalysisJob.event_map_json`, `event_map_provider_id`, and per-scene `staged_results_json`.
- Consumes: structured transcript with recording metadata and speaker IDs.

- [ ] **Step 1: Write failing event-first pipeline tests**

```python
outcome = await orchestrator.run(job_id, provider_snapshot)
assert analyzer.calls[0] == "event_map"
assert analyzer.calls[1:] == list(PROMPT_SCENES)
assert json.loads(job.event_map_json)["events"][0]["event_id"] == "event_001"
```

Add recovery assertions: same-provider retry reuses the valid event map and successful scene checkpoints; provider switch clears both and regenerates all model-derived state while keeping transcript and diarization.

- [ ] **Step 2: Verify integration tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_event_map_pipeline.py tests/integration/test_analysis_pipeline.py -q`

Expected: FAIL because no event-map phase or columns exist.

- [ ] **Step 3: Implement event mapper with one repair attempt**

Use the same `ProviderAnalysisClient` and model snapshot. Parse `EventMap`; on validation failure send the invalid JSON, validation errors and EventMap Schema for exactly one repair. Never send API keys, local file paths or unrelated batches.

- [ ] **Step 4: Update orchestrator state machine**

Order: load structured transcript → generate/reuse event map → run/reuse six scenes → extract profile from event map and validated scene signals → validate all cross-references → publish. Persist after each successful remote call. Mark the job failed without deleting checkpoints.

- [ ] **Step 5: Run recovery tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_event_map_pipeline.py tests/integration/test_analysis_pipeline.py -q`

Expected: PASS.

Commit: `git commit -m "feat: add event-map analysis phase"`

---

### Task 6: 六场景分析、画像门槛与原子发布

**Files:**
- Modify: `backend/src/audio_memory/analysis/parser.py`
- Modify: `backend/src/audio_memory/analysis/provider.py`
- Modify: `backend/src/audio_memory/analysis/profile.py`
- Modify: `backend/src/audio_memory/analysis/publisher.py`
- Modify: `backend/src/audio_memory/content/service.py`
- Test: `backend/tests/integration/test_atomic_batch_commit.py`
- Test: `backend/tests/integration/test_content_api.py`
- Test: `backend/tests/unit/analysis/test_profile_signals.py`

**Interfaces:**
- Consumes: discriminated `SceneResult` union and validated `EventMap`.
- Produces: zero or more meeting cards, at most one card for each aggregate scene, globally deduplicated todos, and profile deltas from `explicit_single_event|multi_event_pattern` signals.

- [ ] **Step 1: Write failing publication and profile tests**

```python
assert published_scenes == ["meeting", "meeting", "parenting", "content", "growth", "inspiration"]
assert no_partial_cards_exist_after_scene_failure(database, job_id)
assert build_profile_delta(single_lightweight_reaction) == []
assert build_profile_delta(explicit_long_term_interest)[0].confidence >= 0.7
```

- [ ] **Step 2: Verify tests fail**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_atomic_batch_commit.py tests/unit/analysis/test_profile_signals.py -q`

Expected: FAIL because publisher assumes one card per scene and profile ignores evidence modes.

- [ ] **Step 3: Flatten and publish cards in deterministic order**

Order all meeting cards by event start time, followed by parenting, content, growth and inspiration. Compute labels, time strings and counts from structured payloads; strip `inferred_title_hint` before card persistence and feedback export. Do not persist partial cards if any scene or reference validator fails.

- [ ] **Step 4: Implement conservative todo deduplication**

Merge only when normalized action/object, owner and compatible due time match and source contexts identify repeated confirmation. Otherwise preserve separate todos. Expired todo completion remains backend-controlled.

- [ ] **Step 5: Implement profile signal gates**

Accept `explicit_single_event` only with explicit long-term interest/background or deep active linkage to the user's stated goal. Accept `multi_event_pattern` only with at least two distinct event IDs. Reject user identity below `0.70` and all lightweight reactions.

- [ ] **Step 6: Run atomic publication tests and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_atomic_batch_commit.py tests/integration/test_content_api.py tests/unit/analysis/test_profile_signals.py -q`

Expected: PASS.

Commit: `git commit -m "feat: publish evidence-backed scene cards"`

---

### Task 7: 前端适配新版卡片字段

**Files:**
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css`
- Modify: `prototype/tests/api-state.test.mjs`
- Modify: `prototype/tests/detail-layout.test.mjs`
- Modify: `prototype/tests/e2e/upload-analysis.spec.js`
- Modify: `prototype/tests/e2e/content-actions.spec.js`

**Interfaces:**
- Consumes: card payloads with common `card.title`, `card.summary`, `event_ids` and scene-specific detail bodies.
- Produces: deterministic detail renderers for meeting, parenting, content, growth and inspiration; no raw diagnostic fields are rendered.

- [ ] **Step 1: Add failing normalizer and rendering tests**

```javascript
assert.equal(contentCard.title, '本次分别关注了端侧 AI 与智能硬件')
assert.equal(contentCard.details.consumedItems.length, 2)
assert.equal(contentCard.details.consumedItems[0].displayTitle, '一段关于端侧 AI 的视频')
assert.equal(JSON.stringify(contentCard).includes('inferred_title_hint'), false)
```

Browser assertions must verify two unrelated consumed events are shown as two detail sections, two meetings render as two cards, and aggregate card feedback/QA remain connected.

- [ ] **Step 2: Verify frontend tests fail**

Run: `cd prototype && node --test tests/api-state.test.mjs tests/detail-layout.test.mjs && npx playwright test tests/e2e/upload-analysis.spec.js tests/e2e/content-actions.spec.js --reporter=line`

Expected: FAIL because current normalizer expects generic `detail_sections`.

- [ ] **Step 3: Implement scene-specific presentation adapters**

Keep `App.jsx` rendering data-driven: map each scene payload to common `DetailBlock` objects in `api/state.js`; do not branch on arbitrary model keys in JSX. Never show generation reasons, confidence, inferred title hints, finding IDs or evidence IDs in phase one.

- [ ] **Step 4: Run frontend quality gate and commit**

Run: `cd prototype && node --test tests/*.test.mjs && npm run build && npm run test:e2e -- --reporter=line`

Expected: all frontend unit and browser tests PASS; build exits 0.

Commit: `git commit -m "feat: render structured scene results"`

---

### Task 8: 评测集与安装验收（历史草案）

> **历史说明：** 其中离线评测和安装门禁由 PRD V1.1 Task 10 收口。任何真实厂商调用均需当次用户明确授权；未授权时仅运行离线样例，不读取保存的 Key。

**Files:**
- Create: `backend/tests/fixtures/prompt-eval/multi-scene.json`
- Create: `backend/tests/fixtures/prompt-eval/negative-cases.json`
- Create: `backend/tests/fixtures/prompt-eval/injection.json`
- Create: `scripts/evaluate-prompts.py`
- Create: `backend/tests/e2e/test_prompt_eval_contract.py`
- Modify: `scripts/doctor.sh`
- Modify: `README.md`
- Modify: `prototype/README.md`

**Interfaces:**
- Produces: redacted offline evaluation report on stdout.
- Consumes: repository fixtures only; does not read `KeychainRepository` or call a provider.

- [ ] **Step 1: Add deterministic contract fixtures and failing assertions**

Fixtures must cover: one event in multiple scenes; two meetings; unrelated content events in one card; unrelated parenting interactions; other-person todo; media call-to-action; vague title; high-impact single growth event; lightweight inspiration keyword; explicit single-event interest; multi-event interest; unknown recording date; and Prompt injection text.

```python
assert report.schema_valid_rate == 1.0
assert report.unknown_evidence_ids == 0
assert report.cross_event_contamination == 0
assert report.false_user_todos == 0
```

- [ ] **Step 2: Verify the contract test fails before harness implementation**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/e2e/test_prompt_eval_contract.py -q`

Expected: FAIL because the evaluator and fixtures do not exist.

- [ ] **Step 3: Implement the offline evaluation mode**

Offline mode validates the complete stored-example contract, production Schemas and all cross-reference rules. It rejects provider execution flags and never reads Keychain, prints keys or sends fixture transcripts outside the process.

- [ ] **Step 4: Extend doctor and documentation**

Doctor verifies diarization dependency, both ONNX files, model manifests, migrations and fixed Prompt resources. Update stale prototype documentation so it no longer claims API validation or analysis is simulated.

- [ ] **Step 5: Run the complete quality gate**

Run:

```bash
cd backend
UV_CACHE_DIR=../.uv-cache uv run pytest -q
cd ../prototype
node --test tests/*.test.mjs
npm run build
npm run test:e2e -- --reporter=line
cd ..
bash tests/install-smoke.sh
./scripts/doctor.sh
```

Expected: all backend, frontend, browser and installer tests PASS; doctor reports Whisper and diarization models available.

- [ ] **Step 6: Run the offline evaluation and commit**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run python ../scripts/evaluate-prompts.py --fixture tests/fixtures/prompt-eval/multi-scene.json --fixture tests/fixtures/prompt-eval/negative-cases.json --fixture tests/fixtures/prompt-eval/injection.json`

Expected: stdout reports Schema valid rate 100%, required coverage derived from case behavior, and zero unknown evidence IDs, cross-event contamination, false user todos, reanalysis Whisper calls, overdue auto-completion and secret leakage. Real-provider comparison is not run and requires a separately authorized future task.

Commit: superseded by PRD V1.1 Task 10.
