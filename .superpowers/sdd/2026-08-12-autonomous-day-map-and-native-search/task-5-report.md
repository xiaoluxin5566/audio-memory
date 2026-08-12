# Task 5 report: autonomous Day Map runner

## Outcome

Implemented the production runner state machine for:

1. complete-transcript Day Map generation;
2. zero to five provider-native search rounds;
3. a second complete-transcript final-analysis pass;
4. transcript-only hidden-profile extraction; and
5. publication through the existing atomic publisher with persisted Day Map,
   search-round, and external-source state.

The complete transcript is now always the first route, including inputs above the
old 30,000-character planning threshold. The existing direct/windowed route is
entered only after `ProviderAnalysisError(code="provider_input_rejected")`.
HTTP 413 and narrowly recognized 400/422 context/input-limit responses produce
that typed error. Network, account, schema, content-policy, and generic provider
errors do not trigger compaction.

## Checkpoint and resume behavior

- `day_map`, `search_rounds`, `external_sources`, and final `autonomous` result
  are stored in `AnalysisVersion.staged_results_json`.
- Each search decision is saved as a pending `SearchRound` before invoking the
  provider tool. A resumed run therefore repeats the exact interrupted round
  and query, without repeating completed rounds.
- Each completed search round and its canonical external sources are committed
  before asking the model whether another round is valuable.
- Stored rounds must be contiguous, remain within the five-round limit, and
  exactly match the canonical persisted sources.
- Search exhaustion forces the final analysis pass. Unsupported native search
  persists the structured error and continues to pure-audio final analysis.
- A transient typed native-search error receives the same bounded one-extra-
  attempt retry used by provider generation requests. Successful earlier stages
  remain checkpointed.

## Evidence and privacy boundaries

- Day Map scene evidence and file IDs are checked against the reliable
  transcript.
- Final transcript evidence and external source references are validated
  independently.
- The runner does not assign or infer any category label.
- Hidden-profile extraction receives the eligible transcript and no final-card
  text. Candidates containing URLs or any persisted external-source title,
  publisher, date, URL, or support text are removed before persistence. Existing
  segment-ID validation remains in force.

## TDD evidence

Initial focused RED run:

```text
7 failed, 7 passed
```

The seven failures were the intended missing runner behaviors: no Day Map
pipeline, no typed fallback boundary, and the old direct/compact default. A
separate provider-boundary RED test confirmed HTTP 413 was previously reported
as `content_rejected`. A transient-search RED test confirmed the first typed
network failure previously escaped without the bounded retry.

Final focused verification:

```text
PYTHONPATH=src .venv/bin/pytest \
  tests/unit/analysis/test_day_map_runner.py \
  tests/unit/analysis/test_autonomous_context.py -q

15 passed in 0.69s
```

Relevant analysis/prompt/feed regression:

```text
PYTHONPATH=src .venv/bin/pytest \
  tests/unit/analysis \
  tests/unit/prompts/test_day_map_prompts.py \
  tests/unit/content/test_feed_service.py -q

128 passed in 2.37s
```

Final backend regression (pytest import isolation is needed because the project
contains two unrelated modules named `test_events.py`):

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/pytest --import-mode=importlib -q

740 passed, 28 skipped in 17.48s
```

All 28 skips are the suite's explicitly disabled legacy Event Map pipeline.

## Self-review notes

- Search result records are reconstructed only from provider-issued normalized
  sources, preserving provider result ID, title, URL, publisher, date, snippet,
  and originating round in the shape required by the publisher.
- Provider/source identity mismatch fails before profile extraction or
  publication.
- The production search adapters currently expose supported native search only
  where Task 2 proved it. Unsupported adapters retain the pure-audio route.
- No live provider capability probe or real-audio acceptance was performed;
  those remain Task 7, not Task 5.

## Review fix round 1

Addressed all four Important review findings:

1. A typed full-input rejection now stages `fallback` metadata, a compatible
   category-free Day Map with one `本次概览`, empty search/source state when
   needed, a terminal search phase, and the compact-route final result. A
   full-run regression exercises `run()` through the publisher's Day Map
   contract and verifies the overview is counted exactly once.
2. Every terminal search decision is persisted in `search_phase` before the
   final model call. Its completed-round count is validated on resume. A final
   call failure/resume regression proves that neither the decision model nor
   native search runs again.
3. `autonomous_day_map_invalid` and
   `autonomous_search_decision_invalid` are accepted by the upload analysis
   recovery allowlist. The upload API regression exercises their in-place
   autonomous-version retry path.
4. Native-search network, rate-limit, and provider-unavailable results now
   carry a structured `retriable` marker. The runner retries one structured
   failure once; a second unavailable result is persisted as the round error
   and analysis continues to pure-audio finalization. Exhausted retriable
   exceptions are likewise converted to a safe unavailable result rather than
   blocking publication.

The first combined RED run for the new review regressions produced seven
expected failures: missing fallback overview state, missing terminal phase,
missing structured retriable result metadata, and the two absent upload retry
codes. After implementation, focused review coverage passed:

```text
PYTHONPATH=src .venv/bin/pytest \
  tests/unit/analysis/test_day_map_runner.py \
  tests/unit/analysis/test_native_search_adapters.py \
  'tests/integration/test_upload_jobs.py::test_failed_model_analysis_retries_with_active_provider_without_whisper' \
  -q

38 passed in 1.84s
```

Relevant regression plus syntax compilation:

```text
PYTHONPATH=src .venv/bin/python -m compileall -q \
  src tests/unit/analysis/test_day_map_runner.py
PYTHONPATH=src .venv/bin/pytest \
  tests/unit/analysis \
  tests/unit/prompts/test_day_map_prompts.py \
  tests/unit/content/test_feed_service.py \
  tests/integration/test_upload_jobs.py -q

158 passed in 4.42s
```

Fresh full backend verification:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/pytest --import-mode=importlib -q

746 passed, 28 skipped in 18.36s
```

All 28 skips remain the explicitly disabled legacy Event Map pipeline.
