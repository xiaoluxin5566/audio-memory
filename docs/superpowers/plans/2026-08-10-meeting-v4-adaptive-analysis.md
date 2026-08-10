# Meeting V4 Adaptive Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed Meeting V3 detail with a model-directed adaptive analysis contract and page, then reanalyze the existing 3,442 reliable segments using Meeting Prompt V4.

**Architecture:** Preserve the existing single routed Meeting request and atomic publisher. Expand only the Meeting scene's strict response types into evidence-bearing facts, quote analyses, arguments, recommendations, adaptive sections, and uncertainties; validate each atomic item against routed dossiers; render those fields directly. The model owns card boundaries and content richness through Prompt V4—there is no planner or semantic post-processing stage.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, SQLAlchemy, React, Vite, Node test runner, Playwright.

## Global Constraints

- Reuse the existing worktree and branch `codex/local-fast-v0-1`.
- Do not touch untracked `backend/.venv`, `prototype/node_modules`, or `docs/prompt-editing/`.
- Do not retranscribe audio or call diarization.
- Do not add a model stage, planner, semantic deduper, page, navigation item, scene, or card type.
- Only Meeting Schema/Prompt/UI may change; preserve all other scene contracts.
- Every production change follows red-green-refactor and ends in a focused commit.
- Never write source paths, transcript text, model bodies, credentials, or personal screenshots to the repository.

---

### Task 1: Define the adaptive Meeting response contract

**Files:**
- Modify: `backend/src/audio_memory/prompts/schemas.py`
- Modify: `backend/tests/unit/prompts/test_scene_schemas.py`

**Interfaces:**
- Produces: `MeetingEvidenceItem`, `MeetingQuoteAnalysis`, `MeetingArgument`, `MeetingRecommendation`, `MeetingAdaptiveSection`, `MeetingUncertainty`, revised `MeetingDetail`, and multi-Event `MeetingCard`.
- Preserves: `MeetingSceneResult` outer fields and `model_dump_for_frontend()`.

- [ ] **Step 1: Write failing Schema tests**

Add tests that construct one Meeting card with two `event_ids`, independent quote/argument/recommendation/section fields, and matching detail Events. Assert frontend output retains public analytical content and evidence IDs remain excluded from the public payload. Add failures for mismatched card/detail Events, empty evidence, duplicate Events, unknown fields, and generic titles.

- [ ] **Step 2: Run the focused Schema tests and confirm RED**

Run: `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_scene_schemas.py -k meeting`

Expected: imports or validation fail because the adaptive Meeting types do not exist and `MeetingCard` still permits one Event only.

- [ ] **Step 3: Implement the strict adaptive types and Meeting frontend allowlist**

Use strict Pydantic models. Every analytical object contains non-empty `event_ids` and `evidence_segment_ids`; quote text and analysis are length-bounded. `MeetingDetail.event_ids` must equal `MeetingCard.event_ids`. Keep field order aligned with the editorial page. Extend `_FRONTEND_MEETING_CARD_FIELDS` with public text fields while excluding internal Event and evidence IDs.

- [ ] **Step 4: Run the focused Schema tests and confirm GREEN**

Run: `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_scene_schemas.py -k meeting`

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/prompts/schemas.py backend/tests/unit/prompts/test_scene_schemas.py
git commit -m "feat: add adaptive meeting analysis schema"
```

### Task 2: Validate adaptive evidence against routed dossiers

**Files:**
- Modify: `backend/src/audio_memory/prompts/evidence.py`
- Modify: `backend/tests/unit/prompts/test_evidence_integrity.py`

**Interfaces:**
- Consumes: adaptive Meeting evidence objects from Task 1.
- Produces: dossier-scoped validation for every fact, quote, argument, recommendation, section, uncertainty, and participant.

- [ ] **Step 1: Write failing evidence tests**

Add a two-Event Meeting card whose quote belongs to Event A and recommendation belongs to Event B; both succeed inside routed dossiers. Add failures for an item whose Events and segments do not fit one dossier, a verbatim quote using an unknown segment, a wrong Event, cross-file evidence, and out-of-range evidence. Retain the unknown-owner interview exception and media guards.

- [ ] **Step 2: Run the focused evidence tests and confirm RED**

Run: `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_evidence_integrity.py -k meeting`

Expected: validator accesses removed fixed fields or does not validate new analytical fields.

- [ ] **Step 3: Implement one shared Meeting evidence-item validator**

For every analytical item call `_validate_multi_event_evidence(item.event_ids, item.evidence_segment_ids, events, segment_ids, scope)`. Validate participants the same way. Keep top-level todo identity logic unchanged. Do not score, merge, split, or rewrite content.

- [ ] **Step 4: Run evidence and runner integration tests**

Run: `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_evidence_integrity.py tests/integration/test_analysis_pipeline.py tests/integration/test_reanalysis_worker.py`

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/prompts/evidence.py backend/tests/unit/prompts/test_evidence_integrity.py
git commit -m "feat: validate adaptive meeting evidence"
```

### Task 3: Install Meeting Prompt V4 and migrate packaged defaults

**Files:**
- Modify: `backend/src/audio_memory/prompts/defaults/meeting.md`
- Modify: `backend/src/audio_memory/prompts/store.py`
- Modify: `backend/tests/unit/prompts/test_store.py`
- Modify: `backend/tests/e2e/test_prompt_eval_contract.py`
- Modify: prompt-evaluation fixture selected by `test_prompt_eval_contract.py`

**Interfaces:**
- Produces: packaged Meeting V4 and one-time V3→V4 migration.
- Preserves: user-edited prompt protection and archive behavior.

- [ ] **Step 1: Write failing store and prompt-contract tests**

Assert an untouched packaged V3 archives once and becomes V4; a user-edited V3 remains unchanged. Add evaluator cases requiring one card for a multi-dossier interview and rejecting redundant overlapping meeting cards, quote-less shallow output, missing argument analysis, and generic unsupported advice.

- [ ] **Step 2: Run tests and confirm RED**

Run: `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_store.py tests/e2e/test_prompt_eval_contract.py`

- [ ] **Step 3: Replace `meeting.md` with the exact user-approved V4 Prompt**

Include global dossier reading, semantic card boundaries, merge/split self-check, representative verbatim quotes, both sides' arguments, fact/inference separation, adaptive sections, specific recommendations, evidence rules, and final quality self-review. Advance the packaged version constant to 4 and add the V3 packaged hash/content migration entry without weakening user-edit detection.

- [ ] **Step 4: Run tests and confirm GREEN**

Run: `env PYTHONPATH=src .venv/bin/pytest -q tests/unit/prompts/test_store.py tests/e2e/test_prompt_eval_contract.py`

- [ ] **Step 5: Commit**

```bash
git add backend/src/audio_memory/prompts/defaults/meeting.md backend/src/audio_memory/prompts/store.py backend/tests/unit/prompts/test_store.py backend/tests/e2e/test_prompt_eval_contract.py backend/tests/fixtures
git commit -m "feat: install adaptive meeting prompt v4"
```

### Task 4: Render adaptive Meeting analysis

**Files:**
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css`
- Modify: `prototype/tests/api-state.test.mjs`
- Modify: `prototype/tests/detail-layout.test.mjs`

**Interfaces:**
- Consumes: public adaptive Meeting payload from Task 1.
- Produces: normalized editorial Meeting detail sections and working evidence playback.

- [ ] **Step 1: Write failing normalization and layout tests**

Use a two-Event Meeting fixture with analysis angle, context, participants, key facts, quote analysis, arguments, one custom section, recommendation with actions/suggested language, and uncertainty. Assert every public field appears, quote text uses a quote panel, arguments and recommendations retain their hierarchy, and old fixed headings do not appear.

- [ ] **Step 2: Run tests and confirm RED**

Run: `node --test tests/api-state.test.mjs tests/detail-layout.test.mjs`

- [ ] **Step 3: Implement the adaptive normalizer and editorial components**

Return typed detail blocks (`lead`, `participants`, `facts`, `quotes`, `arguments`, `analysis`, `recommendations`, `uncertainties`) from `meetingBlocks`. Render each type with semantic markup and the existing restrained palette. Keep responsive stacking, readable long-form typography, and the existing `EvidencePlayback` component. Do not redesign navigation or other scenes.

- [ ] **Step 4: Run frontend tests and production build**

Run: `node --test tests/*.test.mjs && npm run build`

- [ ] **Step 5: Commit**

```bash
git add prototype/src/api/state.js prototype/src/App.jsx prototype/src/styles.css prototype/tests/api-state.test.mjs prototype/tests/detail-layout.test.mjs
git commit -m "feat: render adaptive meeting analysis"
```

### Task 5: Save V4, reanalyze real history, and hand off the webpage

**Files:**
- Create: `docs/benchmark-evidence/2026-08-10-meeting-v4-adaptive-analysis.md`

**Interfaces:**
- Consumes: shipped Schema, evidence rules, prompt, and UI.
- Produces: one newly published real analysis version and privacy-safe acceptance evidence.

- [ ] **Step 1: Run full automated verification**

Run backend: `env PYTHONPATH=src .venv/bin/pytest -q`

Run frontend: `node --test tests/*.test.mjs && npm run build && npm run test:e2e`

- [ ] **Step 2: Restart the service from this worktree**

Use `env PYTHONPATH=src .venv/bin/uvicorn audio_memory.main:app --host 127.0.0.1 --port 8765`. Confirm `/api/history` and `/api/feed` return 200 and the process imports this worktree.

- [ ] **Step 3: Explicitly save the approved active Meeting prompt as V4**

Use the local Prompt API/UI so the active prompt snapshot equals the packaged V4 content and version 4. Do not edit the user prompt directory by shell write.

- [ ] **Step 4: Preview and run history reanalysis**

Confirm one batch, one file, 3,442 reliable segments, zero Whisper/diarization calls, no blockers, and Meeting V4. Start one reanalysis and wait for a terminal state. Failed versions must not publish; diagnose safely and retry only within existing bounded repair policy.

- [ ] **Step 5: Inspect real card boundaries and richness**

Expect one complete interview card instead of the two redundant cards. Inspect friend-chat cards for distinct analysis angles. Verify visible short quote analysis, both sides' arguments, key facts, adaptive sections, targeted recommendations, uncertainty, and one playable evidence link. Record only counts and normalized statuses.

- [ ] **Step 6: Record privacy-safe evidence and commit**

```bash
git add docs/benchmark-evidence/2026-08-10-meeting-v4-adaptive-analysis.md
git commit -m "docs: record meeting v4 acceptance"
```

- [ ] **Step 7: Open the live information-flow page for user acceptance**

Leave `http://127.0.0.1:8765/` open on the new Meeting card detail. Do not proceed to web verification or another scene until the user accepts Meeting V4 quality.

