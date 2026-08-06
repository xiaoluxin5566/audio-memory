# Task 6 implementation report

## Status

Implemented version-keyed publication, current-version feed projection, durable
todo candidate reconciliation and tombstones, old-version QA/feedback
preservation, deterministic two-pass profile rebuilding, profile-only retry,
and version-aware history cleanup. Historical publication reuses the original
`JobFile` and `Transcript` rows and never moves or copies source audio.

## RED / GREEN evidence by behavior

1. Todo reconciliation:
   - RED: `test_todo_reconciliation.py` initially failed collection with
     `ModuleNotFoundError: audio_memory.analysis.todos`.
   - GREEN: the first cycle passed 7/7 tests for stable-source refresh,
     user-edited text/deadline preservation, manual completion preservation,
     overdue-incomplete preservation, tombstones, ambiguous sources, and
     incompatible deadlines.
   - Follow-up RED showed an incompatible deadline reused the same unique
     fingerprint; repeated reanalysis then accumulated a third todo, and a
     deadline-disambiguated tombstone did not block resurrection. GREEN uses a
     deterministic deadline disambiguator while matching stable source fields,
     so later versions update the same separated todo and either base or
     disambiguated tombstones block it.
   - Review RED showed different normalized objects merged (`len == 1`, wanted
     2). GREEN compares object identity, adds optional `StrictTodoDraft.object`,
     and includes normalized object in candidate persistence/fingerprints.
2. Deterministic profile rebuild:
   - RED: `test_profile_rebuild.py` initially failed collection with
     `ModuleNotFoundError: audio_memory.analysis.profile_rebuild`.
   - GREEN: identical facts aggregate independently of version order; only the
     supplied current versions participate; active-profile replacement rolls
     back completely on insertion failure.
   - Review RED showed a current migrated version with no candidate rows lost
     its legacy fact. GREEN retains facts belonging to migration-marked current
     versions while rebuilding candidate-backed current versions.
   - Follow-up RED showed a mixed refreshed/legacy aggregate growing from
     evidence count 2 to 3. GREEN filters retained provenance to missing legacy
     jobs and uses stable aggregate counts; two consecutive rebuilds now return
     the same sources and evidence count.
3. Atomic version publication and current feed:
   - RED: importing `VersionPublisher` failed because only the pre-Task-6 class
     existed.
   - After the initial publisher implementation, two tests still failed because
     feed returned old and new cards together and retained a `should_generate`
     false scene. GREEN filters the join by
     `Card.analysis_version_id == Batch.current_analysis_version_id`.
   - A missing sixth scene is rejected before filesystem/database side effects;
     the old pointer remains current and zero partial new cards exist.
   - New cards have empty QA. Old cards and their QA remain stored, queryable,
     and accept additional old-card questions after the current pointer moves.
4. Recoverable first publication and history audio reuse:
   - RED: after a physical audio move and formal publication rollback, a new
     retry version failed with `FileNotFoundError` because its version-derived
     destination differed.
   - GREEN: the original batch/audio destination is deterministic by source
     job, so a distinct retry version reconciles the already-moved file. The
     historical path keeps the existing source path unchanged and creates no
     audio or transcript duplicate.
5. Todo candidate publication:
   - RED: two same-source candidates with incompatible dates produced only one
     candidate/todo and `todo_count == 1`.
   - GREEN: exact compatible duplicates deduplicate, but incompatible deadline
     identities persist separately. A publisher-level object test also keeps
     two same-action/deadline todos when their objects differ.
   - Persisted todo counts are queried by version after reconciliation, so the
     first outcome and completed-version idempotent outcome agree.
6. Todo user state and feedback snapshots:
   - RED: content API tests observed `user_edited=False`, no delete tombstone,
     and feedback provider/model values from mutable batch/job state.
   - GREEN: user text/deadline writes set `user_edited`, completion writes set
     `completion_source=user`, deletion stores the stable fingerprint, and
     feedback reads provider/model/prompt from the card's `AnalysisVersion`.
7. Two-pass historical profile finalization:
   - RED: with profile finalization removed, both final-item tests left the
     history batch `running` instead of `completed` or
     `content_completed_profile_failed`.
   - GREEN: the content transaction first publishes the current versions and
     records a durable profile-retry state. The independent profile transaction
     atomically replaces facts; success finalizes the batch, while failure
     retains the old facts and the retryable failure status.
   - RED: `VersionPublisher` had no `retry_profile` operation. GREEN exposes an
     idempotent status-validated profile-only retry that changes no cards.
8. History-item recovery/release:
   - RED: expired-lease recovery and coordinator close reset the version to
     pending but left its linked `ReanalysisItem` running.
   - GREEN: both transitions reset the owned linked item in the same database
     transaction. Successful publication uses the design state `succeeded`.
9. Complete history clearing:
   - RED first left `TodoTombstone`; a richer versioned fixture then raised an
     FK error on standalone pending versions; the staging regression finally
     failed because `HistoryCleaner` accepted no staging root.
   - GREEN: clear nulls current pointers and deletes reanalysis runs, versions,
     published batches, profiles, tombstones, manifests, and jobs in FK-safe
     order, then safely clears both published-audio and staging roots. Provider
     metadata, prompt files, feedback index rows, and exported feedback files
     remain.

## Exact final verification commands and results

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_todo_reconciliation.py tests/unit/analysis/test_profile_rebuild.py tests/integration/test_atomic_batch_commit.py tests/integration/test_content_api.py tests/integration/test_feedback_and_clear.py -q`
  - `41 passed in 1.38s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `357 passed in 6.62s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run python -m compileall -q src tests`
  - exit 0, no output
- `cd backend && git diff --check`
  - exit 0, no output

The commands above are rerun once more immediately before commit; the final
commit gate below supersedes these pre-report results if counts differ.

## Transaction and rollback invariants

- The exact six-scene set is validated before an audio move or formal write.
- First publication moves staging audio to a source-job-stable destination.
  Batch creation, version attachment, caller-session `require_card_version`,
  cards, todo candidates, reconciled todos, new-upload profile deltas, current
  pointer, job/version terminal state, and history-item success share one
  SQLite transaction.
- A formal transaction failure exposes neither a batch nor partial cards and
  keeps the prior current version visible. A later version can reconcile an
  already-moved audio file at the same source-job destination.
- Historical publication has a non-null source batch and therefore never calls
  the audio move path. It references the original job/files/transcripts only.
- Old versions/cards are not deleted during replacement, so QA and feedback
  references remain valid. Feed visibility is solely the current pointer.
- Profile rebuilding is intentionally independent from content publication.
  Its delete/insert swap is one transaction. Failure rolls the swap back and
  leaves content published with `content_completed_profile_failed`.
- Todo tombstones and protected user state participate in the same content
  publication transaction; an exception cannot expose candidates without the
  corresponding pointer/state transition.

## Migration and compatibility decisions

- Migration 0003 provides `analysis_version_id`, todo candidate/tombstone
  provenance, normalized todo fields, profile candidates, and current-version
  pointers. Formal review fix round 1 adds migration 0006 for immutable
  `published_card_count` and `published_todo_count` fields on each analysis
  version. Formal review fix round 2 backfills every existing completed
  version during upgrade, so completed rows never depend on a mutable live-row
  compatibility query.
- `AnalysisPublisher` remains an import alias to `VersionPublisher` so Task 5
  wiring and existing callers use the new strict implementation without a
  parallel legacy path.
- Original batch IDs are now deterministic by source job rather than analysis
  attempt. This is necessary for recovery across a new retry version after a
  pre-transaction audio move.
- `StrictTodoDraft.object` is optional, preserving existing provider payloads
  while allowing new outputs to distinguish same-action todos by object.
- Migrated initial versions are identifiable by their empty fixed-rules hash.
  Rebuild retains their active facts when candidate backfill is unavailable;
  candidate-backed versions remain authoritative.

## Self-review and independent review

- Mutation checks cover missing scene, wrong feed join, pointer-before-content,
  tombstone omission, user-state reset, incompatible todo merge, duplicate
  candidate collapse, mutable feedback metadata, destructive profile swap,
  repeated aggregate growth, stale history items, and incomplete cleanup.
- Publication uses the caller transaction for `require_card_version` and does
  not open a validation session that could race the pointer update.
- The profile snapshot used by scene work remains the immutable
  `AnalysisVersion.profile_snapshot_json`; no per-item published profile is fed
  into another item in the same history batch.
- An independent review initially found five Important and one Minor issue:
  cross-version audio recovery, todo deadline/object uniqueness, legacy profile
  retention, profile-only retry, staging cleanup, and idempotent todo counts.
  All received failing regressions and fixes. A second review found mixed
  legacy aggregate growth and incomplete production object handling; both were
  closed with failing tests. Final reviewer verdict: **READY**, with no Critical
  or Important findings.
- `docs/HANDOFF-2026-08-06.md` was not modified, staged, deleted, or committed.

## Concerns / deferred boundaries

- The public profile-only retry operation is implemented at the publisher
  boundary; the history reanalysis control API that invokes it belongs to the
  later history-service/API task.
- Legacy aggregate evidence cannot be perfectly apportioned per source because
  pre-version facts did not store per-job counts. The rebuild conservatively
  retains only missing legacy-job provenance and uses a stable maximum count,
  preventing data loss and repeated-count inflation.
- Retry-analysis accepted states for `credential_changed` and
  `fixed_rules_changed` remain outside Task 6 because this task did not modify
  the retry API. The stale `ReanalysisItem` recovery/release observation was in
  scope and is closed.

## Formal review fix round 1

### Protected todo deadline identity

- Root cause: reconciliation compared a candidate's immutable source deadline
  to mutable `Todo.due_at`. Moving a user-edited todo to another calendar date
  therefore made the same stable source appear incompatible and created a
  duplicate on every reanalysis.
- RED command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_todo_reconciliation.py::test_user_edited_deadline_does_not_duplicate_stable_source_on_reanalysis tests/integration/test_atomic_batch_commit.py::test_completed_version_keeps_immutable_todo_count_after_newer_publication -q`
  failed exactly 2 tests: the first reconciliation returned two todos and the
  older completed version later returned `todo_count == 0`.
- GREEN: a protected (`user_edited`) todo with the exact persisted source
  fingerprint is matched before comparing its mutable display deadline. Its
  text/deadline remain untouched across repeated reanalysis. Unedited todos
  still require deadline compatibility, and disambiguated candidates retain
  their own stable fingerprints, so genuinely incompatible candidates remain
  separate.

### Immutable completed-publication outcome

- Root cause: `_completed_outcome` counted `Todo.analysis_version_id`, but
  reconciliation intentionally transfers a stable global todo to the newest
  version. Retrying an older completed publication therefore changed its
  previously returned count.
- RED migration command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_analysis_version_migration.py::test_0006_adds_immutable_publication_counts_and_downgrades -q`
  failed because the outcome columns did not exist.
- GREEN command: the two behavior regressions plus the migration round-trip
  passed `3 passed in 0.42s`.
- GREEN design: publication records card/todo counts on `AnalysisVersion` in
  the same transaction as cards, todos, the current pointer, and terminal
  status. Completed retries read those immutable values. The round-2 migration
  backfill and integrity guard below supersede the original nullable fallback.
- Broader affected-surface check:
  `tests/unit/analysis/test_todo_reconciliation.py`,
  `tests/integration/test_atomic_batch_commit.py`, and
  `tests/integration/test_analysis_version_migration.py` passed
  `34 passed in 1.31s`.

### Formal fix round 1 final verification

- Focused Task 6 command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_todo_reconciliation.py tests/unit/analysis/test_profile_rebuild.py tests/integration/test_atomic_batch_commit.py tests/integration/test_content_api.py tests/integration/test_feedback_and_clear.py tests/integration/test_analysis_version_migration.py -q`
  returned `50 passed in 2.12s`.
- Full backend command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  returned `360 passed in 7.21s`.
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run python -m compileall -q src tests`
  exited 0 with no output.
- `git diff --check` exited 0 with no output.
- `docs/HANDOFF-2026-08-06.md` remains untouched and excluded from this fix.

## Formal review fix round 2

### Migration-time publication outcome freeze

- Root cause: migration 0006 created nullable count columns without populating
  existing completed versions. `_completed_outcome` then fell back to counting
  live `Card.analysis_version_id` and `Todo.analysis_version_id` rows. Those
  associations can be deleted or transferred to a later version, so a
  pre-0006 completed outcome was still mutable after migration.
- RED migration fixture: upgrading a real 0005 completed version left
  `(published_card_count, published_todo_count) == (None, None)` instead of
  `(1, 1)`.
- RED end-to-end command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_analysis_version_migration.py::test_migrated_completed_outcome_stays_fixed_after_later_publication -q`
  failed with the old outcome changing to `card_count == 0` and
  `todo_count == 0` after a later publication transferred its todo and its old
  card was deleted.
- GREEN: migration 0006 now freezes counts for every existing completed
  version. Cards are counted from immutable version-linked card rows at the
  migration snapshot. Todo candidates are preferred as durable version
  provenance; migrated versions without candidates use their migration-time
  linked todo rows. Exact pre-version todo history is unrecoverable, so this is
  the deterministic best-available snapshot.
- GREEN: completed publication reads require both frozen fields and raise a
  data-integrity error if either is absent; there is no mutable live-row
  fallback. New publications continue to record both counts atomically.
- GREEN command: the backfill/downgrade test and the end-to-end mutation test
  passed `2 passed in 0.42s`; the complete migration suite passed
  `8 passed in 0.65s`.
- Downgrade verification removes only the two 0006 columns and preserves the
  completed version's job/batch/provider/model/status plus its unrelated card
  and todo rows and version associations.

### Formal fix round 2 final verification

- Focused Task 6 command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_todo_reconciliation.py tests/unit/analysis/test_profile_rebuild.py tests/integration/test_atomic_batch_commit.py tests/integration/test_content_api.py tests/integration/test_feedback_and_clear.py tests/integration/test_analysis_version_migration.py -q`
  returned `51 passed in 1.92s`.
- Full backend command:
  `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  returned `361 passed in 6.18s`.
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run python -m compileall -q src tests`
  exited 0 with no output.
- `git diff --check` exited 0 with no output.
- `docs/HANDOFF-2026-08-06.md` remains untouched and excluded from this fix.
