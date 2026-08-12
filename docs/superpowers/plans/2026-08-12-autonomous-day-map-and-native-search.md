# Autonomous Day Map and Native Search — implementation plan

> **For implementation:** Follow `superpowers:executing-plans` and execute each task in order, with the stated verification before committing.

**Goal:** Preserve the current audio-analysis result, while adding an autonomous full-transcript route: one visible `本次概览` per upload batch, model-discovered scenes, provider-native web search (up to five rounds), and final cards grounded separately in transcript and external sources.

**Architecture:** A new orchestration path first gives the selected model the complete reliable transcript to create a Day Map and batch overview. The model may request native provider web search; the server executes and persists at most five rounds, then invokes the model again with the whole transcript, map, and returned sources. Existing direct and compact routes remain as safe fallback only after an explicit provider context/input rejection.

**Constraints:** No fixed scene taxonomy; no extra search API key; no external facts in hidden profile; old batches/cards remain unchanged; maximum five rounds and five queries per round; no fabricated sources; `本次概览` is exactly one top card for every new batch.

**Tech stack:** FastAPI/SQLAlchemy/Alembic/Pydantic backend; provider adapters (Kimi, DeepSeek, OpenAI); React prototype frontend; pytest and frontend test runner.

## File map

- Add `backend/src/audio_memory/prompts/day_map_schema.py`: strict Day Map, overview, and search-round contracts.
- Add `backend/src/audio_memory/analysis/native_search.py`: capability result, query validation, source normalization, and persisted search state.
- Modify `backend/src/audio_memory/analysis/adapters/base.py`, `kimi.py`, `deepseek.py`, `openai.py`, `provider.py`: provider-native search capability probes and calls.
- Modify `backend/src/audio_memory/prompts/composer.py`, `autonomous_schema.py`: three full-read prompt phases and evidence/source checks.
- Add `backend/alembic/versions/0012_batch_overview_and_search_rounds.py`; modify `models.py`, `publisher.py`, `content/service.py`: durable overview/source/round data and feed ordering.
- Modify `backend/src/audio_memory/analysis/runner.py`: resilient Day Map → search → final pipeline and fallback.
- Modify `prototype/src/api/state.js`, `App.jsx`, styles/tests: distinct batch overview and external-source UI.
- Add unit/integration tests and `docs/benchmark-evidence/2026-08-12-day-map-native-search-acceptance.md`.

## Task 1: Establish strict autonomous contracts and search-state helpers

**Files:**
- Create `backend/src/audio_memory/prompts/day_map_schema.py`
- Create `backend/src/audio_memory/analysis/native_search.py`
- Create `backend/tests/unit/prompts/test_day_map_schema.py`
- Create `backend/tests/unit/analysis/test_native_search.py`

1. Write failing tests covering a Day Map with arbitrary scenes, a single `本次概览`, a valid search decision, five-query maximum, five-round maximum, source URL/id requirements, and forced finalization on round five.
2. Run `pytest backend/tests/unit/prompts/test_day_map_schema.py backend/tests/unit/analysis/test_native_search.py -q`; confirm failure because the modules do not exist.
3. Implement Pydantic models: `AutonomousDayMap`, `BatchOverview`, `AutonomousScene`, `NativeSearchDecision`, `NativeSearchQuery`, `SearchRound`, `SearchResultItem`, and `ExternalSource`. Scene labels/descriptions must be free text, not enums. Implement pure helper functions that reject invalid model output and preserve provider/source identifiers.
4. Re-run the same tests; confirm pass.
5. Commit only these files: `feat: add day map and native search contracts`.

## Task 2: Add provider-native search adapters with safe capability fallback

**Files:**
- Modify `backend/src/audio_memory/analysis/adapters/base.py`
- Modify `backend/src/audio_memory/analysis/adapters/kimi.py`
- Modify `backend/src/audio_memory/analysis/adapters/deepseek.py`
- Modify `backend/src/audio_memory/analysis/adapters/openai.py`
- Modify `backend/src/audio_memory/analysis/provider.py`
- Create `backend/tests/unit/analysis/test_native_search_adapters.py`

1. Write failing adapter tests: a supported Kimi native-search response normalizes citations/URLs to `ExternalSource`; an unavailable DeepSeek/OpenAI capability returns a structured unavailable result without throwing; malformed sources are retained as errors, not invented.
2. Run `pytest backend/tests/unit/analysis/test_native_search_adapters.py -q`; confirm failure.
3. Define a common adapter capability probe and native-search operation. Kimi uses its official `$web_search` tool protocol. DeepSeek/OpenAI use their documented endpoint/tool capability only when the configured endpoint, key, and model successfully advertise/accept it. Do not silently replace a provider with a third-party search service.
4. Store provider/model/tool metadata and call errors in the return value so the runner can show pure-audio fallback accurately.
5. Re-run adapter tests; confirm pass. Commit: `feat: add provider native web search adapters`.

## Task 3: Add the three model prompt phases and parsers

**Files:**
- Modify `backend/src/audio_memory/prompts/composer.py`
- Modify `backend/src/audio_memory/prompts/autonomous_schema.py`
- Modify `backend/src/audio_memory/analysis/provider.py`
- Create `backend/tests/unit/prompts/test_day_map_prompts.py`

1. Write failing tests asserting: the Day Map prompt receives the whole transcript and asks for no preset categories; the search prompt asks the model to decide whether more verification is valuable; final prompt receives transcript, map, and real sources; final cards expose `evidence_segment_ids` separately from `external_source_ids`.
2. Run `pytest backend/tests/unit/prompts/test_day_map_prompts.py -q`; confirm failure.
3. Implement `compose_autonomous_day_map`, `compose_autonomous_search_loop`, and `compose_autonomous_final_analysis`, plus parsing/repair rules. Require the overview to be a concise batch-level synthesis, not an analysis category. Require any external-source ID in a card to resolve to a persisted source; permit one repair request, then fail the version rather than fabricate a citation.
4. Re-run prompt tests; confirm pass. Commit: `feat: add autonomous full-read prompt phases`.

## Task 4: Persist the overview, search provenance, and position-zero feed item

**Files:**
- Create `backend/alembic/versions/0012_batch_overview_and_search_rounds.py`
- Modify `backend/src/audio_memory/models.py`
- Modify `backend/src/audio_memory/analysis/publisher.py`
- Modify `backend/src/audio_memory/content/service.py`
- Create `backend/tests/unit/analysis/test_day_map_publisher.py`
- Modify/add `backend/tests/unit/content/test_feed_service.py`

1. Write failing tests for one and only one overview for a new batch; idempotent re-publish; overview before cards; persisted search rounds/sources; and old batches showing exactly as before.
2. Run focused publisher/feed tests; confirm failure.
3. Add canonical batch-overview persistence plus structured search round/source storage to `AnalysisVersion` (or related normalized data using existing conventions). For current feed compatibility publish an overview-compatible `Card` at `scene_id="batch_overview"`, `position=0`; shift ordinary cards by one. Make the publisher replace/retry atomically and retain evidence/source mappings.
4. Generate and validate Alembic migration with the project’s migration command. Re-run focused tests; confirm pass.
5. Commit: `feat: persist batch overview and search provenance`.

## Task 5: Orchestrate full-read Day Map → native search → final cards

**Files:**
- Modify `backend/src/audio_memory/analysis/runner.py`
- Modify `backend/src/audio_memory/analysis/provider.py` as needed
- Modify `backend/src/audio_memory/analysis/retry.py` or existing retry helper as needed
- Create `backend/tests/unit/analysis/test_day_map_runner.py`
- Modify `backend/tests/unit/analysis/test_autonomous_context.py`

1. Write failing tests for: full transcript is default even above the current compact threshold; no-search Day Map finalizes immediately; supported search executes/persists up to five rounds; unsupported native search continues pure-audio; resume continues the exact staged round; only explicit context/input rejection invokes the existing compact route; and profile extraction gets transcript evidence only.
2. Run the focused runner tests; confirm failure.
3. Add staged state keys for `day_map`, `search_rounds`, `external_sources`, and final result. Build a checkpointed state machine: full Day Map, optional native search loop, forced final model pass, publish. Treat transient provider failures with existing retry rules; preserve successful prior stages. Do not infer category labels in server code. Filter all external text/URLs from profile candidates before persistence.
4. Re-run focused runner/context tests; confirm pass. Commit: `feat: orchestrate autonomous day map analysis`.

## Task 6: Present one clearly distinguished `本次概览` and sourced cards

**Files:**
- Modify `prototype/src/api/state.js`
- Modify `prototype/src/App.jsx`
- Modify `prototype/src/styles.css` (or active stylesheet)
- Modify/add `prototype/src/**/*.test.*`

1. Write failing state/UI tests: one `kind: "batch_overview"` item per new batch; it is first and titled exactly `本次概览`; it does not render as a normal scene card; cards with sources show a compact `外部资料` section distinct from recording evidence; old feed objects still render.
2. Run the frontend focused tests; confirm failure.
3. Normalize the position-zero overview separately in state. Render it once as the batch entry point. Render source title/domain/link only from backend-provided source objects; do not generate recommendation links in the frontend. Keep all current pages/cards working for historic data.
4. Re-run frontend tests/build; confirm pass. Commit: `feat: present batch overview and external sources`.

## Task 7: Verify compatibility, provider behavior, and real-audio acceptance

**Files:**
- Create `docs/benchmark-evidence/2026-08-12-day-map-native-search-acceptance.md`
- Modify/add targeted integration tests under `backend/tests/integration/`

1. Add integration tests for old batch compatibility, native-search-unavailable fallback, five-round exhaustion, restart/resume, source/evidence separation, and the no-external-profile invariant.
2. Run backend full suite from `backend`: `pytest -q`; run frontend test/build from `prototype` using its project scripts. Fix any regressions before proceeding.
3. Run configured-provider capability probes using a non-user batch/test request only. Record which configured provider/model supports native search, exact fallback behavior, and whether no extra key was used. Never log secrets.
4. Run one real four-file batch through the new branch, then manually inspect: exactly one `本次概览`; autonomous scenes include child interaction and identifiable media/program content when present; any web-enhanced claim has a real source; no external data enters the hidden profile; old historic cards remain visible.
5. Record observed timings, card quality observations, failures/fallbacks, and test commands/results in the acceptance document. Commit: `test: verify day map and native search flow`.

## Final verification and handoff

1. Check `git status --short` and ensure only intended tracked implementation files are staged/committed; preserve existing unrelated untracked files.
2. Re-run the exact regression commands recorded in Task 7 after the final commit.
3. Summarize provider capability outcomes, full-read fallback condition, observed four-file batch outcome, and any remaining product decision for the user. Do not claim native search works for a provider until its configured runtime probe succeeds.
