# Scene Director, Dossier Evidence, and Meeting Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let all reliable transcript segments participate in a shared scene-selection stage, expand each selection into a bounded evidence dossier, route the six existing scenes through those dossiers, and restore detailed meeting/work-communication cards without retranscribing audio.

**Architecture:** Keep the persisted Event Map as a compatible timeline and publication anchor, but introduce a response-only `EventMapDraft`, stable transcript clusters, a fixed shared director, and immutable `SceneDossier` evidence scopes. The runner checkpoints the normalized director/dossier context with any stable supplemental anchors, composes each scene only from routed dossiers, and validates every result against a dossier rather than Event membership. Existing card schemas, atomic publication, history, playback, and frontend information architecture remain unchanged.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, SQLAlchemy async, httpx, pytest/pytest-asyncio, React/Vite, Playwright, SQLite, DeepSeek V4 Flash.

## Global Constraints

- Work only in `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/local-fast-v0-1` on branch `codex/local-fast-v0-1`.
- Reuse source job `d29475e4-f148-4b99-9b7e-1e5751da1e48` and its existing 3,442 reliable segments; do not execute VAD, Whisper, risk classification, selective refinement, diarization, or Compact.
- Treat `docs/superpowers/specs/2026-08-10-scene-director-meeting-context-design.md` Appendix A as the exact semantic baseline for `director.md`; only Schema/input-layer markup may be adapted mechanically.
- Preserve six scene IDs (`todo`, `meeting`, `parenting`, `content`, `growth`, `inspiration`), all existing scene result Schemas, card types, publisher transaction, history behavior, and playback routes.
- Full transcript cluster boundaries remain frozen at file change, gap `>=45_000ms`, span `<=1_200_000ms`, and `<=400` segments.
- Context expansion stays in the same file, at most one adjacent cluster per side, with final span `<=1_800_000ms` and final count `<=600`; never trim selected core clusters.
- `unassigned_segment_ids` is server-owned compatibility data only. It must not appear in the event model response Schema, director packets, scene packets, or dossier evidence admission.
- Reliable user identity still requires confidence `>=0.85` and valid transcript evidence. Unknown identity may use objective role wording and nested meeting actions owned by `unknown`, but cannot create user/shared global todos, personal growth claims, user reactions, or profile facts.
- DeepSeek remains `deepseek-v4-flash`, thinking disabled, temperature `0`, JSON response format, serialized calls, at most one transient retry, and at most one Schema repair. Director calls use the scene bound `16_384` tokens / `120s`.
- Fixed director rules, director Schema, cluster parameters, dossier parameters, and scene Schemas must participate in the reanalysis compatibility snapshot/hash.
- Never persist or log transcript text, original audio paths, full model request/response bodies, API keys, or screenshots containing personal content. Only aggregate counts, hashes, timings, token counts, and normalized errors may be recorded.
- Do not implement web search, other-scene Prompt rewrites, new cards/daily reports, frontend information architecture, or Compact in this plan.

---

## File Structure

- `backend/src/audio_memory/prompts/event_schema.py`: response-only `EventMapDraft` plus compatible persisted `EventMap`.
- `backend/src/audio_memory/analysis/clusters.py`: stable full-transcript clusters and Event-hint projection.
- `backend/src/audio_memory/prompts/director_schema.py`: strict director result contract and allowed value enums.
- `backend/src/audio_memory/prompts/director.md`: approved Appendix A fixed Prompt.
- `backend/src/audio_memory/prompts/composer.py`: event-map draft, director, and dossier-scoped scene request composition.
- `backend/src/audio_memory/analysis/director.py`: selection validation/normalization, deterministic merging, Event matching, and stable supplemental anchors.
- `backend/src/audio_memory/analysis/dossiers.py`: bounded adjacent-context expansion and immutable scene evidence scopes.
- `backend/src/audio_memory/analysis/provider.py`, `analysis/parser.py`, `analysis/events.py`: strict director response parsing with the existing single-repair boundary.
- `backend/src/audio_memory/prompts/evidence.py`: dossier-scoped event/evidence authorization while retaining identity and media guards.
- `backend/src/audio_memory/analysis/runner.py`: serialized director stage, atomic context checkpoint, dossier routing, empty-scene diagnostics, and publication.
- `backend/src/audio_memory/reanalysis/preview.py`, `reanalysis/worker.py`: compatibility hashes, call estimates, and no reuse of pre-director checkpoints.
- `backend/src/audio_memory/prompts/defaults/meeting.md`, `prompts/store.py`, `prompts/common-scene.md`: detailed meeting/work-communication extraction and packaged-default upgrade.
- Focused unit tests under `backend/tests/unit/analysis` and `backend/tests/unit/prompts`; orchestration/recovery coverage under `backend/tests/integration`; Prompt release cases under `backend/tests/e2e`.

### Task 1: Separate the model Event Map draft and build stable full-transcript clusters

**Files:**
- Modify: `backend/src/audio_memory/prompts/event_schema.py`
- Modify: `backend/src/audio_memory/analysis/parser.py`
- Modify: `backend/src/audio_memory/analysis/windows.py`
- Create: `backend/src/audio_memory/analysis/clusters.py`
- Modify: `backend/tests/unit/prompts/test_event_schema.py`
- Modify: `backend/tests/unit/analysis/test_windows.py`
- Create: `backend/tests/unit/analysis/test_clusters.py`

**Interfaces:**
- Produces: `EventMapDraft(user_speaker: UserSpeaker, events: list[Event])`; its JSON Schema has no `unassigned_segment_ids`.
- Changes: `parse_event_map_output(raw: str) -> EventMapDraft` and `complete_window_event_map(window: AnalysisWindow, generated: EventMapDraft) -> EventMap`.
- Produces: `TranscriptCluster(cluster_id: str, file_id: str, file_name: str, start_ms: int, end_ms: int, segments: tuple[dict[str, object], ...])`.
- Produces: `build_transcript_clusters(transcript: list[dict[str, object]]) -> list[TranscriptCluster]`.
- Produces: `event_hints_for_cluster(cluster, event_map, segment_lookup) -> list[dict[str, object]]`.

- [ ] **Step 1: Write the failing EventMapDraft tests**

Assert `EventMapDraft.model_json_schema()` has no `unassigned_segment_ids`, rejects that field when returned by a model, and `EventMap` still reads legacy JSON with or without the compatibility field. Assert `parse_event_map_output` returns `EventMapDraft` and window completion computes the full compatibility complement.

```python
def test_model_event_map_contract_omits_server_owned_unassigned_ids() -> None:
    schema = EventMapDraft.model_json_schema()
    assert "unassigned_segment_ids" not in schema["properties"]
    with pytest.raises(ValidationError):
        EventMapDraft.model_validate({**draft_payload(), "unassigned_segment_ids": []})
```

- [ ] **Step 2: Verify EventMapDraft RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_event_schema.py tests/unit/analysis/test_windows.py` from `backend`.

Expected: import/contract failures because `EventMapDraft` does not exist and parsing still returns `EventMap`.

- [ ] **Step 3: Implement the response/persistence boundary and verify GREEN**

Add `EventMapDraft`, change the parser/provider-facing annotations, and make `complete_window_event_map` validate the draft before building a persisted `EventMap` with a server-computed complement. Retain all graph, evidence uniqueness, parent-bound, and legacy validation on `EventMap`. Re-run Step 2 and expect PASS.

- [ ] **Step 4: Write the failing cluster tests**

Using literal synthetic segments, assert all reliable segments—including IDs present in `event_map.unassigned_segment_ids`—appear exactly once; boundaries equal the frozen window rules; cluster IDs are unchanged by input order; adding unrelated metadata/reliability fields does not change IDs; changing file/time/segment identity does change IDs; hints include overlapping or evidence-linked Events but never the compatibility unassigned list.

```python
clusters = build_transcript_clusters([late_segment, early_segment])
assert [item["segment_id"] for item in clusters[0].segments] == ["seg_0_0", "seg_0_1"]
assert clusters[0].cluster_id == build_transcript_clusters([early_segment, late_segment])[0].cluster_id
```

- [ ] **Step 5: Verify cluster RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/analysis/test_clusters.py`.

Expected: collection fails because `audio_memory.analysis.clusters` does not exist.

- [ ] **Step 6: Implement stable clusters and hints**

Reuse `build_analysis_windows` for validation/order/bounds. Derive `cluster_id` as `cluster_` plus the first 20 hexadecimal characters of SHA-256 over canonical JSON containing `file_id`, `start_ms`, `end_ms`, and the ordered segment IDs. Build Event hints from Event evidence intersection first, then same-file time overlap inferred from each Event's evidence segments. Hint fields are only `event_id`, `event_type`, `title`, `factual_summary`, `start_ms`, `end_ms`, and `candidate_scenes`.

- [ ] **Step 7: Verify GREEN and commit**

Run the three focused files and commit:

```bash
git add backend/src/audio_memory/prompts/event_schema.py backend/src/audio_memory/analysis/parser.py backend/src/audio_memory/analysis/windows.py backend/src/audio_memory/analysis/clusters.py backend/tests/unit/prompts/test_event_schema.py backend/tests/unit/analysis/test_windows.py backend/tests/unit/analysis/test_clusters.py
git commit -m "feat: build stable full transcript clusters"
```

### Task 2: Add the fixed shared director contract and request path

**Files:**
- Create: `backend/src/audio_memory/prompts/director_schema.py`
- Create: `backend/src/audio_memory/prompts/director.md`
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Modify: `backend/src/audio_memory/analysis/parser.py`
- Modify: `backend/src/audio_memory/analysis/provider.py`
- Modify: `backend/src/audio_memory/analysis/events.py`
- Modify: `backend/tests/unit/prompts/test_director_schema.py`
- Modify: `backend/tests/unit/prompts/test_composer.py`
- Modify: `backend/tests/unit/analysis/test_provider.py`

**Interfaces:**
- Produces: `DirectorSelection` and `DirectorResult(selections: list[DirectorSelection])` with strict extra-field rejection, allowed scenes/value signals/priorities, unique non-empty IDs, and context counts limited to `0..1`.
- Produces: `PromptComposer.compose_director(*, cluster, event_hints, schema) -> ModelRequest` using `scene_id=f"director:{cluster.cluster_id}"`, `16_384` tokens, `120s`, fixed `director.md`, and no editable scene layer or profile input.
- Produces: `RemoteSceneAnalyzer.analyze_director(request, provider_snapshot) -> DirectorResult` using the existing one-repair function.
- Changes: `PromptComposer.fixed_rules_hash()` includes `director.md`.

- [ ] **Step 1: Write failing strict-Schema tests**

Assert legal multi-scene selection validates; illegal scene/value signal/priority, duplicate IDs, empty cluster IDs, unknown fields, and context value `2` fail. Assert `selection_id` is unique within one response.

- [ ] **Step 2: Write failing composition tests**

Compose one request and assert the untrusted packet contains the complete cluster segments (`segment_id`, `start_ms`, `end_ms`, `speaker_id`, `text`), cluster/file bounds, Event hints, no profile or `unassigned_segment_ids`, and the full approved Appendix A rules in the fixed layer. Assert the request's `segment_count` is the cluster size and editable Prompt markup is empty.

- [ ] **Step 3: Verify RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_director_schema.py tests/unit/prompts/test_composer.py tests/unit/analysis/test_provider.py`.

Expected: missing director modules/methods.

- [ ] **Step 4: Implement Schema, exact Prompt, composer, parser, and provider path**

Copy Appendix A without semantic edits into `director.md`. Add `MODEL_REQUEST_POLICIES["director"] = ModelRequestPolicy(16_384, 120)`. Add `parse_director_output`, call it through `request_with_one_repair`, and use normalized invalid code `director_schema_invalid`. Keep provider diagnostics content-free.

- [ ] **Step 5: Verify GREEN and commit**

Run Step 3 and commit:

```bash
git add backend/src/audio_memory/prompts/director_schema.py backend/src/audio_memory/prompts/director.md backend/src/audio_memory/prompts/composer.py backend/src/audio_memory/analysis/parser.py backend/src/audio_memory/analysis/provider.py backend/src/audio_memory/analysis/events.py backend/tests/unit/prompts/test_director_schema.py backend/tests/unit/prompts/test_composer.py backend/tests/unit/analysis/test_provider.py
git commit -m "feat: add shared scene director requests"
```

### Task 3: Normalize director selections and create stable supplemental Event anchors

**Files:**
- Create: `backend/src/audio_memory/analysis/director.py`
- Create: `backend/tests/unit/analysis/test_director.py`

**Interfaces:**
- Produces: `DirectorSelectionError(ValueError)` with normalized `code` and no transcript text.
- Produces: `normalize_director_results(*, clusters, event_map, results) -> list[DirectorSelection]`.
- Produces: `attach_event_anchors(*, selections, clusters, event_map, segment_lookup) -> tuple[EventMap, list[AnchoredSelection]]`.
- Produces: `AnchoredSelection(selection: DirectorSelection, primary_event_id: str, source_event_ids: tuple[str, ...])`.

- [ ] **Step 1: Write failing selection-validation tests**

Assert unknown cluster IDs, unknown Event IDs, non-contiguous clusters, cross-file cluster combinations, duplicate batch coverage, and illegal model-local selection reuse are rejected. Assert deterministic merging de-duplicates `cluster_ids + candidate_scenes`, produces server-stable `selection_<hash>` IDs, and orders selections by file/cluster time then priority.

- [ ] **Step 2: Write failing Event matching/anchor tests**

Assert an evidence-linked Event becomes primary; absent model `source_event_ids` is filled from evidence/time overlap; a completely missed scene creates `event_context_<stable_suffix>`; the ID is stable across identical reruns; the anchor assigns only the first stable unassigned core segment; existing assigned segments are never duplicated; recomputed compatibility coverage remains exact. Assert unknown/cross-file inputs fail.

- [ ] **Step 3: Verify RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/analysis/test_director.py` and expect missing module/functions.

- [ ] **Step 4: Implement normalization and anchors**

Index clusters and Events, infer every Event's file set from its evidence, validate every model reference, canonicalize order, and replace model-local IDs with stable hashes. For supplemental anchors choose Event type by candidate scene (`commitment`, `meeting`, `parenting`, `media`, otherwise `discussion`), use director title/reason without inventing facts, bind one stable unassigned segment, and recompute `unassigned_segment_ids = known_ids - assigned_ids` through a new `EventMap` validation.

- [ ] **Step 5: Verify GREEN and commit**

```bash
git add backend/src/audio_memory/analysis/director.py backend/tests/unit/analysis/test_director.py
git commit -m "feat: anchor director selections to stable events"
```

### Task 4: Build bounded SceneDossiers and route scene request text through them

**Files:**
- Create: `backend/src/audio_memory/analysis/dossiers.py`
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Create: `backend/tests/unit/analysis/test_dossiers.py`
- Modify: `backend/tests/unit/prompts/test_composer.py`

**Interfaces:**
- Produces: strict immutable `SceneDossier(dossier_id, primary_event_id, source_event_ids, candidate_scenes, selected_cluster_ids, expanded_cluster_ids, allowed_segment_ids, file_ids, start_ms, end_ms, title, selection_reason, priority)`.
- Produces: `build_scene_dossiers(*, selections, clusters) -> list[SceneDossier]`.
- Produces: `dossiers_for_scene(dossiers, scene_id) -> list[SceneDossier]`.
- Changes: `PromptComposer.compose_scene(..., dossiers: list[SceneDossier])` sends dossier metadata and every allowed segment grouped by dossier, and excludes Event compatibility-unassigned data.

- [ ] **Step 1: Write failing dossier expansion tests**

Cover same-file direct neighbors, requested 0/1 expansion, no cross-file expansion, stable dossier IDs, selected/expanded ordering, and exact allowed-segment union. Test the `30min` and `600` caps independently; when both sides are requested and both do not fit, retain the closer neighbor and trim the farthest. Reject an invalid selected core that alone exceeds either cap instead of trimming it.

- [ ] **Step 2: Verify dossier RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/analysis/test_dossiers.py` and expect missing module.

- [ ] **Step 3: Implement dossiers and verify GREEN**

Use cluster order as the adjacency source, validate all IDs/times/files, sort optional neighbors by distance from the selected core, and derive `dossier_<hash>` from canonical selected/expanded IDs plus primary/source Event IDs and candidate scenes. Re-run the dossier tests.

- [ ] **Step 4: Write failing dossier-scene composition tests**

Assert the same dossier can enter meeting and todo requests, a non-routed dossier is absent, Event-unassigned but dossier-allowed segments appear with text/speaker/time, each segment appears once per dossier, `segment_count` is the unique routed segment count, and no dossiers raises before composing a model request.

- [ ] **Step 5: Verify composer RED, implement, and verify GREEN**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_composer.py`; update `_scene_transcript` to `_scene_dossiers`; include a compact Event metadata packet without compatibility unassigned IDs. Re-run both focused files.

- [ ] **Step 6: Commit**

```bash
git add backend/src/audio_memory/analysis/dossiers.py backend/src/audio_memory/prompts/composer.py backend/tests/unit/analysis/test_dossiers.py backend/tests/unit/prompts/test_composer.py
git commit -m "feat: expand bounded scene dossiers"
```

### Task 5: Validate every scene result against dossier evidence

**Files:**
- Modify: `backend/src/audio_memory/prompts/evidence.py`
- Modify: `backend/tests/unit/prompts/test_evidence_integrity.py`

**Interfaces:**
- Changes: `validate_evidence_integrity(result, event_map, segment_ids, *, dossiers: list[SceneDossier] | None = None, segment_lookup: dict[str, dict[str, object]] | None = None) -> None`.
- Produces: when dossiers are supplied, each Event/evidence reference must fit at least one dossier whose `primary_event_id` or `source_event_ids` authorizes the Event and whose `allowed_segment_ids`, file, and time range contain every evidence segment.
- Preserves: legacy Event-scoped validation only for callers that explicitly omit dossiers; production runner always supplies dossiers.

- [ ] **Step 1: Write failing dossier-evidence tests**

Assert a segment omitted by the primary Event but included by its dossier succeeds. Assert unknown, dossier-outside, wrong-Event, cross-file, and dossier-time-outside evidence fails. Cover single-event evidence, multi-event growth evidence, todo evidence, participant evidence, and reliable user-speaker evidence. Retain current tests for identity, media-as-user-todo, duplicate/empty evidence, and nested basis IDs.

- [ ] **Step 2: Verify RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_evidence_integrity.py`.

Expected: new keyword arguments/signature are unavailable or Event membership rejects dossier-allowed unassigned evidence.

- [ ] **Step 3: Implement a single dossier authorization helper**

Add `_require_dossier_scope(event_ids, evidence_ids, dossiers, segment_lookup)` that requires one matching dossier for each atomic evidence statement, verifies every ID exists, then checks allowed membership, file inclusion, and `start_ms/end_ms` containment. Thread this helper through `_validate_todo`, `_validate_event_evidence`, and `_validate_multi_event_evidence`; do not weaken semantic identity or Event-type guards.

- [ ] **Step 4: Verify GREEN and commit**

```bash
git add backend/src/audio_memory/prompts/evidence.py backend/tests/unit/prompts/test_evidence_integrity.py
git commit -m "feat: validate scene evidence against dossiers"
```

### Task 6: Orchestrate, checkpoint, and resume director/dossier analysis

**Files:**
- Modify: `backend/src/audio_memory/analysis/runner.py`
- Modify: `backend/src/audio_memory/analysis/provider.py`
- Modify: `backend/src/audio_memory/reanalysis/preview.py`
- Modify: `backend/src/audio_memory/reanalysis/worker.py`
- Modify: `backend/tests/integration/test_event_map_pipeline.py`
- Modify: `backend/tests/integration/test_reanalysis_worker.py`
- Modify: `backend/tests/unit/reanalysis/test_preview.py`

**Interfaces:**
- Extends `StrictAnalysisProvider` with `analyze_director`.
- Produces: runner `_scene_context(...) -> tuple[EventMap, list[SceneDossier]]`, one serialized director call per cluster.
- Checkpoints: reserved `staged_results_json["_scene_context"]` containing normalized anchored selections and dossiers, atomically with the final Event Map containing supplemental anchors.
- Scene behavior: only routed dossiers call DeepSeek; missing routes create a strict empty result with `generation_reason="no_selected_dossier"` and no provider call.
- Compatibility: fixed hashes include `director.md`, `EventMapDraft`, `DirectorResult`, cluster/dossier parameter fingerprint, and all scene Schemas; pre-director Event maps are not reused for a new snapshot.

- [ ] **Step 1: Write failing pipeline coverage/routing tests**

Seed transcript segments where only one is Event-assigned and another is unassigned. Assert director requests collectively contain every reliable segment exactly once. Return one meeting+todo selection and assert only meeting/todo scene requests execute; the other four strict results are staged empty with `no_selected_dossier`. Assert both requests include the dossier segment omitted by Event Map and publication receives six results.

- [ ] **Step 2: Write failing failure/checkpoint/resume tests**

Assert unknown cluster/Event or a director provider failure publishes nothing and keeps specific normalized errors. Assert context checkpoint and anchored Event Map update in one database transaction. On a retry with `_scene_context` present, assert zero director calls and resume only missing scene calls. Assert a failure before context persistence leaves neither anchors nor context staged.

- [ ] **Step 3: Write failing compatibility/preview tests**

Assert fixed hashes and preview token change when `director.md`, Director Schema, or dossier parameters change. Assert analysis preview estimates include event-window calls plus director-cluster calls and up to six routed scenes/profile per source, while `whisper_calls == 0` and `diarization_calls == 0`. Assert worker rejects/rebuilds a checkpoint from the older fixed-rule hash.

- [ ] **Step 4: Verify RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/integration/test_event_map_pipeline.py tests/integration/test_reanalysis_worker.py tests/unit/reanalysis/test_preview.py`.

Expected: runner has no director stage, all six scenes use Event evidence, and hashes/estimates omit the new stage.

- [ ] **Step 5: Implement serialized director orchestration**

After `_event_map`, build clusters and either restore `_scene_context` or compose/call/validate one director request per cluster with ownership and credential fences before/after every provider call. Normalize all selections, attach stable anchors, build dossiers, then atomically update `event_map_json`, `event_map_hash`, and `staged_results_json`. Log only cluster/selection/dossier/coverage counts.

- [ ] **Step 6: Implement routed scene execution and evidence validation**

For each fixed scene, filter dossiers. If empty, create the scene's strict empty result locally. Otherwise compose the dossier request, run the existing provider path, and validate using the scene dossiers plus full segment lookup before staging. Do the same validation again for resumed staged results. Quality gating, profile gating, generation fencing, and atomic publisher behavior remain in their current order.

- [ ] **Step 7: Implement hash/preview/worker compatibility**

Increment `PromptComposer.SCHEMA_VERSION`, include new fixed artifacts and a canonical parameter object in `current_fixed_rule_hashes`, and update preview min/max logical calls without claiming exact HTTP attempts. Validate persisted Event Maps as before but require fixed-rule metadata equality so old checkpoints are not reused.

- [ ] **Step 8: Verify GREEN and commit**

Run Step 4 plus `tests/integration/test_analysis_pipeline.py`, then commit:

```bash
git add backend/src/audio_memory/analysis/runner.py backend/src/audio_memory/analysis/provider.py backend/src/audio_memory/reanalysis/preview.py backend/src/audio_memory/reanalysis/worker.py backend/tests/integration/test_event_map_pipeline.py backend/tests/integration/test_reanalysis_worker.py backend/tests/unit/reanalysis/test_preview.py
git commit -m "feat: route analysis through scene director dossiers"
```

### Task 7: Enhance the packaged meeting Prompt and preserve conservative attribution

**Files:**
- Modify: `backend/src/audio_memory/prompts/defaults/meeting.md`
- Modify: `backend/src/audio_memory/prompts/common-scene.md`
- Modify: `backend/src/audio_memory/prompts/store.py`
- Modify: `backend/tests/unit/prompts/test_store.py`
- Modify: `backend/tests/e2e/test_prompt_eval_contract.py`
- Modify: `backend/tests/integration/test_event_map_pipeline.py`

**Interfaces:**
- Meeting Prompt reconstructs background/roles, topics/positions, confirmed facts, explicit conclusions/decisions, open questions/disagreements/dependencies, confirmed and pending actions, and a useful review summary using the existing `MeetingSceneResult` fields.
- `PACKAGED_DEFAULT_VERSION` advances to `3`; the exact prior packaged meeting hash is recognized as legacy and upgraded once, while user-edited meeting Prompts remain byte-for-byte unchanged.
- Unknown identity uses evidence-supported roles, never “you”; nested uncertain action owner stays `unknown`; no explicit decision means `decisions=[]`; media interviews are not现场 meetings.

- [ ] **Step 1: Write failing packaged-upgrade tests**

Assert a new store records default version 3, the previous packaged meeting content upgrades once and archives once, repeated reads are idempotent, and custom content/version remains unchanged. Derive the legacy hash from a literal copy fixture or the known hash value, not the new Prompt implementation.

- [ ] **Step 2: Write failing Prompt behavior contracts**

Add privacy-safe synthetic cases for: high-information informal work communication; media interview exclusion; no invented decision; unknown-role wording with nested `owner_type="unknown"`; adjacent dossier context supporting an open question and conclusion. Assert the final strict result and evidence validator behavior rather than grepping exact prose.

- [ ] **Step 3: Verify RED**

Run `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_store.py tests/e2e/test_prompt_eval_contract.py tests/integration/test_event_map_pipeline.py`.

Expected: packaged version/upgrade and new release-gate fixtures fail.

- [ ] **Step 4: Implement the approved meeting enhancement**

Expand `meeting.md` and only the scene-wide safety rules needed for dossier input. Map “各方立场” and “确认事实” into existing `discussion_topics`/`core_conclusions`; keep `decisions` empty without confirmation; use `open_questions` for unresolved issues/disagreement/dependencies; retain explicit or pending actions in `meeting_todos`; keep top-level todos subject to reliable user identity. Add the old packaged meeting hash, bump the packaged version, and preserve user edits.

- [ ] **Step 5: Verify GREEN and commit**

```bash
git add backend/src/audio_memory/prompts/defaults/meeting.md backend/src/audio_memory/prompts/common-scene.md backend/src/audio_memory/prompts/store.py backend/tests/unit/prompts/test_store.py backend/tests/e2e/test_prompt_eval_contract.py backend/tests/integration/test_event_map_pipeline.py
git commit -m "feat: enrich meeting work communication analysis"
```

### Task 8: Full verification, real 3,442-segment reanalysis, and page acceptance

**Files:**
- Create: `docs/benchmark-evidence/2026-08-10-scene-director-meeting-context.md`
- Modify production/test files only when a fresh failing test reproduces a verification defect.

**Interfaces:**
- Verifies: all automated suites/builds, privacy constraints, analysis-only real run, director/dossier aggregate coverage, card evidence playback, and live history page.

- [ ] **Step 1: Run focused backend verification**

From `backend`:

```bash
env PYTHONPATH=src .venv/bin/pytest -q tests/unit/analysis/test_clusters.py tests/unit/analysis/test_director.py tests/unit/analysis/test_dossiers.py tests/unit/prompts/test_director_schema.py tests/unit/prompts/test_event_schema.py tests/unit/prompts/test_composer.py tests/unit/prompts/test_evidence_integrity.py tests/unit/prompts/test_store.py tests/integration/test_event_map_pipeline.py tests/integration/test_analysis_pipeline.py tests/integration/test_reanalysis_worker.py tests/unit/reanalysis/test_preview.py tests/e2e/test_prompt_eval_contract.py
```

- [ ] **Step 2: Run full backend and frontend regression**

Run `env PYTHONPATH=src .venv/bin/pytest -q` from `backend`; then from `prototype` run `node --test tests/*.test.mjs`, `npm run build`, and `npx playwright test tests/e2e/recovery.spec.js`. If the loopback test is sandbox-blocked, rerun only that test with local-port permission and record both outcomes accurately.

- [ ] **Step 3: Run privacy and change-scope checks**

Inspect `git diff --check`, `git status --short`, and the diff. Confirm `backend/.venv`, `prototype/node_modules`, and `docs/prompt-editing/2026-08-10-deepseek-current-prompts.md` remain untracked/unstaged. Confirm no transcript text, audio paths, complete requests/responses, keys, or personal screenshots enter Git.

- [ ] **Step 4: Verify the correct local service before real work**

Check the listener, process working directory, service log, and `http://127.0.0.1:8765/history`. Stop only a verified stale process, then start the backend from this worktree. Do not use a service launched from the original checkout.

- [ ] **Step 5: Create one analysis-only version for the frozen source**

Use the existing reanalysis API/UI for source job `d29475e4-f148-4b99-9b7e-1e5751da1e48`. Confirm preview reports zero Whisper/diarization calls, then submit one new analysis version. Preserve old versions and do not invoke any transcription stage.

- [ ] **Step 6: Record aggregate acceptance evidence**

Record source job/version IDs, model, fixed hashes, 3,442 reliable segment count, cluster count, director calls, selection/dossier counts, director unique segment coverage, Event assigned/unassigned counts, routed scene counts, evidence validation failures, durations/tokens when available, cards/todos, and normalized errors. Do not record source paths, transcript text, model bodies, or personal screenshots.

- [ ] **Step 7: Inspect the history page and evidence playback**

Open `/history`, inspect meeting cards for full discussion context, background/roles, conclusions, open questions, decisions only when explicit, and actions with conservative ownership. Exercise at least one evidence playback link. Confirm recruitment/career/org/product/business conversations are recoverable and media interviews are not mislabeled as live meetings.

- [ ] **Step 8: Commit evidence and hand off for user acceptance**

```bash
git add docs/benchmark-evidence/2026-08-10-scene-director-meeting-context.md
git commit -m "docs: record scene director meeting acceptance"
```

Leave the verified history page open for user review. Do not start web verification, the other five scene Prompt enhancements, or Compact until the user accepts meeting quality.

---

## Self-Review Results

- Spec coverage: all requirements in sections 5–13 are assigned to Tasks 1–8; non-goals in section 14 are retained as global constraints.
- Placeholder scan: no `TBD`, implementation `TODO`, “similar to Task N”, or unspecified error/test steps remain.
- Type consistency: `EventMapDraft` is provider-facing only; `EventMap` is persisted; `DirectorResult` becomes normalized `AnchoredSelection`; `AnchoredSelection` becomes `SceneDossier`; production evidence validation always receives the routed dossier list and full segment lookup.
- Resume consistency: supplemental anchors and `_scene_context` are committed together; fixed hashes prevent old Event-only checkpoints from being reused with dossier evidence rules.
