# Task 7 empty canonical result review fix

## Root cause

The final-analysis routes validated the raw provider result before applying
compatibility canonicalization. A large-transcript card with empty evidence
lists therefore passed the raw subset checks, then canonicalized to
`{"cards": []}`. Fresh execution staged and returned that empty result, while a
later resume rejected the staged result as a large transcript with no cards.

## Change

- Day Map final analysis, direct compatibility analysis, and long-context final
  analysis now validate both the raw result and its canonical form before
  staging.
- A canonical empty result for a context that requires cards receives the
  existing one bounded semantic retry with the same complete request context.
- If the retry also canonicalizes to empty, the route raises its established
  typed evidence error before writing an autonomous result.
- An older persisted empty canonical checkpoint is removed and treated as the
  failed first attempt; resume sends one feedback-guided retry rather than
  raising an untyped raw-validation error.

Raw validation is still performed before canonicalization on every provider
response. Normal successful output is unchanged.

## Regression coverage

- Large Day Map final result: fresh execution retries once, does not stage an
  empty canonical result, and resume of the historical empty checkpoint uses
  the same bounded retry/error behavior.
- Direct compatibility and long-context final routes: a raw-valid,
  evidence-empty card retries once and fails with the route's typed evidence
  error if still empty.

## Verification

```text
backend/.venv/bin/pytest backend/tests/unit/analysis/test_day_map_runner.py -q
RED: 1 failed, 17 passed (the fresh result incorrectly staged canonical empty cards)

backend/.venv/bin/pytest \
  backend/tests/unit/analysis/test_day_map_runner.py \
  backend/tests/unit/analysis/test_autonomous_context.py -q
27 passed

backend/.venv/bin/pytest backend/tests/integration/test_day_map_native_search_flow.py -q
4 passed

(cd backend && ./.venv/bin/pytest -q)
764 passed, 28 skipped

backend/.venv/bin/python -m compileall -q backend/src/audio_memory/analysis/runner.py
git diff --check
clean
```
