# Audio Memory 第一阶段真实 Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有全屏交互原型升级为可在 macOS Apple Silicon 本地安装、真实运行 Whisper、调用 Kimi/DeepSeek/OpenAI 并长期保存全部历史的第一阶段体验版 Demo。

**Architecture:** 保留 `prototype/` 中的 React/Vite UI，把 localStorage 和模拟引擎替换为 localhost FastAPI 服务。后端使用 SQLite 管理状态与结构化数据，使用受控本地目录保存音频、转写、Prompt 版本与反馈，使用 macOS Keychain 保存 API Key，使用 MLX Whisper 本地转写；六个场景分别分析，整批结果通过 SQLite 事务和文件提交清单一次性发布。

**Tech Stack:** React 19、Vite 6、JavaScript ES Modules、Node.js 22；Python 3.12、FastAPI、Uvicorn、Pydantic 2、SQLAlchemy 2、Alembic、httpx、PyObjC Security、mlx-whisper、ffmpeg、pytest、Playwright。

## Global Constraints

- 第一阶段只支持 macOS Apple Silicon，不支持 Intel Mac、Windows 或移动端。
- 用户通过终端安装和启动；应用没有登录、注册、身份校验或角色系统。
- 原始音频、转写、卡片、问答、画像和反馈保存在本机；模型厂商只接收转写文本。
- 支持的音频格式只有 MP3 和 AAC；以扩展名、MIME 探测和 ffprobe 解码三层校验。
- Kimi、DeepSeek、OpenAI 都复用同一份本地 Whisper 转写。
- API Key 规格以 `docs/superpowers/specs/2026-08-05-api-key-configuration-design.md` 的冻结版本为唯一基线。
- 其他产品行为以 `Audio Memory 第一阶段产品PRD-v0.9.md` 为基线；UI 以 `prototype/` 和已确认截图为视觉基线。
- 当前批次未全部成功前，右侧信息流和音频历史不出现任何半成品。
- 失败、中断后放弃或取消的批次不进入正式历史；可恢复的转写检查点可以保留到用户选择继续或放弃。
- 六个固定场景是 `todo`、`meeting`、`parenting`、`content`、`growth`、`inspiration`；第一阶段不允许新增、删除、排序或停用。
- 待办由用户手动完成；过期只标红，不自动完成。
- 个人画像从首批成功音频开始建立，但第一阶段不向用户展示。
- 清除历史保留 Keychain、Prompt 配置和意见反馈文件。
- 所有用户界面文案使用简体中文。

## Source Precedence

1. 冻结的 API Key 设计规格。
2. 本计划的接口、目录和类型定义。
3. 产品 PRD 的非 API Key 需求。
4. 全屏原型规格和 `prototype/` 的视觉与交互。
5. `designs/` 只作为字段归档，不作为页面外壳或布局来源。

## Target File Map

```text
backend/
├── pyproject.toml
├── alembic.ini
├── migrations/
│   └── versions/0001_initial.py
├── src/audio_memory/
│   ├── main.py                 # FastAPI 生命周期、静态前端与路由装配
│   ├── config.py               # 本地目录、模型默认值、超时和环境配置
│   ├── domain.py               # 跨模块 Pydantic 类型与枚举
│   ├── db.py                   # SQLAlchemy engine、session、事务辅助
│   ├── models.py               # SQLite ORM 表
│   ├── instance_lock.py        # 本地 APFS 目录上的内核排他锁
│   ├── providers/              # Keychain、协调器、三厂商适配器
│   ├── uploads/                # 文件接收、格式校验、暂存和清理
│   ├── transcription/          # ffprobe、分段、MLX Whisper、检查点
│   ├── prompts/                # 默认 Prompt、版本文件、组合器和 Schema
│   ├── analysis/               # 六场景编排、画像合并、原子提交
│   ├── content/                # 信息流、历史、待办、问答、反馈、清除
│   └── api/                    # providers/jobs/feed/prompts/content 路由
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
prototype/
├── src/
│   ├── api/                    # HTTP、XHR 上传、SSE 客户端
│   ├── components/             # 从 App.jsx 拆出的稳定 UI 单元
│   ├── pages/                  # 信息流、历史、Prompt 页面
│   ├── App.jsx
│   └── styles.css
├── tests/
└── package.json
scripts/
├── install.sh
├── start.sh
└── doctor.sh
```

---

### Task 1: 本地运行骨架、目录契约和健康检查

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/src/audio_memory/__init__.py`
- Create: `backend/src/audio_memory/config.py`
- Create: `backend/src/audio_memory/domain.py`
- Create: `backend/src/audio_memory/instance_lock.py`
- Create: `backend/src/audio_memory/main.py`
- Create: `backend/tests/unit/test_config.py`
- Create: `backend/tests/integration/test_health.py`

**Interfaces:**
- Produces: `AppPaths.from_home(home: Path) -> AppPaths`.
- Produces: `InstanceLock.acquire() -> None`, `InstanceLock.release() -> None`.
- Produces: `GET /api/health -> {status, version, platform, architecture}`.
- Application data root: `~/Library/Application Support/AudioMemory/`.

- [ ] **Step 1: Write failing path and platform tests**

```python
def test_app_paths_are_all_under_local_support(tmp_path):
    paths = AppPaths.from_home(tmp_path)
    root = tmp_path / "Library/Application Support/AudioMemory"
    assert paths.root == root
    assert paths.database == root / "audio-memory.sqlite3"
    assert paths.lock == root / "runtime/audio-memory.lock"
    assert paths.feedback == root / "意见反馈"

def test_platform_guard_rejects_non_arm64(monkeypatch):
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    with pytest.raises(UnsupportedPlatformError):
        assert_supported_platform()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && uv run pytest tests/unit/test_config.py -v`

Expected: FAIL because `AppPaths` and `assert_supported_platform` do not exist.

- [ ] **Step 3: Implement paths, platform guard and kernel lock**

Use `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `paths.lock`, keep `fd` open for the process lifetime, and store diagnostic PID/start time only after the kernel lock succeeds. Never place the lock under the repository, bundle path, SMB, NFS, or a user-selected directory.

`backend/pyproject.toml` must require Python `>=3.12,<3.13` and declare FastAPI, Uvicorn, Pydantic 2, SQLAlchemy 2, aiosqlite, Alembic, httpx, `pyobjc-framework-Security`, `mlx-whisper`, pytest, pytest-asyncio and respx. Commit `uv.lock` after dependency resolution so installation is reproducible.

- [ ] **Step 4: Add FastAPI lifespan and health endpoint**

The lifespan must acquire the instance lock before opening SQLite, create required directories with mode `0700`, and release resources in reverse order. `GET /api/health` returns HTTP 200 only after startup is complete.

- [ ] **Step 5: Run focused tests and commit**

Run: `cd backend && uv run pytest tests/unit/test_config.py tests/integration/test_health.py -v`

Expected: PASS.

Commit: `git commit -m "feat: add local backend runtime foundation"`

---

### Task 2: SQLite schema、迁移和正式数据边界

**Files:**
- Create: `backend/src/audio_memory/db.py`
- Create: `backend/src/audio_memory/models.py`
- Create: `backend/migrations/env.py`
- Create: `backend/migrations/versions/0001_initial.py`
- Create: `backend/tests/integration/test_database_schema.py`
- Create: `backend/tests/integration/test_atomic_batch_commit.py`

**Interfaces:**
- Produces: `session_scope() -> AsyncIterator[AsyncSession]`.
- Produces tables: `provider_metadata`, `analysis_jobs`, `job_files`, `transcripts`, `batches`, `cards`, `todos`, `qa_messages`, `profile_facts`, `prompt_versions`, `temp_file_manifest`, `feedback_index`.
- `analysis_jobs.stage`: `uploading | transcribing | analyzing | ready_to_commit | completed | failed | interrupted | cancelled`.

- [ ] **Step 1: Write migration and invariant tests**

```python
async def test_only_one_provider_can_be_active(session):
    session.add_all([
        ProviderMetadata(provider_id="kimi", active=True),
        ProviderMetadata(provider_id="openai", active=True),
    ])
    with pytest.raises(IntegrityError):
        await session.commit()

async def test_uncommitted_batch_is_absent_from_feed(session, repository):
    job = await repository.create_job(stage="analyzing")
    await repository.stage_card(job.id, scene_id="meeting", payload={"title": "draft"})
    assert await repository.list_feed_batches() == []
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && uv run pytest tests/integration/test_database_schema.py tests/integration/test_atomic_batch_commit.py -v`

Expected: FAIL because models and migrations do not exist.

- [ ] **Step 3: Implement schema and indexes**

Use UUID text primary keys, UTC ISO timestamps, foreign keys with explicit delete behavior, `UNIQUE(provider_id)`, and a SQLite partial unique index ensuring at most one `active = 1`. Store JSON payloads as text validated through Pydantic before write; never store API Keys.

- [ ] **Step 4: Implement atomic publication transaction**

`publish_batch(job_id)` must insert the batch, audio history, cards, todos, QA roots and profile deltas, mark the job completed, and expose the batch to feed queries within one SQLite transaction. Any failure rolls back every formal row.

- [ ] **Step 5: Run migration twice, test rollback and commit**

Run: `cd backend && uv run alembic upgrade head && uv run alembic upgrade head && uv run pytest tests/integration/test_database_schema.py tests/integration/test_atomic_batch_commit.py -v`

Expected: both migrations exit 0 and all tests PASS.

Commit: `git commit -m "feat: add persistent Audio Memory data model"`

---

### Task 3: Keychain、厂商状态协调器和真实 Key 校验

**Files:**
- Create: `backend/src/audio_memory/providers/types.py`
- Create: `backend/src/audio_memory/providers/keychain.py`
- Create: `backend/src/audio_memory/providers/coordinator.py`
- Create: `backend/src/audio_memory/providers/validation.py`
- Create: `backend/src/audio_memory/providers/adapters/base.py`
- Create: `backend/src/audio_memory/providers/adapters/kimi.py`
- Create: `backend/src/audio_memory/providers/adapters/deepseek.py`
- Create: `backend/src/audio_memory/providers/adapters/openai.py`
- Create: `backend/src/audio_memory/api/providers.py`
- Create: `backend/tests/unit/providers/`
- Create: `backend/tests/integration/test_provider_api.py`

**Interfaces:**
- `KeychainRepository.read(provider_id) -> KeychainReadResult`.
- `KeychainRepository.replace(provider_id, candidate: SecretBytes) -> None`.
- `ProviderStateCoordinator.list_states() -> list[ProviderState]`.
- `validate_saved(provider_id, deadline) -> ValidationResult`.
- `validate_candidate(provider_id, session_id, candidate, deadline) -> ValidationResult`.
- Routes exactly follow the frozen API Key specification.

- [ ] **Step 1: Write Keychain state-code and replacement tests**

```python
def test_auth_failure_is_not_unconfigured(fake_security):
    fake_security.copy_status = errSecAuthFailed
    result = repository.read("kimi")
    assert result.access == "unavailable"
    assert result.configured == "unknown"

def test_duplicate_add_retries_update_once(fake_security):
    fake_security.update_statuses = [errSecItemNotFound, errSecSuccess]
    fake_security.add_status = errSecDuplicateItem
    repository.replace("kimi", SecretBytes(b"new-key"))
    assert fake_security.update_calls == 2
```

- [ ] **Step 2: Write coordinator race tests**

Cover shared in-flight validation, stale `credential_generation`, separate `candidate_validation_id + session_id`, modal-close cancellation, cooldown monotonic timing, idempotent activate, active/start-analysis serialization, and 15-second provider/20-second total deadlines.

- [ ] **Step 3: Implement Security Framework adapter**

Use PyObjC Security functions with Service `Audio Memory`, accounts `provider:kimi|deepseek|openai`, Data Protection Keychain, `WhenUnlockedThisDeviceOnly`, and no synchronization. Redact candidate bytes from exceptions, repr, structured logs and HTTP traces.

- [ ] **Step 4: Implement provider adapters and validation protocol**

Defaults in `config.py`:

```python
PROVIDERS = {
    "kimi": ProviderConfig("https://api.moonshot.cn/v1", "kimi-k2.5", "chat_completions"),
    "deepseek": ProviderConfig("https://api.deepseek.com", "deepseek-v4-flash", "chat_completions"),
    "openai": ProviderConfig("https://api.openai.com/v1", "gpt-5-mini", "responses"),
}
```

Validation uses `Reply exactly: OK`, temperature 0 where supported, output cap 4 tokens, no tools/streaming, and succeeds only when the parsed output trimmed and case-folded equals `ok`.

- [ ] **Step 5: Run the frozen-spec test matrix and commit**

Run: `cd backend && uv run pytest tests/unit/providers tests/integration/test_provider_api.py -v`

Expected: all state, race, Keychain and API contract tests PASS without printing any secret.

Commit: `git commit -m "feat: implement secure provider configuration"`

---

### Task 4: 上传队列、格式验证、暂存文件和任务事件

**Files:**
- Create: `backend/src/audio_memory/uploads/service.py`
- Create: `backend/src/audio_memory/uploads/probe.py`
- Create: `backend/src/audio_memory/uploads/cleanup.py`
- Create: `backend/src/audio_memory/api/jobs.py`
- Create: `backend/src/audio_memory/api/events.py`
- Create: `backend/tests/integration/test_upload_jobs.py`
- Create: `prototype/src/api/client.js`
- Create: `prototype/src/api/upload.js`
- Create: `prototype/src/api/events.js`

**Interfaces:**
- `POST /api/jobs -> JobView` creates one provider-neutral batch.
- `POST /api/jobs/{job_id}/files` accepts one multipart file and emits progress.
- `DELETE /api/jobs/{job_id}/files/{file_id}` removes a pre-analysis file.
- `POST /api/jobs/{job_id}/start` locks the file list and provider snapshot.
- `GET /api/jobs/{job_id}/events` is an SSE stream with monotonic event IDs.

- [ ] **Step 1: Write file validation and pause tests**

```python
@pytest.mark.parametrize("name,allowed", [
    ("a.mp3", True), ("b.aac", True), ("c.wav", False), ("fake.mp3", False)
])
async def test_upload_format_contract(client, fixtures, name, allowed):
    response = await fixtures.upload(client, name)
    assert (response.status_code == 201) is allowed
```

- [ ] **Step 2: Implement streamed local upload**

Write chunks to `staging/{job_uuid}/`, update SHA-256 and byte progress, reject duplicate `(sha256, size)` within the batch, fsync before marking uploaded, then run ffprobe. Unsupported or undecodable files pause subsequent client submissions until removed.

- [ ] **Step 3: Implement manifest-backed cleanup**

Register every temporary path in `temp_file_manifest` before creation. Cleanup resolves the real path and rejects deletion unless it is below `paths.staging`; remove physical content before deleting the manifest row. Startup runs the same cleanup for abandoned upload-stage jobs.

- [ ] **Step 4: Implement XHR and SSE clients**

Use XHR for per-file upload percentage and EventSource for server job state. Reconnect SSE with `Last-Event-ID`; after reconnect always fetch `GET /api/jobs/{id}` as the state authority.

The browser sends accepted files one at a time in the user's selection order. It does not begin the next file while the current upload is active or while the batch is paused by an unsupported file.

- [ ] **Step 5: Run API and browser-client unit tests and commit**

Run: `cd backend && uv run pytest tests/integration/test_upload_jobs.py -v && cd ../prototype && node --test tests/*.test.mjs`

Expected: PASS, including invalid format pause, duplicate detection, cleanup safety and reconnect behavior.

Commit: `git commit -m "feat: add durable local upload jobs"`

---

### Task 5: MLX Whisper 转写、分段检查点和恢复

**Files:**
- Create: `backend/src/audio_memory/transcription/engine.py`
- Create: `backend/src/audio_memory/transcription/segments.py`
- Create: `backend/src/audio_memory/transcription/checkpoints.py`
- Create: `backend/tests/unit/transcription/test_segments.py`
- Create: `backend/tests/integration/test_transcription_recovery.py`
- Create: `backend/tests/fixtures/audio/short-zh.mp3`
- Create: `backend/tests/fixtures/audio/short-zh.aac`

**Interfaces:**
- `WhisperEngine.transcribe_file(file, resume_from) -> AsyncIterator[TranscriptSegment]`.
- `TranscriptSegment(file_id, index, start_ms, end_ms, text, words)`.
- Default checkpoint: `mlx-community/whisper-large-v3-turbo` configured in `config.py`.

- [ ] **Step 1: Write deterministic segment/checkpoint tests**

Test stable segment indexes, UTC checkpoint writes, skipping committed segments on retry, ordered concatenation across files, and progress computed from processed duration rather than wall-clock estimates.

- [ ] **Step 2: Implement ffmpeg normalization and MLX worker isolation**

Normalize each source to temporary 16 kHz mono PCM under the job staging directory. Run MLX Whisper in a dedicated worker process so cancellation or native failure cannot corrupt the FastAPI event loop. Never send audio bytes to any provider adapter.

- [ ] **Step 3: Persist each segment before progress emission**

For every completed segment: validate timestamps, insert transcript segment and checkpoint transactionally, then emit SSE progress. On restart, set active transcription/analysis jobs to `interrupted`; do not resume paid model work automatically.

- [ ] **Step 4: Implement retry/cancel/resume rules**

Retry only the failed file/segment. Cancel terminates the worker, removes temporary normalized audio and uncommitted transcripts, and returns the upload panel to empty. Resume requires explicit user action and reuses all valid checkpoints.

- [ ] **Step 5: Run real local transcription smoke tests and commit**

Run: `cd backend && uv run pytest tests/unit/transcription tests/integration/test_transcription_recovery.py -v -m 'not slow' && uv run pytest tests/integration/test_transcription_recovery.py -v -m slow`

Expected: fixtures transcribe, interruption resumes without duplicating segments, and no network call occurs.

Commit: `git commit -m "feat: add recoverable local Whisper transcription"`

---

### Task 6: Prompt 文件、版本管理、Schema 和组合器

**Files:**
- Create: `backend/src/audio_memory/prompts/defaults/todo.md`
- Create: `backend/src/audio_memory/prompts/defaults/meeting.md`
- Create: `backend/src/audio_memory/prompts/defaults/parenting.md`
- Create: `backend/src/audio_memory/prompts/defaults/content.md`
- Create: `backend/src/audio_memory/prompts/defaults/growth.md`
- Create: `backend/src/audio_memory/prompts/defaults/inspiration.md`
- Create: `backend/src/audio_memory/prompts/store.py`
- Create: `backend/src/audio_memory/prompts/schemas.py`
- Create: `backend/src/audio_memory/prompts/composer.py`
- Create: `backend/src/audio_memory/api/prompts.py`
- Create: `backend/tests/unit/prompts/test_store.py`
- Create: `backend/tests/unit/prompts/test_composer.py`

**Interfaces:**
- `PromptStore.get(scene_id) -> PromptDocument`.
- `PromptStore.save(scene_id, expected_version, content) -> PromptDocument`.
- `PromptComposer.compose(scene_id, transcript, profile, prompt_version) -> ModelRequest`.
- `SceneResult` uses stable `scene_id`, `should_generate`, `card`, `detail_sections`, `todos`, `evidence_refs`, `confidence`.

- [ ] **Step 1: Write six-scene initialization and atomic-save tests**

Assert exactly six current prompts, non-empty defaults, version 1 initialization, optimistic version conflict HTTP 409, old-version snapshot creation, temp-file fsync, and atomic `os.replace`.

- [ ] **Step 2: Implement Prompt store under Application Support**

Use `prompts/{scene_id}/current.md`, `metadata.json`, and `versions/{version}-{timestamp}.md`. Reject unknown scene IDs and blank content. A failed write leaves the previous current file readable.

- [ ] **Step 3: Implement stable schemas**

Define strict Pydantic models for card shell, dynamic detail sections, todos, evidence and confidence. Unknown detail section keys remain renderable through `text | list | grouped_items`; model output cannot inject Markdown/HTML as UI structure.

- [ ] **Step 4: Implement the three-layer composer**

Concatenate immutable system rules, the selected natural-language scene Prompt, and immutable JSON Schema instructions. Treat transcript as delimited data, include only profile facts permitted for the scene, and persist `prompt_version + schema_version` in every analysis attempt.

- [ ] **Step 5: Run prompt tests and commit**

Run: `cd backend && uv run pytest tests/unit/prompts -v`

Expected: PASS, including concurrent edit conflict and failed atomic replacement.

Commit: `git commit -m "feat: add versioned scene prompt engine"`

---

### Task 7: 六场景分析、隐藏画像和模型失败恢复

**Files:**
- Create: `backend/src/audio_memory/analysis/orchestrator.py`
- Create: `backend/src/audio_memory/analysis/parser.py`
- Create: `backend/src/audio_memory/analysis/profile.py`
- Create: `backend/src/audio_memory/analysis/publisher.py`
- Create: `backend/tests/unit/analysis/test_scene_parser.py`
- Create: `backend/tests/integration/test_analysis_pipeline.py`

**Interfaces:**
- `AnalysisOrchestrator.run(job_id, provider_snapshot) -> AnalysisOutcome`.
- `SceneAnalyzer.analyze(scene_id, transcript, profile) -> SceneResult`.
- `ProfileMerger.merge(existing, delta, evidence) -> list[ProfileFact]`.
- `Publisher.publish(job_id, scene_results, profile_delta) -> BatchView`.

- [ ] **Step 1: Write scene-generation and no-empty-card tests**

Test `should_generate=false` omission, five card order, todo aggregation outside batch cards, one parenting/content/growth/inspiration card per upload, evidence ownership, and profile facts not leaking other speakers into the user subject.

- [ ] **Step 2: Implement per-scene calls with checkpoints**

Run the six scene Prompts as independently checkpointed requests against one locked provider/model/Prompt snapshot. After the scene calls, run one additional internal, non-user-editable profile-extraction request over the full transcript and existing profile; its output is a profile delta, not a visible card. Execute calls sequentially in phase one to reduce rate-limit bursts. Temporary network/provider failures retry at most twice with bounded backoff; auth, permission, balance, schema and protocol errors do not loop indefinitely.

- [ ] **Step 3: Parse, validate and repair once**

Validate provider output against `SceneResult`. For JSON syntax/schema failure, issue one repair request containing only validation errors and the invalid JSON, never the API Key. If repair fails, mark model analysis failed and keep the complete transcript for retry or provider switch.

- [ ] **Step 4: Merge hidden profile and publish atomically**

Profile facts include value, confidence, source audio, first/last seen time, evidence count, explicit/inferred flag, status and subject ID. Publish only after all six scenes and profile merge succeed; move audio from staging to `audio/{batch_id}/` using a commit manifest, then commit formal SQLite rows. Recovery reconciles a moved-file/database-rollback mismatch before exposing history.

- [ ] **Step 5: Test provider switch without retranscription and commit**

Run: `cd backend && uv run pytest tests/unit/analysis tests/integration/test_analysis_pipeline.py -v`

Expected: analysis failure retains transcripts; switching provider creates a new attempt using the same transcript IDs and never calls Whisper again.

Commit: `git commit -m "feat: implement multi-scene analysis pipeline"`

---

### Task 8: 信息流、历史、待办、追问、反馈和清除历史 API

**Files:**
- Create: `backend/src/audio_memory/content/service.py`
- Create: `backend/src/audio_memory/content/feedback.py`
- Create: `backend/src/audio_memory/content/clear.py`
- Create: `backend/src/audio_memory/api/content.py`
- Create: `backend/tests/integration/test_content_api.py`
- Create: `backend/tests/integration/test_feedback_files.py`
- Create: `backend/tests/integration/test_clear_history.py`

**Interfaces:**
- `GET /api/feed`, `GET /api/history`.
- `PATCH /api/todos/{id}`, `DELETE /api/todos/{id}`.
- `POST /api/cards/{id}/questions` scoped to the card and source transcript.
- `POST /api/cards/{id}/feedback`.
- `DELETE /api/history` with body `{confirm: true}`.

- [ ] **Step 1: Write feed/history ordering tests**

Assert natural-day grouping, newest batch first, fixed card order, global todos above dates, completed todos last, overdue incomplete todos highlighted, and history containing completed batches only.

- [ ] **Step 2: Implement todo and scoped QA services**

Todo edits trim but do not silently replace blank text; completion is user-driven; delete is immediate local deletion. QA context contains only current card content, its transcript, relevant profile facts and complete prior QA for that card.

- [ ] **Step 3: Implement feedback files**

Write one UTF-8 JSON record per submission under `意见反馈/{date}/{feedback_id}.json` using temp file + fsync + atomic replace. Include scene, audio metadata, full transcript, complete generated content, provider/model/Prompt/Schema versions, rating/comment and complete QA. `内容不准` requires a non-empty comment.

- [ ] **Step 4: Implement clear-history boundary**

Within a coordinated maintenance lock, cancel active work, delete formal audio/transcripts/cards/todos/QA/profile/history rows and associated files, then vacuum asynchronously. Preserve provider metadata, Keychain entries, Prompt files/versions and feedback files.

- [ ] **Step 5: Run content boundary tests and commit**

Run: `cd backend && uv run pytest tests/integration/test_content_api.py tests/integration/test_feedback_files.py tests/integration/test_clear_history.py -v`

Expected: PASS, including crash-safe feedback creation and preservation checks after clear.

Commit: `git commit -m "feat: add persistent content and feedback APIs"`

---

### Task 9: 将全屏 React 原型接入真实 API

**Files:**
- Modify: `prototype/src/App.jsx`
- Delete: `prototype/src/mockEngine.js`
- Modify: `prototype/src/store.js`
- Modify: `prototype/src/api/client.js`
- Create: `prototype/src/hooks/useProviders.js`
- Create: `prototype/src/hooks/useActiveJob.js`
- Create: `prototype/src/pages/FeedPage.jsx`
- Create: `prototype/src/pages/HistoryPage.jsx`
- Create: `prototype/src/pages/PromptPage.jsx`
- Create: `prototype/src/components/providers/ProviderModal.jsx`
- Create: `prototype/src/components/upload/UploadPanel.jsx`
- Create: `prototype/src/components/cards/CardDetail.jsx`
- Modify: `prototype/src/styles.css`
- Create: `prototype/tests/api-state.test.mjs`
- Modify: `prototype/package.json`
- Modify: `prototype/package-lock.json`

**Interfaces:**
- Browser state is a projection of backend responses; localStorage may keep only non-authoritative UI preferences such as last route.
- Existing routes remain `/`, `/history`, `/settings/prompts` in one tab.

- [ ] **Step 1: Add API contract fixtures and failing reducer tests**

Cover provider `initializing/keychain_unavailable/rate_limited`, candidate session cancellation, upload pause, transcription/analysis progress, interrupted recovery, completed publication, detail QA, feedback modal and clear-history reset.

- [ ] **Step 2: Split `App.jsx` without visual redesign**

Move responsibilities into focused pages/components while preserving existing classes, spacing, typography, card anatomy, detail overlay, feedback modal and navigation. Do not import old `designs/` review frames.

- [ ] **Step 3: Replace simulation with backend state**

Use `/api/providers`, job APIs/SSE, feed/history/Prompt APIs and content mutations. The right feed retains its prior content during upload/transcription/analysis and refreshes only after a completed publication event.

- [ ] **Step 4: Implement all frozen provider interactions**

Use one modal session UUID per open. Keep candidate Key only in component memory; abort the request on close; never store or echo masked Key. Implement cooldown with a monotonic client countdown and refresh providers when it reaches zero.

- [ ] **Step 5: Run frontend tests, build and visual QA**

Run: `cd prototype && node --test tests/*.test.mjs && npm run build && npm run test:sites`

Expected: all tests PASS and production build exits 0. Capture the same viewport states used in `screenshots/` and compare layout before accepting changes.

Commit: `git commit -m "feat: connect product UI to local backend"`

---

### Task 10: 安装、启动、诊断和本地静态资源服务

**Files:**
- Create: `scripts/install.sh`
- Create: `scripts/start.sh`
- Create: `scripts/doctor.sh`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `prototype/vite.config.mjs`
- Create: `backend/tests/integration/test_static_routes.py`
- Create: `tests/install-smoke.sh`

**Interfaces:**
- `./scripts/install.sh` prepares Python 3.12 environment, ffmpeg, MLX model and frontend build.
- `./scripts/start.sh` starts one local service and prints/opens its localhost URL.
- `./scripts/doctor.sh` performs read-only checks and prints copyable fixes.

- [ ] **Step 1: Write installer smoke assertions**

Check Darwin/arm64 rejection messages, paths containing spaces, idempotent second install, missing ffmpeg, unavailable model files, occupied port, existing healthy service and nonzero exit on fatal dependency failure.

- [ ] **Step 2: Implement deterministic install**

Use `uv` to install pinned Python 3.12 dependencies into the application directory, `npm ci` for the frontend, `npm run build`, ffmpeg presence verification and an explicit MLX model download with checksum/manifest. Do not modify shell profile files.

- [ ] **Step 3: Serve built frontend from FastAPI**

Serve Vite assets and return `index.html` for `/`, `/history`, `/settings/prompts`; keep `/api/*` separate. Bind only to `127.0.0.1`, choose a configured fixed port, and require the kernel instance lock before listening.

- [ ] **Step 4: Implement doctor output**

Report platform, free disk, Python/Node/ffmpeg, MLX model, writable Application Support, Keychain accessibility, SQLite migration, current backend health and last normalized error. Never print Keychain data or raw provider responses.

- [ ] **Step 5: Test clean install and commit**

Run: `bash tests/install-smoke.sh && cd backend && uv run pytest tests/integration/test_static_routes.py -v`

Expected: a clean temporary-home install starts and all three frontend routes return HTTP 200.

Commit: `git commit -m "feat: add macOS install and launch workflow"`

---

### Task 11: 端到端验收、隐私检查和真实服务冒烟

**Files:**
- Create: `prototype/tests/e2e/first-run.spec.js`
- Create: `prototype/tests/e2e/upload-analysis.spec.js`
- Create: `prototype/tests/e2e/recovery.spec.js`
- Create: `prototype/tests/e2e/content-actions.spec.js`
- Create: `backend/tests/e2e/test_secret_absence.py`
- Create: `docs/qa/phase-1-acceptance.md`
- Modify: `prototype/package.json`
- Modify: `prototype/package-lock.json`

**Interfaces:**
- Playwright runs against one real local backend with fake provider adapters by default.
- Real provider smoke tests are opt-in and read test Keys from temporary Keychain accounts, never environment logs.

- [ ] **Step 1: Implement first-run and provider E2E**

Verify empty feed, three-provider modal, failed candidate preservation while open, close cancellation, successful save, explicit activation, restart validation and Keychain-unavailable copy.

Add `@playwright/test` as a pinned dev dependency and a `test:e2e` package script. Browser binaries are installed by `scripts/install.sh` only for development/test mode, not for ordinary end-user installation.

- [ ] **Step 2: Implement complete batch E2E**

Upload MP3/AAC, observe individual progress, pause on unsupported file, remove and continue, transcribe, analyze six scenes, publish once, clear upload panel and verify history. Assert no current-batch card appears before completion.

- [ ] **Step 3: Implement recovery and alternate-provider E2E**

Kill the backend during transcription and analysis. Verify explicit recovery, checkpoint reuse, analysis failure retaining transcript, provider switch without Whisper rerun, cancellation cleanup and no failed batch in history.

- [ ] **Step 4: Implement content/feedback/clear E2E**

Test todo edit/complete/delete, all card details, right-aligned user bubble and AI bubble, scoped follow-up, feedback accurate/inaccurate branches, complete feedback JSON, Prompt save affecting only new analysis, and clear-history preservation boundaries.

- [ ] **Step 5: Run full quality gate and commit**

Run:

```bash
cd backend && uv run pytest -v
cd ../prototype && node --test tests/*.test.mjs && npm run build && npm run test:sites
npx playwright test
rg -n --hidden --glob '!node_modules/**' --glob '!dist/**' 'sk-[A-Za-z0-9]|Bearer [A-Za-z0-9]' .
```

Expected: all automated checks PASS; secret scan reports no committed or persisted Key; manual acceptance checklist has no P0/P1 failures.

Commit: `git commit -m "test: complete phase one acceptance coverage"`

---

## Delivery Order and Review Gates

| Milestone | Tasks | Independently testable result | Review gate |
|---|---:|---|---|
| M1 本地底座 | 1–2 | 可启动后端、迁移数据库、验证原子提交 | 架构与数据评审 |
| M2 模型配置 | 3 | 三厂商 Keychain 配置与冻结状态机 | API Key 专项验收 |
| M3 音频链路 | 4–5 | 本地上传、Whisper、恢复和取消 | 隐私与稳定性评审 |
| M4 AI 内容 | 6–8 | Prompt、六场景、画像、信息流与反馈 | Prompt/结果质量评审 |
| M5 完整产品 | 9–10 | 原型接真服务、可安装启动 | 全局 UI 与安装评审 |
| M6 验收版 | 11 | 端到端回归与真实厂商冒烟 | 第一阶段发布决策 |

## Requirement Coverage Matrix

| PRD 范围 | 实现任务 | 核心验收证据 |
|---|---:|---|
| FR-001–003 平台、安装、启动和长期保存 | 1、2、10、11 | 安装烟测、健康检查、重启持久化 |
| FR-010–015 厂商和 API Key | 3、9、11 | 冻结状态机测试、Keychain 集成、首次配置 E2E |
| FR-020–028 音频上传 | 4、9、11 | MP3/AAC、逐条进度、暂停恢复、重复文件 |
| FR-030–035 本地 Whisper | 5、11 | 本地网络隔离、分段进度、检查点恢复 |
| FR-040–048 分析与原子发布 | 2、7、11 | 六场景检查点、失败不发布、事务回滚 |
| FR-050–055 信息流 | 7–9、11 | 日期/批次排序、固定卡片顺序、无半成品 |
| FR-060–065 全局待办 | 7–9、11 | 聚合、编辑、手动完成、删除、过期标红 |
| FR-070–078 通用详情 | 6、8、9、11 | 动态模块降级、版本快照、覆盖式详情 |
| FR-080–099 五类卡片 | 6–9、11 | 各场景 Schema、汇总规则和外部字段 |
| FR-100–105 隐藏个人画像 | 7、8、11 | 首批建立、证据合并、主体隔离、清除 |
| FR-110–113 意见反馈 | 8、9、11 | 两分支弹窗、必填校验、完整本地记录 |
| FR-120–122 音频历史 | 2、8、9、11 | 只显示完成批次、日期倒序、只读列表 |
| FR-130–132 清除历史 | 8、9、11 | 删除范围与 Key/Prompt/反馈保留测试 |
| FR-140–148 Prompt 设置 | 6、9、11 | 六场景、编辑保存、原子版本、新分析生效 |
| 第9章失败、重试、恢复、取消 | 4、5、7、10、11 | 分阶段恢复矩阵、显式继续、取消清理 |

## Explicit Phase-One Decisions

- 使用 Python 3.12 后端是为了复用 MLX Whisper；前端继续使用现有 React/Vite 原型。
- 第一阶段上传队列在点击开始分析前不做崩溃恢复；上传阶段服务中断后清理并回到默认态。
- 转写开始后保存检查点；分析失败保留完整转写并支持切换厂商重试。
- 六场景分别调用，便于独立 Prompt 迭代和错误定位；第一阶段顺序执行，避免并发限流风暴。
- 默认模型通过 `config.py` 集中管理，历史结果保存实际模型快照；修改默认值不迁移历史。
- 后端运行期间冷却使用单调时钟，前端使用本地单调倒计时，归零后以后端刷新结果为准。
- 诊断环形缓冲区使用串行队列或单锁实现；50条低频记录不引入无锁结构。
- 候选 Key状态按 `session_id + candidate_validation_id` 隔离；正式 Key状态只按 `credential_generation` 更新。

## Definition of Done

- 用户可在全新 Apple Silicon Mac 上通过终端完成安装和启动。
- 三个厂商分别配置、校验、保存和切换；Key 不出现在页面、SQLite、文件或日志。
- MP3/AAC 从上传、本地 Whisper、六场景分析到信息流形成完整闭环。
- 任一失败点不会污染正式历史，转写后模型失败不会要求重新转写。
- 历史、待办、详情、追问、反馈、Prompt 和清除历史符合 PRD。
- 服务、浏览器或系统中断后的行为符合恢复矩阵。
- 自动化测试、构建、安装烟测、秘密扫描和手工验收全部通过。

## Technical References

- MLX Whisper setup and Python API: <https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md>
- Moonshot Kimi K2.5 official repository and API compatibility notes: <https://github.com/MoonshotAI/Kimi-K2.5>
- DeepSeek current Chat Completions model IDs: <https://api-docs.deepseek.com/api/create-chat-completion>
- OpenAI model catalog and versioning: <https://platform.openai.com/docs/api-reference/models/object>
- OpenAI Structured Outputs response format: <https://platform.openai.com/docs/api-reference/responses>

Provider model IDs are configuration snapshots dated 2026-08-05. Before a release build, adapter contract tests must verify that each configured default remains available; changing a default requires a reviewed configuration commit and does not rewrite historical model snapshots.
