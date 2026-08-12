# Custom Prompt Result Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the latest batch’s visible analysis with the three validated Event/Insight cards and render them natively in the existing Audio Memory feed and detail UI.

**Architecture:** Add a focused backend importer that validates the external result, maps it into the existing autonomous card envelope, and publishes a new completed analysis version in one transaction. Extend the frontend normalization layer to recognize imported Event/Insight metadata while reusing the current card and detail components.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, Pydantic v2, SQLite, React, Vite, Node test runner.

## Global Constraints

- Only the latest batch’s visible cards are replaced.
- Old analysis versions, original transcripts, audio, and hidden profile rows remain unchanged.
- Import performs no model call and no profile extraction.
- The exported JSON is an import input, not a frontend runtime dependency.
- Import validation is fail-closed and publication is atomic.

---

### Task 1: Validate and Convert Event/Insight Results

**Files:**
- Create: `backend/src/audio_memory/analysis/result_import.py`
- Create: `backend/tests/unit/analysis/test_result_import.py`

**Interfaces:**
- Consumes: parsed JSON dictionary and a mapping of `segment_id -> transcript text`.
- Produces: `convert_external_analysis(payload: dict[str, object], transcript: dict[str, str]) -> AutonomousAnalysisResult`.

- [ ] **Step 1: Write the failing conversion test**

Create a literal fixture containing one Event card and one Insight card. Assert that `findings` become autonomous sections, `analysis` remains a full section, `quotes` remain verbatim, `actions` become recommendations, metadata is preserved in section types, and card evidence is a stable de-duplicated union.

- [ ] **Step 2: Run the conversion test and verify RED**

Run: `cd backend && .venv/bin/pytest tests/unit/analysis/test_result_import.py -q`

Expected: FAIL because `audio_memory.analysis.result_import` does not exist.

- [ ] **Step 3: Implement minimal conversion**

Implement strict local Pydantic input models for `status`, `card_kind`, `scene_types`, `findings`, `analysis`, `quotes`, and `actions`. Convert them into the existing `AutonomousAnalysisResult` without changing their prose.

- [ ] **Step 4: Add failing validation tests**

Add separate tests asserting rejection of:

- non-`complete` status;
- empty cards;
- unknown evidence IDs;
- a quote that is not a continuous substring of one referenced segment.

- [ ] **Step 5: Run validation tests and verify RED**

Run: `cd backend && .venv/bin/pytest tests/unit/analysis/test_result_import.py -q`

Expected: FAIL on each missing validation branch.

- [ ] **Step 6: Implement fail-closed validation and verify GREEN**

Validate all evidence-bearing objects against the transcript map and require every quote to match at least one of its own evidence segments. Run the same test command and expect all tests to pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add backend/src/audio_memory/analysis/result_import.py backend/tests/unit/analysis/test_result_import.py
git commit -m "feat: validate imported autonomous analysis"
```

### Task 2: Atomically Publish an Imported Version

**Files:**
- Modify: `backend/src/audio_memory/analysis/result_import.py`
- Create: `backend/tests/integration/test_result_import.py`
- Create: `scripts/import_analysis_result.py`

**Interfaces:**
- Consumes: database path, result JSON path, and latest published batch.
- Produces: `import_latest_analysis(session_factory, payload) -> str`, returning the new analysis version ID.

- [ ] **Step 1: Write the failing publication integration test**

Build a temporary SQLite database with one completed batch, one old current version, transcript rows, and profile rows. Import one valid card, then assert:

- a new completed analysis version exists;
- new card rows belong to that version;
- `batch.current_analysis_version_id` points to the new version;
- the old version and profile rows are unchanged;
- no todo rows are created.

- [ ] **Step 2: Run the publication test and verify RED**

Run: `cd backend && .venv/bin/pytest tests/integration/test_result_import.py -q`

Expected: FAIL because `import_latest_analysis` does not exist.

- [ ] **Step 3: Implement atomic publication**

Use one SQLAlchemy transaction to load the latest batch and transcript map, call `convert_external_analysis`, create a completed `AnalysisVersion`, insert autonomous cards using the existing card payload/evidence conventions, and finally switch `current_analysis_version_id`.

- [ ] **Step 4: Add a rollback test**

Supply an unknown evidence ID and assert the import raises while the batch still points to the old version and no partial version/card rows remain.

- [ ] **Step 5: Run integration tests and verify GREEN**

Run: `cd backend && .venv/bin/pytest tests/integration/test_result_import.py -q`

Expected: all tests pass.

- [ ] **Step 6: Add and exercise the CLI wrapper**

Implement `scripts/import_analysis_result.py --database PATH --input PATH`. It must print only the new version ID and card count, and must not access provider credentials. Exercise it against a temporary test database in the integration test.

- [ ] **Step 7: Commit Task 2**

```bash
git add backend/src/audio_memory/analysis/result_import.py backend/tests/integration/test_result_import.py scripts/import_analysis_result.py
git commit -m "feat: publish imported analysis version"
```

### Task 3: Render Event/Insight Metadata in the Existing UI

**Files:**
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/tests/api-state.test.mjs`

**Interfaces:**
- Consumes: existing feed payload with autonomous card sections whose types include `external_meta`, `finding:<type>:<confidence>`, and `analysis`.
- Produces: normalized cards with `cardKind`, `sceneTypes`, labels `事件分析` / `深度洞察`, and existing `detailSections`.

- [ ] **Step 1: Write the failing normalization test**

Add a literal feed payload for one Event and one Insight card. Assert the normalized labels, preserved summaries, ordered detail sections, finding confidence metadata, quote block, and action block.

- [ ] **Step 2: Run the frontend test and verify RED**

Run: `cd prototype && npm test -- --run tests/api-state.test.mjs`

Expected: FAIL because imported metadata is not recognized and labels remain `AI 深度分析`.

- [ ] **Step 3: Implement minimal normalization**

Extend `autonomousBlocks` and `normalizeStrictCards` to decode imported metadata and expose `cardKind`, `sceneTypes`, finding types, and confidence without adding a second rendering pipeline.

- [ ] **Step 4: Write the failing rendering test**

Assert that the UI presents Event/Insight labels and that finding sections display fact/inference/pattern and confidence in human-readable Chinese while empty optional sections stay hidden.

- [ ] **Step 5: Run rendering test and verify RED**

Run the focused frontend test command and confirm the failure is caused by absent Event/Insight rendering.

- [ ] **Step 6: Implement UI rendering and verify GREEN**

Reuse `AutonomousDetailSection`; add compact metadata treatment for findings and Event/Insight labels while preserving existing visual language and accessibility hierarchy. Run focused tests until green.

- [ ] **Step 7: Commit Task 3**

```bash
git add prototype/src/api/state.js prototype/src/App.jsx prototype/tests/api-state.test.mjs
git commit -m "feat: render event and insight analysis cards"
```

### Task 4: Import the Real Result and Verify the Product

**Files:**
- Read: `exports/custom-prompt-analysis-v2.json`
- Modify at runtime: `~/Library/Application Support/AudioMemory/audio-memory.sqlite3`

**Interfaces:**
- Consumes: the validated real JSON created from 3,442 transcript segments.
- Produces: the latest batch’s new current analysis version and three visible cards.

- [ ] **Step 1: Run focused backend and frontend suites**

Run:

```bash
cd backend && .venv/bin/pytest tests/unit/analysis/test_result_import.py tests/integration/test_result_import.py -q
cd prototype && npm test -- --run tests/api-state.test.mjs
```

Expected: all tests pass with no warnings or errors.

- [ ] **Step 2: Back up the current database**

Create a timestamped SQLite backup beside the database using SQLite’s online backup API. Verify that the backup opens and contains the current batch pointer before import.

- [ ] **Step 3: Import the real result**

Run:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/import_analysis_result.py \
  --database "$HOME/Library/Application Support/AudioMemory/audio-memory.sqlite3" \
  --input exports/custom-prompt-analysis-v2.json
```

Expected: the command reports one new version and `cards=3`.

- [ ] **Step 4: Verify database invariants**

Check that the latest batch points to the new completed version, exactly three current cards are visible, the previous version remains, transcript row counts are unchanged, and profile row contents and timestamps are unchanged.

- [ ] **Step 5: Start the original app and verify visually**

Open `http://127.0.0.1:8765/`, confirm the feed shows only the three imported cards, and inspect all three detail views for title, summary, findings, analysis, quotes, actions, spacing, overflow, and empty-section suppression.

- [ ] **Step 6: Run regression checks**

Run the relevant backend suite, frontend suite, and `git diff --check`. Confirm the browser console contains no errors.

- [ ] **Step 7: Commit implementation state**

Stage only the importer, tests, and frontend compatibility changes. Do not stage unrelated pre-existing work.

```bash
git commit -m "feat: replace current analysis with custom prompt result"
```
