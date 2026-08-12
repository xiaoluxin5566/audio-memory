# Local Fast V0.1 DeepSeek Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse the existing 3,442 reliable transcript segments to complete DeepSeek event-map, six-scene, profile, and atomic publication without rerunning any local transcription stage.

**Architecture:** Add one stage-aware request contract to the existing analysis provider client, keep the provider worker serialized, and make the DeepSeek adapter return both text and `finish_reason`. Compact only the event-map transcript projection, then complete and validate `unassigned_segment_ids` on the server before checkpointing. Preserve typed provider/schema/coverage failures through the runner, coordinator, API, and page while keeping request/response content out of diagnostics.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, httpx, pytest/pytest-asyncio, React/Vite, Playwright, SQLite.

## Global Constraints

- Work only in `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1` on branch `codex/local-fast-v0-1`.
- `docs/LOCAL-FAST-V0.1-PARAMETER-BASELINE.md` is the only V0.1 parameter source; the first real run must use its values unchanged and record a deterministic parameter fingerprint.
- Stage one must not run VAD, Whisper, risk classification, selective refinement, or diarization.
- Reuse source job `d29475e4-f148-4b99-9b7e-1e5751da1e48`; create a new analysis version and leave failed version `c65e86d7-5dc7-401f-90e0-96d92b01e866` intact.
- The event-map transcript projection contains file metadata once per file and only `id`, `start_ms`, `end_ms`, and `text` per segment; transcript text must not be removed.
- DeepSeek uses model `deepseek-v4-flash`, `thinking={"type":"disabled"}`, `temperature=0`, and `response_format={"type":"json_object"}`.
- Event map uses `max_tokens=32768` and timeout 180 seconds; every scene uses `max_tokens=16384` and timeout 120 seconds; profile uses `max_tokens=8192` and timeout 120 seconds.
- Network timeout, HTTP 429, and HTTP 5xx get at most one extra attempt; JSON/Schema failure gets at most one repair attempt; six scenes remain serial.
- Never persist or log an API key, original audio path, transcript text, full request, full response, or a screenshot containing personal content.
- Safe diagnostics may contain only provider/model, scene, parameter fingerprint, request/response byte counts, segment count, token usage, elapsed time, status category, `finish_reason`, repair flag, and coverage counts.
- Do not refactor the six scene schemas or publisher while implementing this plan.

---

## File Structure

- `backend/src/audio_memory/prompts/composer.py`: owns the immutable per-stage request policy and event-map compact projection.
- `backend/src/audio_memory/providers/adapters/base.py`: returns a normalized chat-completion result containing text and finish reason.
- `backend/src/audio_memory/providers/adapters/deepseek.py`: adds DeepSeek thinking-disabled behavior to formal requests as well as validation.
- `backend/src/audio_memory/analysis/provider.py`: builds requests, applies bounded retries, classifies provider responses, and stores safe in-memory diagnostics.
- `backend/src/audio_memory/analysis/events.py`: performs exactly one schema repair and converts the second invalid result into a typed safe error.
- `backend/src/audio_memory/prompts/event_schema.py`: permits the model to omit the server-owned unassigned list.
- `backend/src/audio_memory/analysis/runner.py`: completes event-map coverage locally, rejects unknown IDs, checkpoints only valid maps, and preserves error codes.
- `backend/src/audio_memory/analysis/task_coordinator.py`: acts only as a last-resort failure transition and never overwrites a version already failed with a specific code.
- `backend/src/audio_memory/api/jobs.py`: permits retry for every stage-one analysis error code without retranscription.
- `prototype/src/App.jsx`: shows the specific safe analysis error code and retains the “重新分析” path.
- `backend/tests/**`, `prototype/tests/**`: protect request contracts, coverage, error propagation, atomic publication, and page behavior.

### Task 1: Stage-aware request policy, DeepSeek envelope, and safe diagnostics

**Files:**
- Modify: `backend/src/audio_memory/prompts/composer.py:13-105`
- Modify: `backend/src/audio_memory/providers/adapters/base.py:1-34`
- Modify: `backend/src/audio_memory/providers/adapters/deepseek.py:1-9`
- Modify: `backend/src/audio_memory/analysis/provider.py:17-188`
- Create: `backend/tests/unit/analysis/test_provider.py`
- Modify: `backend/tests/integration/test_analysis_pipeline.py:24-355`

**Interfaces:**
- Produces: `ModelRequest.max_tokens: int`, `ModelRequest.timeout_seconds: float`, and `ModelRequest.segment_count: int`.
- Produces: `ChatCompletionResult(text: str, finish_reason: str | None)` from `ChatCompletionsAdapter.extract_result(body)`; existing `extract_text(body)` delegates to it for validation compatibility.
- Produces: `ProviderRequestDiagnostic` and `ProviderAnalysisClient.request_diagnostics`, containing safe aggregate fields only.
- Produces: `ProviderAnalysisClient.parameter_fingerprint`, the SHA-256 of canonical JSON containing the frozen model/request policy.
- Consumes: `request_with_one_repair` passes the three request-policy fields and repair flag to `ProviderAnalysisClient.generate` in Task 2.

- [ ] **Step 1: Write failing request-policy tests**

Add table-driven tests that compose event-map, scene, and profile requests and assert literal policies: `(32768, 180)`, `(16384, 120)`, and `(8192, 120)`. Add a MockTransport test for DeepSeek that captures the formal payload and asserts `thinking == {"type": "disabled"}`, `max_tokens` matches the scene, `temperature == 0`, JSON response format is present, and the HTTP request timeout matches the scene contract.

- [ ] **Step 2: Write failing response and diagnostic tests**

Return a response shaped as `{"choices":[{"message":{"content":"{}"},"finish_reason":"length"}],"usage":{"prompt_tokens":9,"completion_tokens":4}}`. Assert that the adapter exposes `finish_reason`, the client raises `ProviderAnalysisError(code="model_output_truncated")`, and its diagnostic contains byte counts/token counts/scene/elapsed/finish reason but does not contain the system prompt, user payload, response body, secret, or transcript fixture string.

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_provider.py backend/tests/integration/test_analysis_pipeline.py
```

Expected: failures for missing request-policy fields, formal DeepSeek thinking/max tokens, finish reason, truncation mapping, parameter fingerprint, and diagnostics.

- [ ] **Step 4: Implement the minimal request contract**

Add the literal frozen policies once in `composer.py` and have every `ModelRequest` carry its resolved policy. Construct the profile request with the same contract. Extend `generate`/`_request` with explicit keyword-only scene, max-token, timeout, segment-count, and repair parameters. For OpenAI Responses, map the output bound to `max_output_tokens`; for chat-completions, map it to `max_tokens`; for DeepSeek formal calls, add thinking disabled.

- [ ] **Step 5: Implement finish-reason classification and safe metrics**

Parse the first choice through `extract_result`. Map `finish_reason == "length"` to `model_output_truncated` before JSON parsing or repair, and content-filter/rejection finish reasons to `content_rejected`. Record canonical request byte length before send, response byte length from response content, usage, elapsed wall time, status category, and finish reason in a bounded in-memory list. Compute the parameter fingerprint from canonical, content-free policy JSON.

- [ ] **Step 6: Enforce one extra transient retry**

Change the retry loop from three total attempts to two total attempts. Keep retries limited to the existing timeout/network, 429, and 5xx classifications; do not retry authentication, balance, content rejection, invalid response, truncation, Schema, or coverage errors.

- [ ] **Step 7: Verify GREEN and commit**

Run the command from Step 3 and expect all selected tests to pass, then commit only Task 1 files:

```bash
git add backend/src/audio_memory/prompts/composer.py backend/src/audio_memory/providers/adapters/base.py backend/src/audio_memory/providers/adapters/deepseek.py backend/src/audio_memory/analysis/provider.py backend/tests/unit/analysis/test_provider.py backend/tests/integration/test_analysis_pipeline.py
git commit -m "fix: bound deepseek analysis requests"
```

### Task 2: One repair with typed Schema failures

**Files:**
- Modify: `backend/src/audio_memory/analysis/events.py:13-48`
- Modify: `backend/src/audio_memory/analysis/provider.py:191-256`
- Modify: `backend/tests/integration/test_analysis_pipeline.py:195-300`

**Interfaces:**
- Produces: `request_with_one_repair(..., invalid_code: str)`; it returns parsed data or raises `ProviderAnalysisError` with the supplied code after the second invalid result.
- Consumes: `ModelRequest` policy fields from Task 1 for both original and repair calls.
- Produces: event-map invalid output code `event_map_schema_invalid`; scene/profile invalid output code `model_response_invalid`.

- [ ] **Step 1: Write failing repair-boundary tests**

Assert that an event-map first invalid/second valid sequence makes two calls, the second call has `repair_attempted=True`, and both calls retain the event-map max-token/timeout policy. Assert that two invalid event-map responses raise `ProviderAnalysisError.code == "event_map_schema_invalid"` after exactly two calls. Add corresponding scene/profile assertions for `model_response_invalid`.

- [ ] **Step 2: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/integration/test_analysis_pipeline.py
```

Expected: the second invalid response currently escapes as `SceneOutputError`/`ValueError` and repair calls do not carry stage diagnostics.

- [ ] **Step 3: Implement typed repair errors**

Pass every request-policy field to both calls. Catch the second parse failure and raise a content-free `ProviderAnalysisError` with the caller-supplied code, preserving the validation exception only as the Python cause. Do not log or persist `raw`, `repair`, or the repair user payload.

- [ ] **Step 4: Verify GREEN and commit**

Run the Step 2 test, then:

```bash
git add backend/src/audio_memory/analysis/events.py backend/src/audio_memory/analysis/provider.py backend/tests/integration/test_analysis_pipeline.py
git commit -m "fix: preserve analysis schema errors"
```

### Task 3: Compact event-map projection and server-owned unassigned IDs

**Files:**
- Modify: `backend/src/audio_memory/prompts/composer.py:42-74`
- Modify: `backend/src/audio_memory/prompts/event-map.md:1-41`
- Modify: `backend/src/audio_memory/prompts/event_schema.py:123-157`
- Modify: `backend/tests/unit/prompts/test_composer.py:44-105`
- Modify: `backend/tests/unit/prompts/test_event_schema.py:1-208`

**Interfaces:**
- Produces: event-map `transcript_data` with `files: list[dict]` and `segments: list[{id,start_ms,end_ms,text}]`.
- Produces: `EventMap.unassigned_segment_ids` defaulting to an empty list so omission is parseable before local completion.
- Consumes: the full internal transcript remains unchanged for scene and profile calls.

- [ ] **Step 1: Write failing projection tests**

Build two transcript segments from the same file with distinct text. Decode the untrusted packet and assert exactly one file metadata object, exactly two segment objects, literal segment keys `{id,start_ms,end_ms,text}`, both original text values intact, and no per-segment file name/timezone/reliability/speaker fields. Keep the existing injection escaping assertions.

- [ ] **Step 2: Write failing omitted-unassigned test**

Validate an otherwise valid `EventMap` payload with no `unassigned_segment_ids` key and assert it parses with `[]`; retain tests that reject duplicate/overlapping unassigned IDs after the server fills the field.

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/prompts/test_composer.py backend/tests/unit/prompts/test_event_schema.py
```

Expected: the current packet repeats every file field and the schema requires the unassigned list.

- [ ] **Step 4: Implement the projection and prompt contract**

Deduplicate file metadata by `file_id` in first-seen order; project segments in source order without changing or normalizing text. Update `event-map.md` to state that the model outputs events/evidence and may omit `unassigned_segment_ids` because the server owns complete coverage. Keep the output field in the final schema for checkpoint compatibility.

- [ ] **Step 5: Verify GREEN and commit**

Run the Step 3 command, then:

```bash
git add backend/src/audio_memory/prompts/composer.py backend/src/audio_memory/prompts/event-map.md backend/src/audio_memory/prompts/event_schema.py backend/tests/unit/prompts/test_composer.py backend/tests/unit/prompts/test_event_schema.py
git commit -m "fix: compact deepseek event map input"
```

### Task 4: Local coverage completion and explicit event-map errors

**Files:**
- Modify: `backend/src/audio_memory/analysis/runner.py:112-215,217-283`
- Modify: `backend/tests/integration/test_event_map_pipeline.py:44-204,315-357`

**Interfaces:**
- Produces: complete checkpointed `EventMap` where `unassigned_segment_ids == sorted(known_ids - assigned_ids)`.
- Produces: `event_map_unknown_segment` when event or user-speaker evidence references an ID outside the reliable transcript.
- Produces: `event_map_coverage_invalid` for any remaining overlap/coverage invariant and `event_map_schema_invalid` for model Schema failure.
- Guarantees: no event-map checkpoint and no staged scene result is written on event-map failure.

- [ ] **Step 1: Write failing local-completion test**

Seed two reliable transcript segments and return an event map assigning only the first while omitting the unassigned field. Run `_event_map`, reload the version, and assert the second ID was filled locally and the exact completed map/hash was checkpointed.

- [ ] **Step 2: Write failing unknown-ID and atomicity tests**

Return an event whose evidence contains `seg_missing` and assert `ProviderAnalysisError.code == "event_map_unknown_segment"`, `event_map_json is None`, `staged_results_json == {}`, and the publisher was not called. Add the same assertion for an unknown user-speaker evidence ID. Add a malformed overlap case that becomes `event_map_coverage_invalid` without checkpointing.

- [ ] **Step 3: Write failing error-preservation test**

Have the provider raise `ProviderAnalysisError(code="event_map_schema_invalid")`, run the runner, and assert both `AnalysisVersion.error_code` and the source job error code retain that exact value.

- [ ] **Step 4: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/integration/test_event_map_pipeline.py
```

Expected: missing IDs currently cause a generic coverage `ValueError`; unknown/missing errors are not typed; omitted unassigned IDs are not completed.

- [ ] **Step 5: Implement local completion before final validation**

Derive `known_ids` from the frozen reliable transcript, `assigned_ids` from all event evidence, and separately include user-speaker evidence in the unknown-reference check. Reject unknown references first. Replace the model-provided unassigned list with the sorted set difference, revalidate the full `EventMap`, verify exact coverage, and only then serialize/hash/checkpoint it. Translate final validation/coverage exceptions to the explicit safe codes without including ID lists in the persisted error.

- [ ] **Step 6: Verify GREEN and commit**

Run the Step 4 command, then:

```bash
git add backend/src/audio_memory/analysis/runner.py backend/tests/integration/test_event_map_pipeline.py
git commit -m "fix: complete event map coverage locally"
```

### Task 5: Preserve specific errors through worker, retry API, and page

**Files:**
- Modify: `backend/src/audio_memory/analysis/task_coordinator.py:434-470`
- Modify: `backend/src/audio_memory/api/jobs.py:270-320`
- Modify: `backend/tests/unit/analysis/test_task_coordinator.py:300-324`
- Modify: `backend/tests/integration/test_upload_jobs.py:160-220`
- Modify: `prototype/src/App.jsx:210-290`
- Modify: `prototype/tests/e2e/recovery.spec.js:1-100`

**Interfaces:**
- Consumes: the explicit error codes defined in Global Constraints.
- Produces: retry eligibility for every recoverable stage-one analysis failure without entering the transcription pipeline.
- Produces: a failed job card that displays the safe concrete code while keeping the existing explanation that Whisper will not rerun.

- [ ] **Step 1: Write failing coordinator/API tests**

Use a runner that marks its version failed with `model_output_truncated` and then raises. Assert the coordinator does not replace the code. Parameterize retry API tests over all stage-one codes and assert a new analysis version is queued while transcript counts remain unchanged and no transcription task is created.

- [ ] **Step 2: Write failing page test**

Extend the recovery E2E fixture with `error_code: "event_map_unknown_segment"`. Assert the failed card visibly contains that code, explains that the transcript is retained, and the retry button calls only `/retry-analysis`.

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_task_coordinator.py backend/tests/integration/test_upload_jobs.py
cd prototype && node --test tests/*.test.mjs && npx playwright test tests/e2e/recovery.spec.js
```

Expected: the retry allowlist rejects new codes and the page hides the concrete error code.

- [ ] **Step 4: Implement minimal propagation and display**

Keep the coordinator fallback update fenced to versions still `running`; add a regression comment explaining why already-failed versions are untouched. Expand the retry allowlist to the explicit analysis codes. Render the code as safe diagnostic text on the failed job card; do not expose exception messages or provider bodies.

- [ ] **Step 5: Verify GREEN and commit**

Run the Step 3 commands, then:

```bash
git add backend/src/audio_memory/analysis/task_coordinator.py backend/src/audio_memory/api/jobs.py backend/tests/unit/analysis/test_task_coordinator.py backend/tests/integration/test_upload_jobs.py prototype/src/App.jsx prototype/tests/e2e/recovery.spec.js
git commit -m "fix: surface specific analysis failures"
```

### Task 6: Automated regression and privacy verification

**Files:**
- Modify only if a failing regression exposes a stage-one defect; do not broaden scope.

**Interfaces:**
- Verifies all Task 1-5 contracts together.

- [ ] **Step 1: Run focused backend tests**

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_provider.py backend/tests/integration/test_analysis_pipeline.py backend/tests/integration/test_event_map_pipeline.py backend/tests/unit/prompts/test_composer.py backend/tests/unit/prompts/test_event_schema.py backend/tests/unit/analysis/test_task_coordinator.py backend/tests/integration/test_upload_jobs.py
```

Expected: all pass with no warnings containing request/response content.

- [ ] **Step 2: Run the full backend suite**

```bash
cd backend && env UV_CACHE_DIR='../.uv-cache' /Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q
```

Expected: the 543-test baseline plus new tests all pass.

- [ ] **Step 3: Run frontend tests and production build**

```bash
cd prototype && node --test tests/*.test.mjs
cd prototype && npm run build
cd prototype && npx playwright test tests/e2e/recovery.spec.js
```

Expected: unit tests, production build, and recovery E2E pass.

- [ ] **Step 4: Inspect the diff for forbidden content and unintended scope**

Run `git diff --check`, `git status --short`, and inspect every changed file. Confirm no fixture/report/log contains transcript text, audio paths, secrets, full request/response bodies, or screenshots; confirm transcription/diarization/risk implementation files are unchanged.

- [ ] **Step 5: Commit any test-only corrections**

If and only if the preceding verification required a scoped correction, stage those exact files and commit with `test: verify deepseek analysis recovery`. Otherwise make no empty commit.

### Task 7: Reanalyze the frozen 3,442 segments and open the product page

**Files:**
- Create: `docs/benchmark-evidence/2026-08-10-local-fast-v0-1-deepseek.md`
- Do not add the database, audio, transcript, browser profile, page screenshot, request, or response to Git.

**Interfaces:**
- Consumes: source job `d29475e4-f148-4b99-9b7e-1e5751da1e48` and the existing reliable rows only.
- Produces: one new analysis version and, on success, an atomically published batch with cards/todos/evidence references.
- Produces: an aggregate-only acceptance report with model, parameter fingerprint, reliable count, request count, byte/token totals, finish reasons, per-stage elapsed times, coverage counts, final status, card/todo counts, and commit.

- [ ] **Step 1: Prove the source snapshot before mutation**

Run read-only aggregate queries and record only: source job ID, reliable count `3442`, total count `4117`, discarded count `675`, text character count `28470`, failed analysis version ID/status/code, empty event-map byte count, and empty staged-results byte count. Abort the real call if the reliable count or source job differs.

- [ ] **Step 2: Confirm and replace the stale local server**

Resolve the process listening on `127.0.0.1:8765` and inspect its current working directory. If it is the original V0 workspace server, terminate only that resolved PID gracefully. Start the backend from this V0.1 worktree with the existing local database and wait for its health endpoint; do not start a second server against the same database.

- [ ] **Step 3: Trigger analysis-only retry**

Call the product retry-analysis endpoint for the source job. Immediately verify a new analysis version exists, the source transcript row count is still 4,117, the reliable count is still 3,442, and no transcription task/temp Whisper directory was created.

- [ ] **Step 4: Monitor without exposing content**

Poll aggregate version/job state and safe in-memory request diagnostics. Stop on completed, provider-paused, failed, or a bounded overall deadline derived from 180 seconds for event map plus seven 120-second calls and the allowed retry/repair bounds. Never print response bodies or transcript rows.

- [ ] **Step 5: Verify publication invariants**

On success, assert: event-map JSON is non-empty; its assigned/unassigned union equals all 3,442 reliable IDs; unknown count is zero; all six staged scene keys were produced before publication; profile extraction completed; the version is completed; cards/todos are linked to the new version; every published evidence ID is in the known reliable set. On failure, assert the concrete error code is visible and no partial card/todo batch was published.

- [ ] **Step 6: Write the aggregate acceptance report**

Write only the allowed fields listed under Interfaces. Include explicit statements that VAD, Whisper, risk gate, refinement, and diarization calls were zero during this retry and that the old failed analysis version was retained.

- [ ] **Step 7: Open the product page for user acceptance**

Open the local product page in the app browser at the completed job/report view. Leave the server running and stop work before starting the compact plan. The user must be able to inspect cards, todos, evidence playback entries, final/error state, and progress. Do not save a screenshot to the repository.

- [ ] **Step 8: Commit the aggregate report**

After the real run reaches a terminal state and the report contains no forbidden fields:

```bash
git add docs/benchmark-evidence/2026-08-10-local-fast-v0-1-deepseek.md
git commit -m "test: record deepseek reanalysis acceptance"
```

## Plan Self-Review

- Spec coverage: request bounds, thinking disabled, one transient retry, one repair, compact event-map projection, local coverage completion, explicit errors, atomic publication, analysis-only retry, aggregate metrics, and page opening are each assigned to a task.
- Scope: no VAD/Whisper/risk/diarization implementation file is modified in stage one.
- Type consistency: every model call consumes the same `ModelRequest` policy fields; every typed failure is a `ProviderAnalysisError` consumed by `AnalysisRunner`; completed event maps remain the existing `EventMap` type.
- Privacy: tests use synthetic strings; the real-run report stores only aggregate counts and hashes; diagnostics never contain content-bearing fields.
- Completeness: every task has exact files, behaviors, commands, expected outcomes, and commit boundaries.
