# Historical-Quality DeepSeek Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse the frozen 3,442 reliable transcript segments to recover multi-event, evidence-backed analysis at the quality level of the historical reports, while preventing uncertain identity from becoming a hard user attribution.

**Architecture:** Deterministically split the reliable transcript into bounded, non-overlapping analysis windows; run the existing strict DeepSeek event-map request once per window; validate, namespace, and merge the local maps on the server. Feed the six existing scenes a compact event-grouped evidence projection, broaden the fixed meeting rules to valuable work communication, skip profile extraction when identity is unknown, and reject semantically implausible all-empty long-audio publications.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, httpx, pytest/pytest-asyncio, React/Vite, Playwright, SQLite, DeepSeek V4 Flash.

## Global Constraints

- Work only in `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1` on branch `codex/local-fast-v0-1`.
- Reuse source job `d29475e4-f148-4b99-9b7e-1e5751da1e48` and exactly its 3,442 reliable segments; do not execute VAD, Whisper, risk classification, selective refinement, diarization, or compact transcription.
- Keep model `deepseek-v4-flash`, `thinking={"type":"disabled"}`, temperature `0`, JSON response format, event-map `32768/180s`, scene `16384/120s`, profile `8192/120s`, one transient retry, one Schema repair, and serialized remote calls.
- Window parameters are frozen for this run: silence gap `45_000ms`, maximum window span `1_200_000ms`, and maximum `400` segments; split on every file boundary.
- Reliable global user identity requires a non-empty stable speaker ID, support from at least two independent windows, confidence `>=0.85`, unique evidence IDs, and evidence contained in the reliable transcript.
- Unknown identity may produce objective event cards and nested `meeting_todos` with `owner_type="unknown"`; it may not produce global todos, user behavior evaluation, user reactions/interests, or profile candidates.
- Six scene IDs, strict scene Schemas, card publisher transaction, evidence playback, and current frontend information architecture remain unchanged.
- Never persist or log original audio paths, transcript text, full model requests/responses, API keys, or screenshots containing personal content.
- Do not start the compact transcription plan until the user accepts the resulting page.

---

## File Structure

- `backend/src/audio_memory/analysis/windows.py`: immutable analysis-window parameters, deterministic window construction, local map completion, event namespacing, global merge, and semantic quality validation.
- `backend/src/audio_memory/analysis/runner.py`: orchestrates one event-map call per window, checkpoints only the merged map, runs the quality gate, and skips profile extraction when identity is unknown.
- `backend/src/audio_memory/prompts/composer.py`: composes distinguishable local event-map requests and an event-grouped compact scene evidence packet.
- `backend/src/audio_memory/analysis/provider.py`: includes window parameters in the safe DeepSeek parameter fingerprint.
- `backend/src/audio_memory/prompts/event_schema.py`, `system.md`, `evidence.py`: use one consistent `0.85` global identity threshold.
- `backend/src/audio_memory/prompts/event-map.md`, `common-scene.md`, `defaults/meeting.md`: encode local event recall, valuable work-communication coverage, and uncertain-attribution language.
- `backend/src/audio_memory/analysis/errors.py`, `backend/src/audio_memory/api/jobs.py`: make `analysis_quality_insufficient` safely retryable without retranscription.
- `backend/tests/unit/analysis/test_windows.py`: protects the pure window, merge, identity, and quality behavior.
- `backend/tests/unit/prompts/test_composer.py`, `test_evidence_integrity.py`: protect compact packets and attribution boundaries.
- `backend/tests/integration/test_event_map_pipeline.py`: protects multi-pass orchestration, checkpoint atomicity, profile skipping, and publication blocking.
- `backend/tests/integration/test_upload_jobs.py`, `prototype/tests/e2e/recovery.spec.js`: protect analysis-only recovery for the new quality error.

### Task 1: Deterministic analysis windows

**Files:**
- Create: `backend/src/audio_memory/analysis/windows.py`
- Create: `backend/tests/unit/analysis/test_windows.py`

**Interfaces:**
- Produces: `ANALYSIS_WINDOW_GAP_MS = 45_000`, `ANALYSIS_WINDOW_MAX_SPAN_MS = 1_200_000`, and `ANALYSIS_WINDOW_MAX_SEGMENTS = 400`.
- Produces: `AnalysisWindow(window_id: str, file_id: str, start_ms: int, end_ms: int, segments: tuple[dict[str, object], ...])`.
- Produces: `build_analysis_windows(transcript: list[dict[str, object]]) -> list[AnalysisWindow]`.
- Guarantees: every input segment occurs in exactly one output window; window order is stable; files never mix; every window is non-empty.

- [ ] **Step 1: Write failing window-boundary tests**

Create synthetic segments and assert splitting at a file change, at a `45_000ms` gap, before a `1_200_000ms` span would be exceeded, and before a 401st segment would be appended. Assert a `44_999ms` gap does not split by itself.

```python
def test_build_analysis_windows_splits_on_frozen_boundaries() -> None:
    transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 46_000, 47_000),
        segment("seg_1_0", "file-b", 0, 1_000),
    ]

    windows = build_analysis_windows(transcript)

    assert [[item["segment_id"] for item in window.segments] for window in windows] == [
        ["seg_0_0"], ["seg_0_1"], ["seg_1_0"]
    ]
```

- [ ] **Step 2: Write failing coverage and stability tests**

Assert shuffled input is sorted by first-seen file order plus time/segment ID, no ID is lost or duplicated, `window_id` values are `window_0000`, `window_0001`, and each start/end equals the minimum/maximum contained segment boundary. Duplicate segment IDs and invalid time ranges must raise `AnalysisWindowError`.

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_windows.py
```

Expected: test collection fails because `audio_memory.analysis.windows` does not exist.

- [ ] **Step 4: Implement the minimal pure builder**

Implement the dataclass, constants, input validation, stable file-order map, and a single pass that flushes the current window before adding a segment that crosses any frozen boundary. Do not read the database or call a model from this module.

```python
should_split = bool(current) and (
    file_id != current_file_id
    or start_ms - current_end_ms >= ANALYSIS_WINDOW_GAP_MS
    or end_ms - current_start_ms > ANALYSIS_WINDOW_MAX_SPAN_MS
    or len(current) >= ANALYSIS_WINDOW_MAX_SEGMENTS
)
```

- [ ] **Step 5: Verify GREEN and commit**

Run the Step 3 command and expect all window tests to pass, then commit only the new module and test.

```bash
git add backend/src/audio_memory/analysis/windows.py backend/tests/unit/analysis/test_windows.py
git commit -m "feat: split long analysis into evidence windows"
```

### Task 2: Complete and merge local event maps safely

**Files:**
- Modify: `backend/src/audio_memory/analysis/windows.py`
- Modify: `backend/tests/unit/analysis/test_windows.py`
- Modify: `backend/src/audio_memory/prompts/event_schema.py:49-75`
- Modify: `backend/src/audio_memory/prompts/system.md:28-33`
- Modify: `backend/src/audio_memory/prompts/evidence.py:80-135`
- Modify: `backend/tests/unit/prompts/test_event_schema.py`
- Modify: `backend/tests/unit/prompts/test_evidence_integrity.py`

**Interfaces:**
- Produces: `complete_window_event_map(window: AnalysisWindow, generated: EventMap) -> EventMap`.
- Produces: `merge_window_event_maps(windows: list[AnalysisWindow], maps: list[EventMap]) -> EventMap`.
- Produces: globally unique IDs `event_w0000_<model_suffix>` with parent references rewritten to the same namespace.
- Guarantees: local unknown evidence is rejected; local unassigned IDs are server-owned; every event time range contains all of its evidence; the global map covers every input segment exactly once.

- [ ] **Step 1: Write failing local-completion tests**

Construct a two-segment window and a generated map assigning only the first segment while omitting `unassigned_segment_ids`. Assert completion fills the second. Add cases for an unknown evidence ID, duplicate assignment, and an event whose time range does not contain its evidence; each must raise `AnalysisWindowError` without exposing the ID list in its string form.

- [ ] **Step 2: Write failing namespace and merge tests**

Return `event_001` from two windows. Assert the merged IDs are `event_w0000_001` and `event_w0001_001`, parent IDs are rewritten, event order follows window order, coverage equals the union of window segments, and model-provided unassigned values cannot remove server-computed coverage.

- [ ] **Step 3: Write failing identity-consensus tests**

Assert one high-confidence local identity remains globally unknown; the same speaker ID with confidence `0.90` in two windows becomes globally reliable; conflicting speaker IDs remain unknown; confidence `0.84` never becomes reliable. Update existing schema and integrity tests to expect `0.85`, including the error message.

- [ ] **Step 4: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_windows.py backend/tests/unit/prompts/test_event_schema.py backend/tests/unit/prompts/test_evidence_integrity.py
```

Expected: missing completion/merge functions and existing `0.70` identity assertions fail.

- [ ] **Step 5: Implement local completion and namespaced merge**

Validate the model payload with `EventMap`, derive known/assigned/unassigned sets from the window, verify each event boundary against a segment lookup, rewrite event and parent IDs with `model_copy`, and validate again. Merge only completed maps. Build the global identity from a speaker-to-window support table and use confidence `min(local confidences)` for a successful consensus.

```python
qualified = {
    speaker_id: candidates
    for speaker_id, candidates in support.items()
    if len({window_id for window_id, _ in candidates}) >= 2
    and min(item.confidence for _, item in candidates) >= 0.85
}
```

If exactly one speaker qualifies, union its evidence IDs in stable order. Otherwise return `speaker_id=None`, confidence `0`, an explicit uncertainty reason, and empty evidence.

- [ ] **Step 6: Unify the global identity threshold**

Change `UserSpeaker.validate_reliable_evidence`, `UserSpeaker.is_reliable`, fixed system instructions, and evidence-integrity checks from `0.70` to literal `0.85`. Do not lower scene card confidence or todo confidence thresholds.

- [ ] **Step 7: Verify GREEN and commit**

Run the Step 4 command and commit the pure merge plus threshold changes.

```bash
git add backend/src/audio_memory/analysis/windows.py backend/tests/unit/analysis/test_windows.py backend/src/audio_memory/prompts/event_schema.py backend/src/audio_memory/prompts/system.md backend/src/audio_memory/prompts/evidence.py backend/tests/unit/prompts/test_event_schema.py backend/tests/unit/prompts/test_evidence_integrity.py
git commit -m "feat: merge local event maps with conservative identity"
```

### Task 3: Orchestrate multi-pass event maps and compact scene evidence

**Files:**
- Modify: `backend/src/audio_memory/prompts/composer.py:58-175`
- Modify: `backend/src/audio_memory/analysis/runner.py:116-316`
- Modify: `backend/src/audio_memory/analysis/provider.py:42-62`
- Modify: `backend/src/audio_memory/prompts/event-map.md`
- Modify: `backend/tests/unit/prompts/test_composer.py`
- Modify: `backend/tests/unit/analysis/test_provider.py`
- Modify: `backend/tests/integration/test_event_map_pipeline.py`

**Interfaces:**
- Changes: `PromptComposer.compose_event_map(..., window_id: str | None = None)` sets safe diagnostic `scene_id` to `event-map:<window_id>` when supplied while preserving `event-map` for existing single-window callers.
- Produces: `PromptComposer._scene_transcript(transcript, event_map)` returning event-grouped assigned evidence with segment fields `{id,start_ms,end_ms,speaker_id,text}`.
- Changes: `AnalysisRunner._event_map` builds windows, calls the provider serially for each window, completes each local map, merges once, and checkpoints only the global map.
- Changes: the DeepSeek parameter fingerprint includes the three frozen window constants and the two-window identity consensus rule.

- [ ] **Step 1: Write failing local request and scene-projection tests**

Assert a local event-map request has diagnostic scene ID `event-map:window_0003`, unchanged token/timeout policy, only that window's segments, and intact text. For a scene request, assert unassigned segments are absent, assigned segments appear exactly once under their event, speaker IDs are retained, database file metadata/reliability fields are absent, and `segment_count` equals the number of assigned evidence segments.

- [ ] **Step 2: Write failing multi-pass runner test**

Seed three reliable transcript segments separated into two windows. Use a recording provider that returns `event_001` for both calls. Assert provider calls are `event-map:window_0000`, `event-map:window_0001`, followed by the six scene IDs; assert the checkpoint contains two namespaced events and complete coverage. Assert the publisher receives all six scenes only after the merged map is stored.

- [ ] **Step 3: Write failing atomicity and fingerprint tests**

Make the second local event-map call fail with `event_map_schema_invalid`. Assert `event_map_json is None`, `staged_results_json == {}`, profile and publisher are not called, and the exact error survives. Assert the parameter fingerprint changes when a monkeypatched window constant changes and does not contain transcript content.

- [ ] **Step 4: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/prompts/test_composer.py backend/tests/unit/analysis/test_provider.py backend/tests/integration/test_event_map_pipeline.py
```

Expected: the composer has no window identifier or scene projection and the runner makes only one event-map call.

- [ ] **Step 5: Implement local request composition and compact scene packets**

Add the optional window ID, keep per-segment event-map fields unchanged, and build scene groups from the final event map. Use an input segment lookup and event order; omit unassigned segments entirely. Preserve HTML/XML escaping through `_untrusted_packet`.

- [ ] **Step 6: Implement serialized multi-pass orchestration**

In `_event_map`, return a valid existing global checkpoint unchanged. Otherwise build windows and loop in stable order. Before and after every provider call, retain the existing ownership and credential-generation fences. Convert `AnalysisWindowError` to `ProviderAnalysisError(code="event_map_coverage_invalid")` without embedding segment IDs. Merge all local maps, perform the existing global coverage check, log only window/event/count metrics, and then use the current atomic checkpoint update.

- [ ] **Step 7: Strengthen local event recall instructions**

Update `event-map.md` to state that the input is one bounded local window; it must split distinct activity goals, participants, media sources, recruiting calls, work discussions, and topic transitions inside that window. It must not label an entire window `casual_chat` when it contains work, interview, media, parenting, commitment, or product-decision evidence.

- [ ] **Step 8: Verify GREEN and commit**

Run the Step 4 command and commit the orchestration as one independently testable change.

```bash
git add backend/src/audio_memory/prompts/composer.py backend/src/audio_memory/analysis/runner.py backend/src/audio_memory/analysis/provider.py backend/src/audio_memory/prompts/event-map.md backend/tests/unit/prompts/test_composer.py backend/tests/unit/analysis/test_provider.py backend/tests/integration/test_event_map_pipeline.py
git commit -m "feat: analyze transcript windows before scene synthesis"
```

### Task 4: Restore valuable work communication without unsafe attribution

**Files:**
- Modify: `backend/src/audio_memory/prompts/common-scene.md`
- Modify: `backend/src/audio_memory/prompts/defaults/meeting.md`
- Modify: `backend/src/audio_memory/analysis/runner.py:140-195`
- Modify: `backend/tests/e2e/test_prompt_eval_contract.py`
- Modify: `backend/tests/unit/prompts/test_evidence_integrity.py`
- Modify: `backend/tests/integration/test_event_map_pipeline.py`

**Interfaces:**
- Fixed rule: meeting analysis includes evidence-rich recruiting interviews, career/product/business discussions, responsible-person communication, and informal work conversations with conclusions, trade-offs, open questions, or action value.
- Fixed rule: when global identity is unknown, objective cards use role language, nested action items use `owner_type="unknown"`, and all top-level global todos remain empty.
- Runner behavior: call `profile_extractor.extract` only when `event_map.user_speaker.is_reliable`; otherwise persist an empty profile candidate list and continue publication.

- [ ] **Step 1: Write failing work-communication prompt contract tests**

Add a privacy-safe synthetic recruiting/work fixture. Assert fixed instructions explicitly include recruiting, career/product discussion, role-based language for unknown identity, and the distinction between nested pending actions and global todos. Assert ordinary social chat remains a negative case.

- [ ] **Step 2: Write failing attribution-boundary tests**

Build an unknown-identity `MeetingSceneResult` with one objective card and one nested `meeting_todos` item owned by `unknown`; assert evidence integrity accepts it. Add the same item to top-level `todos` as user-owned and assert integrity rejects it. Assert a growth card and content `user_reactions` still require reliable identity.

- [ ] **Step 3: Write failing profile-skip integration test**

Use an unknown-identity merged event map and an extractor that fails if called. Run the pipeline with a visible objective meeting card and assert publication succeeds with `profile_delta == []` and zero profile calls. Add a reliable two-window identity case and assert the extractor is called once.

- [ ] **Step 4: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/e2e/test_prompt_eval_contract.py backend/tests/unit/prompts/test_evidence_integrity.py backend/tests/integration/test_event_map_pipeline.py
```

Expected: fixed instructions exclude informal work communication and the runner always calls profile extraction.

- [ ] **Step 5: Implement fixed work-recall and attribution rules**

Update the fixed common rules first so existing prompt snapshots receive the behavior. Update the default meeting prompt for future snapshots. Require objective wording when identity is unknown; put explicit but unattributed actions only in `MeetingDetail.meeting_todos` with `owner_type="unknown"`, `assignee_text` set to the spoken role when available, and no top-level todo duplication.

- [ ] **Step 6: Skip unsafe profile extraction**

Branch in the runner after scene validation: reliable identity follows the existing extractor/save/validate path; unknown identity calls `_save_profile_candidates(version.id, [], ...)` and validates the empty delta without a provider request.

- [ ] **Step 7: Verify GREEN and commit**

Run the Step 4 command and commit the prompt and profile behavior.

```bash
git add backend/src/audio_memory/prompts/common-scene.md backend/src/audio_memory/prompts/defaults/meeting.md backend/src/audio_memory/analysis/runner.py backend/tests/e2e/test_prompt_eval_contract.py backend/tests/unit/prompts/test_evidence_integrity.py backend/tests/integration/test_event_map_pipeline.py
git commit -m "feat: recover objective work communication cards"
```

### Task 5: Block semantically empty long-audio publication

**Files:**
- Modify: `backend/src/audio_memory/analysis/windows.py`
- Modify: `backend/src/audio_memory/analysis/runner.py:140-195`
- Modify: `backend/src/audio_memory/analysis/errors.py:4-21`
- Modify: `backend/tests/unit/analysis/test_windows.py`
- Modify: `backend/tests/integration/test_event_map_pipeline.py`
- Modify: `backend/tests/integration/test_upload_jobs.py:190-230`
- Modify: `prototype/tests/e2e/recovery.spec.js`

**Interfaces:**
- Produces: `validate_analysis_quality(transcript, event_map, results) -> None`, raising `AnalysisQualityError` with a content-free reason code.
- Runner maps every `AnalysisQualityError` to `ProviderAnalysisError(code="analysis_quality_insufficient")` before profile extraction and publication.
- Retry API accepts `analysis_quality_insufficient` and creates a new analysis-only version without transcription work.

- [ ] **Step 1: Write failing quality-gate unit tests**

Cover all frozen conditions: two-hour transcript with one event; one low-confidence event covering at least 80% of span; all-empty scenes despite valuable event types; all-empty scenes with at least 10,000 text characters. Add passing cases for a short empty recording, a multi-event long recording with a visible card, and a long recording containing only unassigned noise below the text threshold.

```python
with pytest.raises(AnalysisQualityError) as captured:
    validate_analysis_quality(long_transcript, one_event_map, empty_results())
assert captured.value.reason == "long_audio_undersegmented"
```

- [ ] **Step 2: Write failing pipeline and retry tests**

Assert the runner marks the version/job `analysis_quality_insufficient`, does not call profile or publisher, and retains the merged event-map checkpoint plus six staged scene results for diagnosis. Add the error code to the retry API parameterization and browser recovery fixture; assert the page keeps “不会再次执行 Whisper” and posts only to `/retry-analysis`.

- [ ] **Step 3: Verify RED**

Run:

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_windows.py backend/tests/integration/test_event_map_pipeline.py backend/tests/integration/test_upload_jobs.py
cd prototype
node --test tests/*.test.mjs
npx playwright test tests/e2e/recovery.spec.js
```

Expected: no semantic gate exists and the new error code is not retryable.

- [ ] **Step 4: Implement the pure quality check and runner mapping**

Compute per-file transcript spans, total text characters, event durations, event types, and whether any scene generated a card or todo. Do not inspect keywords or private text. Run the check after all six results have passed evidence integrity but before profile extraction. Persist only the normalized error code through the existing failure path.

- [ ] **Step 5: Add analysis-only recovery**

Add `analysis_quality_insufficient` to `ANALYSIS_RETRYABLE_ERROR_CODES`. Reuse the existing generic page rendering; only extend the E2E parameter set because the UI already displays safe codes and preserves the analysis-only retry message.

- [ ] **Step 6: Verify GREEN and commit**

Run the Step 3 commands and commit the quality boundary.

```bash
git add backend/src/audio_memory/analysis/windows.py backend/src/audio_memory/analysis/runner.py backend/src/audio_memory/analysis/errors.py backend/tests/unit/analysis/test_windows.py backend/tests/integration/test_event_map_pipeline.py backend/tests/integration/test_upload_jobs.py prototype/tests/e2e/recovery.spec.js
git commit -m "fix: reject empty long-audio analysis"
```

### Task 6: Full regression, real 3,442-segment run, and page handoff

**Files:**
- Create: `docs/benchmark-evidence/2026-08-10-historical-quality-analysis.md`
- Modify only if fresh verification exposes a defect covered by this design; every defect requires a new failing test before production changes.

**Interfaces:**
- Verifies: all unit/integration tests, frontend tests/build, privacy scan, analysis-only real run, aggregate historical-parity checks, and browser evidence playback.

- [ ] **Step 1: Run focused backend tests**

```bash
/Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q backend/tests/unit/analysis/test_windows.py backend/tests/unit/analysis/test_provider.py backend/tests/unit/prompts/test_composer.py backend/tests/unit/prompts/test_event_schema.py backend/tests/unit/prompts/test_evidence_integrity.py backend/tests/integration/test_event_map_pipeline.py backend/tests/integration/test_upload_jobs.py backend/tests/e2e/test_prompt_eval_contract.py
```

Expected: all selected tests pass with zero failures.

- [ ] **Step 2: Run the full backend suite**

```bash
env UV_CACHE_DIR='../.uv-cache' /Users/liujinxin/Documents/音频Always\ on\ Demo/backend/.venv/bin/python -m pytest -q
```

Run from `backend/`. Expected: zero failures and no warnings containing private request/response content.

- [ ] **Step 3: Run frontend tests and production build**

```bash
node --test tests/*.test.mjs
npx playwright test tests/e2e/recovery.spec.js
npm run build
```

Run from `prototype/`. Expected: all tests pass and the Vite/Sites build exits `0`.

- [ ] **Step 4: Verify branch scope and privacy**

Run `git diff --check`, inspect `git status --short`, and search the new diff for API key patterns, transcript fixture text, source audio paths, full request/response fields, or personal-content screenshots. The only pre-existing untracked paths allowed are `backend/.venv` and `prototype/node_modules`.

- [ ] **Step 5: Confirm the serving process uses the V0.1 worktree**

Resolve the process listening on `127.0.0.1:8765` and inspect its current working directory. If it points to the original workspace, stop only that known process and start the backend from this worktree using the existing approved server command. Do not terminate unrelated Python processes.

- [ ] **Step 6: Create and run one analysis-only version**

Use the existing `/api/jobs/d29475e4-f148-4b99-9b7e-1e5751da1e48/retry-analysis` product path or the same coordinator entrypoint. Before and after the run, verify aggregate transcript counts remain `4,117 total / 3,442 reliable / 675 discarded` and local transcription-stage call counts remain zero.

Record only: new version ID, status/error code, elapsed time, window count, logical request count, token totals, event count/types, assigned/unassigned counts, scene generation flags, card/todo/profile counts, and evidence-reference validity.

- [ ] **Step 7: Apply the historical-parity gate**

The real result is acceptable for page review only if it has more than one independent event, at least one visible evidence-backed card, no unknown evidence IDs, no all-empty successful publication, and card content visibly covers at least two of these previously confirmed categories: work/career discussion, recruiting/interview communication, résumé/next-round actions, AI-glasses/product method. Unknown identity may legitimately keep the global todo count at zero only when the card shows spoken actions as pending confirmation.

- [ ] **Step 8: Write the aggregate acceptance report and commit**

Create `docs/benchmark-evidence/2026-08-10-historical-quality-analysis.md` without transcript text, audio paths, API keys, full requests/responses, or screenshots. Include exact test counts and the aggregate real-run comparison. Run `git diff --check`, then commit the report.

```bash
git add docs/benchmark-evidence/2026-08-10-historical-quality-analysis.md
git commit -m "docs: record historical analysis parity"
```

- [ ] **Step 9: Open the history page for user acceptance**

Use the in-app browser to navigate to `http://127.0.0.1:8765/history`, leave the newly published analysis visible, and verify card opening plus at least one evidence playback entry. Stop at this checkpoint and wait for the user to accept the page before beginning `docs/superpowers/plans/2026-08-10-local-fast-v0-1-transcription.md`.
