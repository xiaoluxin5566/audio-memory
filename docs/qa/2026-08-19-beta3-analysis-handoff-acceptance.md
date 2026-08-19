# Beta 3 analysis handoff acceptance

Date: 2026-08-19

## Scope

This acceptance covers the durable handoff from completed local transcription to queued model analysis, startup recovery, read-only diagnosis, redacted lifecycle logs, and user-visible queue progress.

No real provider, API key, Keychain entry, production data root, or production service was used.

## Results

- Backend: `1092 passed, 28 skipped`.
- Frontend Node tests: `96 passed`.
- Browser acceptance: `26 passed`.
- Production frontend build: succeeded.
- Diff whitespace check: clean.

## End-to-end handoff evidence

`backend/tests/e2e/test_analysis_handoff.py` exercises the real task coordinator with a fake provider:

1. A completed local transcript is already persisted.
2. The transcription pipeline atomically creates a pending analysis version and moves the job to `analyzing`.
3. The worker is notified, claims the version, invokes the fake provider, and completes the job.
4. The original transcript remains unchanged.
5. Sleep protection is released exactly once by the analysis owner.

The expected redacted lifecycle event order is asserted:

1. `transcription.completed`
2. `analysis.enqueue.started`
3. `analysis.enqueue.lock_acquired`
4. `analysis.enqueue.transaction_started`
5. `analysis.enqueue.committed`
6. `analysis.enqueue.worker_notified`
7. `analysis.worker.claimed`
8. `analysis.provider.request_started`
9. `analysis.provider.request_finished`

The log assertion also verifies that transcript text is not emitted.

## Failure and recovery coverage

- Submission failure and timeout preserve the transcript, mark the job retryable as `model_analysis_failed`, and release sleep protection.
- Cancellation is re-raised, is not swallowed, and releases sleep protection.
- An exception observed after a durable queue commit keeps the job analyzing and retains analysis ownership of sleep protection.
- Startup reconciliation preserves healthy pending/running work, retries expired leases, and marks orphan analyzing jobs as failed instead of leaving them stuck.
- The doctor command reports orphan jobs, expired leases, stale pending work, job/version mismatches, WAL mode, and busy timeout without mutating the database.
- The UI distinguishes `pending`, `running`, and missing durable queue states; it no longer claims DeepSeek is reading before a worker claim.

## Explicit boundary

A real DeepSeek request was not run. Live provider and Keychain validation requires separate user authorization and is not necessary to validate the queue handoff, recovery, state transition, or log safety implemented here.
