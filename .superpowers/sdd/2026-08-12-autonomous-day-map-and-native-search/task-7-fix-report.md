# Task 7 final review fixes

## Scope

- Require preview-token verification and exact signed snapshot/source matching before replaying an active reanalysis batch.
- Validate fresh autonomous final-analysis evidence before compatibility sanitization can remove unknown segment IDs or non-verbatim quotes.
- Ensure already-staged historical autonomous output is validated before compatibility sanitization.

## RED

Command:

```text
backend/.venv/bin/pytest -q \
  backend/tests/unit/reanalysis/test_preview.py::test_active_batch_replay_requires_valid_token_and_exact_signed_scope \
  backend/tests/unit/analysis/test_day_map_runner.py::test_mixed_invalid_final_evidence_preserves_context_for_one_retry \
  backend/tests/unit/analysis/test_autonomous_context.py::test_long_final_mixed_invalid_evidence_fails_after_one_context_retry
```

Result: `3 failed`.

- The active batch was returned for a malformed preview token.
- Mixed valid/invalid Day Map final evidence completed after one call because the unknown evidence ID was stripped.
- Mixed valid/invalid long-context final evidence completed instead of failing after one retry.

## GREEN

Implementation:

- `ReanalysisService.create_batch()` now verifies the signed token and requested scope first. Active replay additionally compares provider, model, credential generation, snapshot hash, and ordered source batch IDs; a different active snapshot raises `SnapshotChangedError`. The transaction-time replay branch applies the same gate.
- Both fresh autonomous-final routes validate the raw provider result. Invalid evidence receives exactly one semantic retry with the same input context; a second invalid result raises `autonomous_final_evidence_invalid`. Fresh valid output is persisted unchanged. Existing staged output is also validated before compatibility sanitization, so invalid references are never silently removed.

Focused result: `3 passed in 0.49s`.

## Regression results

- Relevant service/API/runner files: `47 passed in 2.94s`.
- Full backend suite: `759 passed, 28 skipped in 17.32s`.
- `git diff --check`: clean.

The 28 skips are the pre-existing compatibility-only legacy Event Map tests reported by pytest.
