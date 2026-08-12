# Task 7 canonicalization review fix

## Root cause

Fresh Day Map and long-context final results validated raw provider evidence,
then staged and returned the unsanitized payload. Resume validated the staged
payload before applying compatibility sanitization. Therefore, a raw-valid card
whose evidence lists were empty survived a fresh publish path but was removed
on resume.

The direct compatibility route also sanitized a fresh provider payload before
raw validation, which could hide unknown evidence instead of issuing the one
semantic retry required for invalid raw output.

## Change

- Fresh Day Map and long-context final outputs now validate the raw provider
  result first, sanitize/canonicalize it, then stage and return that canonical
  payload.
- The direct compatibility route now uses the same raw-validation-first order,
  stages only the canonical payload, and retries raw-invalid evidence once.
- Existing staged results still validate raw evidence before compatibility
  sanitization, preserving the established resume behavior.

## TDD evidence

Before the production change:

```text
backend/.venv/bin/pytest tests/unit/analysis/test_day_map_runner.py -q
2 failed, 15 passed
```

The failures showed that a fresh empty-evidence card remained in the return
value, and that the direct route dropped unknown raw evidence without retrying.

## Verification

```text
backend/.venv/bin/pytest \
  tests/unit/analysis/test_day_map_runner.py \
  tests/unit/analysis/test_autonomous_context.py \
  tests/unit/prompts/test_autonomous_schema.py -q
31 passed

git diff --check
clean

backend/.venv/bin/pytest -q
761 passed, 28 skipped
```

The focused parity regression confirms that fresh and resumed Day Map routes
both produce and retain the same canonical empty-card output.
