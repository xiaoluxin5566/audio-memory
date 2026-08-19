# Audio Memory beta.3 Stability and Branch Consolidation Implementation Plan

> **For Codex:** Execute this plan task by task with test-driven development. Do not call a real provider or read production Keychain credentials without fresh user authorization. Preserve every dirty legacy worktree until its unique value is documented and migrated.

**Goal:** Make transcription-to-analysis handoff durable and observable, retain strict development/production isolation, migrate valuable legacy branch work onto current `main`, and remove obsolete branches only after verification.

**Architecture:** Keep `AnalysisTaskCoordinator` as the sole analysis queue authority. Move the upload job's transition to `analyzing` into the same short SQLite transaction that creates its pending `AnalysisVersion`; configure all application connections for WAL and bounded lock waiting; reconcile inconsistent durable states at startup; emit redacted structured lifecycle events; expose the same invariants through the read-only doctor command and the UI.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, aiosqlite/SQLite, pytest/pytest-asyncio, React/Vite, Node test runner, Playwright, shell release tooling.

---

## Execution rules

- Work only in `/Users/liujinxin/Documents/音频Always on Demo/.worktrees/beta3-stability` on `codex/beta3-stability`.
- Start each behavior change with a failing focused test; make the smallest implementation pass; then run the surrounding suite.
- Commit each task independently. Do not mix legacy migration, queue repair, UI, or branch deletion in one commit.
- Never reset, clean, or blanket-checkout a legacy worktree. Record dirty files as unique evidence.
- Use a fake provider for end-to-end validation. A real DeepSeek request is a separate approval gate.

## Task 1: Produce the authoritative legacy-branch audit

**Files:**

- Create: `docs/working/2026-08-19-beta3-legacy-branch-audit.md`
- Read: `docs/superpowers/specs/2026-08-19-beta3-stability-and-branch-consolidation-design.md`
- Read: each legacy branch's commits and dirty worktree diff

**Step 1: Capture immutable evidence**

For each of these branches, record base, unique commits, changed paths, current worktree, dirty files, and whether an equivalent exists on `main`:

- `codex/report-audit-revision-pipeline`
- `codex/analysis-sleep-prevention`
- `codex/smooth-progress`
- `codex/dev-prod-isolation`
- `codex/cloud-asr-evaluation`

Use read-only Git inspection (`git merge-base`, `git log main..branch`, `git diff --stat main...branch`, and `git status --short` inside existing worktrees). Do not alter those worktrees.

**Step 2: Classify every unique result**

Give each result exactly one disposition: `equivalent-on-main`, `migrate-to-beta3`, `retain-as-research-evidence`, or `obsolete`. Include evidence paths and a deletion prerequisite.

**Step 3: Validate audit completeness**

Confirm every unmerged local branch appears in the document and every dirty legacy worktree has a file-level disposition.

**Step 4: Commit**

```bash
git add docs/working/2026-08-19-beta3-legacy-branch-audit.md
git commit -m "docs: audit legacy beta branches"
```

## Task 2: Migrate the accepted development/production isolation value

**Files:**

- Modify: `backend/src/audio_memory/config.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/providers/keychain.py`
- Modify: `scripts/runtime_config.py`
- Modify: `scripts/dev_lifecycle.py`
- Modify: `scripts/dev-start.sh`
- Modify: `scripts/dev-stop.sh`
- Create: `backend/tests/unit/test_runtime_config.py`
- Create: `backend/tests/unit/test_dev_lifecycle.py`
- Create: `backend/tests/integration/test_app_startup.py`
- Test: `backend/tests/e2e/test_prompt_eval_contract.py`

**Step 1: Write failing isolation tests**

Cover production `8765` versus development `8766`, separate data/log/lock/Keychain roots, simultaneous startup, profile-specific stop, and rejection of macOS case/symlink/firmlink aliases that resolve into production.

**Step 2: Run the focused tests and verify failure**

```bash
cd backend
.venv/bin/pytest -q tests/unit/test_runtime_config.py tests/unit/test_dev_lifecycle.py tests/integration/test_app_startup.py tests/e2e/test_prompt_eval_contract.py
```

**Step 3: Selectively port only audited behavior**

Reimplement accepted isolation behavior from current `main`; do not merge the legacy branch wholesale. Keep production defaults unchanged and make development roots explicit and pinned.

**Step 4: Run focused tests and commit**

```bash
cd backend
.venv/bin/pytest -q tests/unit/test_runtime_config.py tests/unit/test_dev_lifecycle.py tests/integration/test_app_startup.py tests/e2e/test_prompt_eval_contract.py
cd ..
git add backend/src/audio_memory/config.py backend/src/audio_memory/main.py backend/src/audio_memory/providers/keychain.py scripts/runtime_config.py scripts/dev_lifecycle.py scripts/dev-start.sh scripts/dev-stop.sh backend/tests
git commit -m "feat: enforce beta3 runtime isolation"
```

## Task 3: Configure SQLite for bounded concurrent writes

**Files:**

- Modify: `backend/src/audio_memory/db.py`
- Modify: `backend/migrations/env.py`
- Test: `backend/tests/integration/test_database_schema.py`
- Create: `backend/tests/integration/test_sqlite_concurrency.py`

**Step 1: Write failing PRAGMA tests**

Assert every async application connection reports `foreign_keys=1`, `journal_mode=wal`, `busy_timeout=5000`, and `synchronous=1` (`NORMAL`). Assert migration-created databases reopen with WAL.

**Step 2: Write a failing real-contention test**

Hold a write transaction on one connection, attempt analysis queue insertion on another, release before five seconds, and assert the second writer succeeds without leaving an orphan job.

**Step 3: Implement connection configuration**

Set PRAGMAs in the SQLAlchemy connect event, ensure WAL is established during migration/startup, and fail startup with a safe diagnostic if the requested journal mode is not active.

**Step 4: Run and commit**

```bash
cd backend
.venv/bin/pytest -q tests/integration/test_database_schema.py tests/integration/test_sqlite_concurrency.py
cd ..
git add backend/src/audio_memory/db.py backend/migrations/env.py backend/tests/integration/test_database_schema.py backend/tests/integration/test_sqlite_concurrency.py
git commit -m "fix: bound sqlite write contention"
```

## Task 4: Add redacted structured lifecycle logging

**Files:**

- Create: `backend/src/audio_memory/observability.py`
- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Modify: `backend/src/audio_memory/analysis/provider.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Test: `backend/tests/unit/test_observability.py`
- Test: `backend/tests/unit/analysis/test_task_coordinator.py`
- Test: `backend/tests/integration/test_transcription_recovery.py`

**Step 1: Write failing event-schema and redaction tests**

Require one-line JSON with event, timestamp, job/version/provider/model IDs, elapsed time, status, normalized error type, owner, and lease when applicable. Prove transcript, prompt, model payload, API key, and arbitrary exception text cannot be serialized.

**Step 2: Implement a narrow event helper**

Accept only an allowlist of scalar fields and normalized exception class names. Send events through the existing application logger without changing release log routing.

**Step 3: Instrument lifecycle boundaries**

Emit `transcription.completed`, all `analysis.enqueue.*` events, `analysis.worker.claimed`, provider request start/finish, `analysis.job.failed`, and later recovery events. Log elapsed durations with a monotonic clock.

**Step 4: Run and commit**

```bash
cd backend
.venv/bin/pytest -q tests/unit/test_observability.py tests/unit/analysis/test_task_coordinator.py tests/integration/test_transcription_recovery.py
cd ..
git add backend/src/audio_memory/observability.py backend/src/audio_memory/analysis/task_coordinator.py backend/src/audio_memory/analysis/provider.py backend/src/audio_memory/api/jobs.py backend/tests
git commit -m "feat: log analysis handoff lifecycle"
```

## Task 5: Make transcription-to-analysis handoff atomic and cancellation-safe

**Files:**

- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `backend/src/audio_memory/transcription/checkpoints.py`
- Test: `backend/tests/unit/analysis/test_task_coordinator.py`
- Test: `backend/tests/integration/test_transcription_recovery.py`
- Test: `backend/tests/integration/test_upload_jobs.py`

**Step 1: Add failing durable-invariant tests**

Cover success, pre-commit exception, SQLite timeout, caller cancellation before commit, caller cancellation immediately after commit, and real worker wake-up. In every database snapshot assert:

- `analyzing` has one `pending/running` version;
- failed enqueue preserves all `Transcript` rows;
- failed enqueue becomes `failed/model_analysis_failed` and is retryable;
- sleep protection is released exactly once on failure/cancellation and retained after durable success;
- cancellation is re-raised, never swallowed.

**Step 2: Remove the premature job transition**

Ensure transcription completion persists transcript/risk results but does not independently claim that model analysis has started.

**Step 3: Implement the atomic handoff**

Inside one short `BEGIN IMMEDIATE` transaction, validate source job, create `AnalysisVersion(status=pending)`, and update `AnalysisJob(stage=analyzing, error_code=NULL)`. Retry only SQLite BUSY/LOCKED within the handoff budget. Keep worker notification inside a cancellation-safe post-commit section.

**Step 4: Implement explicit failure ownership**

If no durable version exists, persist the retryable analysis failure and release sleep protection without altering transcripts. If commit succeeded but the caller observed an exception, reconcile from durable state and do not falsely mark failure or release worker-owned protection.

**Step 5: Run and commit**

```bash
cd backend
.venv/bin/pytest -q tests/unit/analysis/test_task_coordinator.py tests/integration/test_transcription_recovery.py tests/integration/test_upload_jobs.py tests/integration/test_sqlite_concurrency.py
cd ..
git add backend/src/audio_memory/analysis/task_coordinator.py backend/src/audio_memory/api/jobs.py backend/src/audio_memory/transcription/checkpoints.py backend/tests
git commit -m "fix: make analysis handoff durable"
```

## Task 6: Reconcile inconsistent queue state at startup

**Files:**

- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Modify: `backend/src/audio_memory/main.py`
- Test: `backend/tests/unit/analysis/test_task_coordinator.py`
- Test: `backend/tests/integration/test_app_startup.py`

**Step 1: Write failing recovery tests**

Seed: orphan `analyzing` job, expired `running` lease, valid pending upload, and consistent completed/failed jobs. Assert only inconsistent rows change, transcripts remain byte-for-byte equivalent, pending work is notified, and each repair logs `analysis.recovery.reconciled` with counts.

**Step 2: Implement bounded reconciliation**

Run reconciliation before the worker starts. Convert orphan jobs to retryable analysis failure, return expired leases and linked reanalysis items to pending, and preserve healthy states.

**Step 3: Run and commit**

```bash
cd backend
.venv/bin/pytest -q tests/unit/analysis/test_task_coordinator.py tests/integration/test_app_startup.py
cd ..
git add backend/src/audio_memory/analysis/task_coordinator.py backend/src/audio_memory/main.py backend/tests
git commit -m "fix: reconcile analysis queue on startup"
```

## Task 7: Add read-only doctor queue diagnostics

**Files:**

- Modify: `scripts/doctor_checks.py`
- Modify: `scripts/doctor.sh`
- Modify: `scripts/audio-memory`
- Test: `backend/tests/e2e/test_prompt_eval_contract.py`
- Test: `backend/tests/unit/test_audio_memory_cli.py`

**Step 1: Write failing doctor fixtures**

Build databases for orphan analyzing, expired lease, stale pending, job/version error mismatch, wrong journal mode, and healthy queue. Assert doctor reports the exact category without mutating any row or PRAGMA.

**Step 2: Implement the read-only check**

Open SQLite in read-only mode, perform invariant queries, print concise non-sensitive counts, and return non-zero when intervention is required.

**Step 3: Run and commit**

```bash
cd backend
.venv/bin/pytest -q tests/e2e/test_prompt_eval_contract.py tests/unit/test_audio_memory_cli.py
cd ..
git add scripts/doctor_checks.py scripts/doctor.sh scripts/audio-memory backend/tests/e2e/test_prompt_eval_contract.py backend/tests/unit/test_audio_memory_cli.py
git commit -m "feat: diagnose analysis queue consistency"
```

## Task 8: Make UI analysis progress reflect durable state

**Files:**

- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `prototype/src/store.js`
- Modify: `prototype/src/App.jsx`
- Test: `backend/tests/integration/test_upload_jobs.py`
- Test: `prototype/tests/provider-models.test.mjs`
- Create: `prototype/tests/job-progress-state.test.mjs`

**Step 1: Write failing API/UI state tests**

Add an explicit analysis phase derived from the latest durable version: `enqueueing`, `pending`, `running`, or `failed`. Prove the UI does not display “DeepSeek 正在阅读全文” when no version exists, and displays “分析未开始，可重试” for enqueue failure.

**Step 2: Implement the minimal API projection and copy**

Keep provider/model labels, but only show actual model processing after a version is `running`. Show a queue-safe waiting message for `pending`.

**Step 3: Run and commit**

```bash
cd backend
.venv/bin/pytest -q tests/integration/test_upload_jobs.py
cd ../prototype
npm test -- --run tests/provider-models.test.mjs tests/job-progress-state.test.mjs
cd ..
git add backend/src/audio_memory/api/jobs.py backend/tests/integration/test_upload_jobs.py prototype/src/store.js prototype/src/App.jsx prototype/tests
git commit -m "fix: show durable analysis progress"
```

## Task 9: Run fake-provider end-to-end and full regression

**Files:**

- Create: `backend/tests/e2e/test_analysis_handoff.py`
- Modify if required: `prototype/tests/e2e/*`
- Create: `docs/qa/2026-08-19-beta3-analysis-handoff-acceptance.md`

**Step 1: Add the controlled end-to-end scenario**

Use a fake provider to prove upload/transcription fixture → atomic enqueue → worker claim → provider start → publication → Feed. Capture database invariants and ordered structured events.

**Step 2: Run backend, frontend, and packaging suites**

```bash
cd backend
.venv/bin/pytest -q tests
cd ../prototype
npm test
npm run build
npm run test:e2e
cd ..
```

Run release packaging and installer tests already present in the repository. Do not publish, tag, or install over the production release.

**Step 3: Record evidence and commit**

Record exact commands, pass counts, fake-provider evidence, database queries, log event order, and any skipped hardware/provider tests.

```bash
git add backend/tests/e2e/test_analysis_handoff.py prototype/tests docs/qa/2026-08-19-beta3-analysis-handoff-acceptance.md
git commit -m "test: verify beta3 analysis handoff"
```

## Task 10: Review migrated value and clean obsolete branches

**Files:**

- Modify: `docs/working/2026-08-19-beta3-legacy-branch-audit.md`
- Create: `docs/working/2026-08-19-beta3-branch-cleanup-evidence.md`

**Step 1: Independent code review**

Review the complete `main...codex/beta3-stability` diff for correctness, security, resource ownership, logging redaction, isolation, and acceptance coverage. Resolve every actionable issue and rerun affected tests.

**Step 2: Prove each deletion prerequisite**

For every legacy branch, link its migrated commit or retained research document and passing tests. Recheck its worktree status immediately before removal.

**Step 3: Request the destructive-action gate**

Present the exact worktrees, local branches, and remote branches proposed for deletion. Obtain user approval before deleting dirty worktrees or remote refs. Preserve release tags.

**Step 4: Remove only approved obsolete worktrees and branches**

Remove worktrees first, then local branches, then explicitly approved remote branches. Never force-delete a branch whose unique value is not proven migrated.

**Step 5: Verify repository cleanliness and commit evidence**

```bash
git worktree list
git branch --no-merged main
git status --short
git add docs/working/2026-08-19-beta3-legacy-branch-audit.md docs/working/2026-08-19-beta3-branch-cleanup-evidence.md
git commit -m "chore: record legacy branch consolidation"
```

## Task 11: Integrate to main and prepare beta.3 candidate

**Step 1: Final verification before integration**

Run the complete backend/frontend/release suite from a clean branch and verify `git diff --check`. Confirm no real credentials, transcript content, generated user data, `.runtime`, or logs are tracked.

**Step 2: Present integration evidence**

Summarize commits, audit dispositions, test counts, remaining authorized real-world checks, and rollback path. Obtain user approval before merging to `main`.

**Step 3: Merge without publishing**

Merge `codex/beta3-stability` into `main`, rerun smoke tests on `main`, and remove the feature worktree/branch only after the merge is verified.

**Step 4: Build the beta.3 candidate**

Build and test the candidate in an isolated HOME. Verify fresh install, beta.2 upgrade with backup/history preservation, rollback, production/development simultaneous operation, doctor, API health, and final Feed.

**Step 5: Separate release gate**

Do not create a release tag, push a release, replace the installed beta.2, or make a real DeepSeek call until the user explicitly approves that action.
