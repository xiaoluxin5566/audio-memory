# Autonomous Analysis Context Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Make the normal upload and reanalysis pipeline use the approved advisor prompt and choose context handling by transcript size: complete source transcript for normal recordings; evidence-anchored notes and exact-source retrieval only for transcripts that cannot fit safely in one final analysis request.

**Architecture:** Keep local Whisper transcription independent. Add a deterministic local context planner that measures the actual serialized transcript payload. The direct route passes the whole transcript to DeepSeek V4 Pro. The long route partitions consecutive source segments without summarizing, asks DeepSeek for per-window high-fidelity notes, asks DeepSeek to cluster those notes and request exact segment IDs, then runs the final card prompt against the selected original segments. Each durable model output is staged so retries resume without rerunning prior work.

**Non-goal:** Audio Compact transcription is deliberately not part of this plan. Its implementation is tracked in `docs/superpowers/plans/2026-08-10-local-fast-v0-1-transcription.md` and begins only after this page-quality checkpoint is accepted.

## Task 1: Deterministic context-routing policy

**Files:**
- Create `backend/src/audio_memory/analysis/autonomous_context.py`
- Create `backend/tests/unit/analysis/test_autonomous_context.py`

**Interfaces:**
- `plan_autonomous_context(transcript) -> DirectContext | LongContextPlan`
- Direct route when the compact serialized transcript is within the fixed safe direct-input budget; no windowing occurs.
- Long route produces consecutive, non-overlapping windows which preserve whole source segments and use 12,000 Chinese-character target / 16,000-character hard maximum except for one oversize source segment.

- [x] Write boundary tests proving a 28,470-character transcript remains direct, deterministic exact-boundary routing, no segment split, and every long-plan ID appears once.
- [x] Verify RED, implement pure routing and window construction, then verify GREEN.

## Task 2: First-class production prompt and request composition

**Files:**
- Modify `docs/superpowers/specs/2026-08-11-autonomous-analysis-prompts.md`
- Modify `backend/src/audio_memory/prompts/composer.py`
- Modify `backend/tests/unit/prompts/test_composer.py`

**Interfaces:**
- The normal autonomous request uses the approved direct-advisor / three-stage / semantic-rendering prompt, not an export file or a manual-only overlay.
- Add typed composer methods for `autonomous-notes`, `autonomous-retrieval-plan`, and `autonomous-final` requests.
- All three request types use DeepSeek V4 Pro’s existing provider snapshot and explicit schemas.

- [x] Add regression tests that assert the production request includes direct second-person advisor voice, the three phases, card-boundary rule, and does not contain the legacy third-person wording.
- [x] Verify RED, update source prompt/version/fingerprint/request policies, then verify GREEN.

## Task 3: Notes and retrieval schemas, parsers, and provider methods

**Files:**
- Modify `backend/src/audio_memory/prompts/autonomous_schema.py`
- Modify `backend/src/audio_memory/analysis/parser.py`
- Modify `backend/src/audio_memory/analysis/provider.py`
- Modify `backend/tests/unit/prompts/test_autonomous_schema.py`
- Modify `backend/tests/unit/analysis/test_provider.py`

**Interfaces:**
- `InformationNotebook` contains bounded high-fidelity notes anchored only to window segment IDs.
- `AutonomousRetrievalPlan` contains independently valuable card plans plus required original `segment_id`s.
- Provider validates each response using the same single schema-repair discipline as final cards.

- [x] Write strict schema tests for duplicate/unknown IDs and raw JSON parsing; write mock-provider repair tests for each new request type.
- [x] Verify RED, add models/parsers/provider methods, then verify GREEN.

## Task 4: Long-transcript orchestration and resumability

**Files:**
- Modify `backend/src/audio_memory/analysis/runner.py`
- Modify `backend/tests/integration/test_analysis_pipeline.py`
- Modify `backend/tests/integration/test_reanalysis_worker.py`

**Interfaces:**
- Direct route retains one final model request and stores `staged_results_json["autonomous"]`.
- Long route persists independently completed note windows, retrieval plan, and final cards in `staged_results_json`; an interrupted run resumes at the first unfinished stage.
- Exact-source final input contains only IDs selected by the plan, plus supporting notes; requested IDs must be known, bounded, and preserved in source order.
- Final card evidence is checked against full original transcript; quotations are additionally checked against the retrieved source.

- [x] Add fake-provider integration tests for direct path request count, long-path notes → plan → source retrieval → final order, safe staged resume, invalid retrieval IDs, and final quote integrity.
- [x] Verify RED, implement routing/orchestration/staging and GREEN.

## Task 5: Hidden profile behavior on routed analysis

**Files:**
- Modify `backend/src/audio_memory/analysis/runner.py`
- Modify `backend/src/audio_memory/analysis/provider.py`
- Modify `backend/tests/integration/test_analysis_pipeline.py`

**Interfaces:**
- Direct runs retain the present full-transcript profile extraction.
- Long runs extract profile candidates from final cards plus the exact retrieved source, never from an unbounded transcript request.
- Existing profile privacy and ownership checks remain unchanged.

- [x] Add tests for profile input projection on both paths and for preservation of existing evidence validation.
- [x] Verify RED, implement routing-aware extraction and GREEN.

## Task 6: Acceptance regression suite and documentation

**Files:**
- Modify `docs/superpowers/specs/2026-08-11-autonomous-analysis-prompts.md`
- Modify relevant backend/prototype tests only as needed

- [x] Run focused unit and integration suites, then full backend test suite.
- [x] Re-run the representative two-card recording through the normal product chain (not importer) and inspect that it produces separate work-system and interview-review cards in advisor voice.
- [x] Document the fixed direct threshold and long-path behaviour in the product spec; do not start audio Compact until the user accepts this page-quality checkpoint.
