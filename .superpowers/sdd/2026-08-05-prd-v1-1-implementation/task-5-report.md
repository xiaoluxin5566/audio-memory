# Task 5 implementation report

## Status

Implemented the durable single-worker analysis queue, event-map-first strict
runner, immutable provider/prompt/profile snapshots, credential-generation
checkpoints, exact-one repair behavior, and application wiring. The old direct
`AnalysisOrchestrator`, `PromptComposer.compose`, legacy `SceneResult`, and the
legacy parser path are retired rather than kept alongside the strict path.

## RED / GREEN evidence by behavior

1. Durable priority, source exclusion, restart recovery, and stopped history:
   - RED: `tests/unit/analysis/test_task_coordinator.py` initially failed during
     collection with `ModuleNotFoundError: audio_memory.analysis.task_coordinator`.
   - GREEN: priority 0 wins over priority 10; a source cannot have a second
     pending/running version; a new coordinator converts abandoned `running`
     rows to `pending`; a stopped history batch leaves its item pending and
     yields nothing. The retry-stage test first observed `failed` instead of
     `analyzing`, then passed after submission made the job state authoritative.
   - Fixed-rule hash RED: two different user prompt snapshots produced different
     hashes. GREEN: the hash is now derived only from packaged `system.md`,
     `event-map.md`, and `common-scene.md`.
2. Persistent queue migration and checkpoint object shape:
   - RED: head migration query failed with `no such column: priority`.
   - GREEN: migration `0004` adds priority and the active-source partial unique
     index, and normalizes null/blank/array checkpoint payloads in both
     `analysis_jobs` and `analysis_versions` to `{}`.
3. Provider state plus credential generation snapshot:
   - RED: provider test failed with `AttributeError` for
     `snapshot_active_with_generation`.
   - GREEN: the method returns `(ProviderState, generation)` while holding both
     activation and state locks; credential replacement increments generation
     under the state lock.
4. Event-map-first runner and resumable checkpoints:
   - RED: event pipeline collection failed with
     `ModuleNotFoundError: audio_memory.analysis.runner`.
   - GREEN: the runner loads structured transcript rows, generates/checkpoints
     `EventMap`, calls six strict scene schemas in frozen order, validates every
     result with `validate_evidence_integrity`, saves each completed scene
     immediately, and resumes without repeating event map or completed scenes.
5. Credential change during a version:
   - RED was part of the missing-runner collection failure.
   - GREEN: a generation change after a real model step raises
     `CredentialChangedError`, marks the version `credential_changed`, clears
     unpublished scene checkpoints, marks a new-upload job failed, and pauses an
     owning history batch as `paused_credential_changed`.
6. Exactly one repair for invalid JSON/schema:
   - RED: real HTTP-boundary tests failed because `RemoteSceneAnalyzer` had no
     strict event-map/scene methods; profile extraction raised immediately on
     malformed JSON.
   - GREEN: event map, scene, and profile outputs each make one repair request
     after the initial invalid response. A second invalid response is returned
     as an error with exactly two total requests and no repair loop.
7. Real request capture and prompt injection:
   - RED: strict provider methods did not exist, so no captured request could be
     produced.
   - GREEN: `httpx.MockTransport` captures the actual JSON sent by
     `ProviderAnalysisClient`. Full-width closing-tag injection and untagged
     “Ignore all previous instructions” text remain inside the editable/data
     layers; the fixed-layer order and single real closing tag are preserved.
     Captured JSON contains no temporary audio path or credential bytes.
8. One active remote model request globally:
   - RED: concurrent calls reached the real mock transport together
     (`maximum_active == 2`).
   - GREEN: the shared `ProviderAnalysisClient` serializes outbound calls;
     analysis and card Q&A cannot overlap on the wire (`maximum_active == 1`).
9. Profile candidates:
   - RED: after a completed runner pass the version had zero
     `ProfileCandidate` rows.
   - GREEN: evidence-backed candidates are replaced idempotently per version,
     restricted to known structured segment IDs, and stored with explicit or
     inferred origin before publication.
10. Atomic strict-path cutover and frontend boundary:
    - RED: after deliberately removing the old orchestrator symbol, application
      import failed until the startup wiring was switched.
    - GREEN: startup constructs `AnalysisRunner` and
      `AnalysisTaskCoordinator`; uploads enter priority 0 only after Whisper;
      model retry submits a new version without invoking Whisper. The active
      publisher calls `model_dump_for_frontend()` before serializing any strict
      scene for frontend consumption.

## Exact verification commands and results

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_task_coordinator.py tests/integration/test_event_map_pipeline.py tests/integration/test_analysis_pipeline.py -q`
  - `19 passed in 1.09s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `303 passed in 4.95s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run python -m compileall -q src tests`
  - exit 0, no output
- `git diff --check`
  - exit 0, no output

An earlier verification run found one extra EOF blank line in
`prompts/schemas.py`; after correcting it, the entire gate above was rerun from
scratch.

## Produced and changed interfaces

- `AnalysisRequest(source_job_id, source_batch_id, provider_id, model_id,
  credential_generation, prompt_snapshot, profile_snapshot, priority)` is a
  frozen dataclass.
- `AnalysisTaskCoordinator.submit_new_upload(request)`,
  `submit_reanalysis(request)`, and `next_request()` use SQLite
  `AnalysisVersion` rows as the sole queue authority. `start(runner)` owns one
  worker; the async condition only wakes it.
- `AnalysisRunner.run(version_id) -> AnalysisOutcome` is the only production
  analysis path.
- `ProviderStateCoordinator.snapshot_active_with_generation() ->
  tuple[ProviderState, int]` and `credential_generation(provider_id)` bind and
  check key generation.
- `RemoteSceneAnalyzer.analyze_event_map()` and `analyze_scene()` consume
  `ModelRequest.rendered_instructions` and strict parsers.
- `parse_event_map_output()` and `parse_scene_output()` validate `EventMap` and
  `StrictSceneResult`; the latter no longer returns a legacy model.
- `PromptComposer.fixed_rules_hash()` provides the packaged fixed-rule identity.

## Migration and compatibility decisions

- `AnalysisVersion` is both version record and durable queue record. Adding a
  second queue table would create two authorities for status, snapshots, and
  recovery.
- Priority defaults to 10 for existing versions; new upload submission enforces
  0 and history submission enforces 10.
- The new partial unique index covers both `pending` and `running`, while the
  existing running-only index remains compatible with the prior migration.
- Legacy `staged_results_json='[]'`, blank, and null values are migrated to the
  same object shape used by fresh schemas. The remaining pre-version
  `BatchRepository` stores compatibility cards under the explicit
  `legacy_cards` object key.
- No compatibility adapter remains for `PromptComposer.compose`, legacy
  `SceneResult`, or direct `AnalysisOrchestrator`. This prevents mixed schemas
  when the new worker is active.
- API retry takes a fresh provider/model/generation and prompt/profile snapshot,
  but does not invoke transcription. New uploads retain the snapshot captured
  before the local transcription task and are queued only after it completes.

## Self-review

- Database/coordinator locks end before `runner.run()` and all network calls.
- All transcript data sent remotely is structured text metadata; no audio bytes
  or local audio path are included.
- Provider/model/prompt/profile/generation values come from the version snapshot,
  never from current UI selection during the run.
- Event map and scenes are revalidated when reused from checkpoints.
- A provider switch without a credential change does not affect the in-flight
  version; a key-generation change invalidates its unpublished scene state.
- Profile candidate writes are idempotent per version and precede publication.
- `docs/HANDOFF-2026-08-06.md` was not modified, staged, deleted, or committed.

## Independent review closure

An independent diff review of `7bb67b8..9d148d2` found four Important
lifecycle issues. Each received a new failing behavior test before the fix:

1. Cancellation RED marked a normally cancelled runner `failed`; GREEN now
   re-raises `CancelledError` while leaving the version `running`, so startup
   recovery returns it to `pending`.
2. Profile evidence RED showed an unknown evidence ID was omitted from
   `ProfileCandidate` but still reached the publisher; GREEN now derives the
   publishable profile delta only from the evidence-verified candidate set.
3. Cross-coordinator claim review found the selection/update was not an
   explicit database compare-and-set. GREEN uses `UPDATE ... WHERE status =
   'pending'` and accepts the claim only when exactly one row changed. The app's
   existing `InstanceLock` prevents two backend processes; the conditional
   claim additionally protects multiple coordinator instances.
4. History terminal RED left `ReanalysisItem.status='running'`; GREEN completes
   the item/batch on success, marks item/batch failure state on errors, and
   returns the item to pending while pausing the batch on credential change.

After these fixes the complete verification gate was rerun, producing the
19/19 focused and 303/303 full-suite results above.

## Concerns / deferred boundary

- Full version-replacement publication semantics (version-linked Card/Todo
  replacement, tombstone merge, and current-version filtering) remain the
  planned Task 6 responsibility. Task 5 keeps the existing publisher boundary,
  but makes its strict frontend serialization safe by requiring
  `model_dump_for_frontend()`.
- History-batch creation/API feeding is outside Task 5. The coordinator consumes
  persisted `ReanalysisItem` ownership, priority, stopped/paused state, and
  credential pause correctly when those items are submitted.

## Formal review fix round 1

The seven Important findings from the formal lifecycle review were each closed
with executable regression coverage:

1. Durable credential generation and startup ordering:
   - RED: a replacement reached generation 1 in memory, but a newly constructed
     coordinator restored generation 0.
   - GREEN: `provider_metadata.credential_generation` is migrated with a
     non-null zero default, replacement persists each increment, initialization
     restores it, and application startup awaits provider initialization before
     starting queue recovery/the worker.
2. Generation/publication and provider-error races:
   - RED: a provider error after credential replacement escaped as the generic
     provider failure, and a change visible only at final publication still
     called the publisher. A separate lock-order test showed physical keychain
     replacement could begin while the publication guard was held.
   - GREEN: provider errors recheck generation before failure classification;
     the final generation check and publication run under the provider state
     guard; physical key replacement, in-memory generation increment, and
     durable generation update now share that same guard.
3. Idempotent, terminal publication:
   - RED: the publisher accepted a job ID, so retrying by version failed with
     `LookupError` and could not prove version idempotency.
   - GREEN: publication is keyed by `AnalysisVersion.id`; batch/card/todo/profile
     identifiers are deterministic; result rows, current-version pointer,
     version/job/history terminal state, and snapshot provider/model metadata
     commit in one transaction. Retry after an audio move but before the
     database commit reconciles the deterministic destination instead of
     duplicating or losing publication.
4. History reanalysis ownership:
   - RED: missing batch IDs and a batch belonging to another source job were
     both accepted.
   - GREEN: submission requires a pending `ReanalysisItem` owned by an active
     `ReanalysisBatch`, matching source job, provider, model, and credential
     generation.
5. Provider-switch publication metadata:
   - RED was covered by the version-id publication test: the old publisher read
     mutable job metadata at publish time.
   - GREEN: provider/model written to `Batch` and `AnalysisJob` come only from
     the immutable `AnalysisVersion` snapshot.
6. Live-worker ownership:
   - RED: initializing a second coordinator reset every `running` version to
     pending and could steal live work.
   - GREEN: claims carry an owner UUID and expiring lease, heartbeat renewal is
     conditional on owner/status, startup recovers only missing/expired leases,
     and orderly close releases only the coordinator's own claims.
7. Fixed-rule resume safety:
   - RED: a running version with a stale `fixed_rules_hash` reused its event map
     and staged scenes.
   - GREEN: the runner checks packaged fixed rules before transcript or remote
     work, clears incompatible checkpoints, marks the version/job explicitly,
     and pauses history ownership for a fresh snapshot.

Migration `0005` adds the durable provider generation and worker lease columns;
its focused test exercises upgrade from `0002`, legacy-row backfill, and
downgrade to `0004`.

Fresh final verification from the completed fix-round worktree:

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `314 passed in 4.73s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run python -m compileall -q src tests`
  - exit 0, no output
- `git diff --check`
  - exit 0, no output

## Formal review follow-up: ownership and fencing

Independent review of `7c76f5e..cee7869` found three additional Important
edge cases. They were closed with a second RED/GREEN cycle:

1. Complete immutable history snapshot:
   - RED: `test_reanalysis_must_match_the_owning_run_snapshot` failed with
     `DID NOT RAISE ValueError`; provider/model/generation matched, but altered
     prompt/profile/fixed-rule inputs were still accepted.
   - GREEN: history submission semantically compares prompt and profile
     snapshots and requires the current fixed-rules hash to equal the owning
     `ReanalysisBatch`, in the same transaction as ownership validation.
2. All terminal pause states block claiming:
   - RED: parameter cases `paused_rules_changed` and `paused_error` both failed
     with `DID NOT RAISE TimeoutError`, proving pending sibling work remained
     claimable.
   - GREEN: both states join the existing stopped/cancelled/credential-change
     states in `_PAUSED_HISTORY_STATES`; the three-case regression now leaves
     each version pending without yielding remote work.
3. Lease fencing after reclaim:
   - RED: fencing tests could not import `LeaseLostError`, reflecting that the
     runner/publisher had no owner token to verify after a reclaim.
   - GREEN: the coordinator passes its owner UUID into the runner; ownership is
     rechecked before and after remote calls and enforced by conditional writes
     for event maps, scene checkpoints, profile candidates, failure handling,
     and publication. The publisher acquires a conditional database write fence
     before its terminal transaction, so a stale owner cannot publish after a
     new owner reclaims the version. Worker cleanup also retains a live owner on
     cancellation until orderly `close()` returns its claim to pending.
4. Credential/rule terminal-transition fencing:
   - RED: `test_stale_worker_cannot_mark_credential_changed` raised
     `CredentialChangedError` and overwrote the new owner; the fixed-rules
     regression failed because `_require_fixed_rules` accepted no owner token.
   - GREEN: both terminal transitions now acquire an owner-qualified
     `running`-state write fence before clearing checkpoints or changing
     job/history state. A stale owner receives `LeaseLostError` and leaves the
     replacement worker's version untouched.

Targeted follow-up GREEN:

- immutable snapshot + three pause cases + runner/publisher fencing:
  - `6 passed in 0.75s`
- full Task 5 focused regression set:
  - `56 passed in 3.01s`

Final verification after the follow-up (superseding earlier intermediate suite
counts):

- migration suite:
  - `6 passed in 0.62s`
- full Task 5 focused regression set:
  - `58 passed in 2.93s`
- complete backend suite:
  - `321 passed in 5.15s`
- `python -m compileall -q src tests`:
  - exit 0, no output
- `git diff --check`:
  - exit 0, no output

## Formal review fix round 2

The scoped re-review left two Important blockers. Both were closed with focused
failure-first coverage:

1. Durable generation before physical credential replacement:
   - RED: the order test observed `physical` before `durable:1`; a metadata
     persistence exception left the replacement key installed; and a keychain
     replacement exception left generation 0.
   - GREEN: while holding the provider state/publication guard, the coordinator
     first commits the next generation, then advances the in-memory generation,
     and only then calls physical key replacement. Persistence failure therefore
     preserves the old key and old generation. Replacement or confirmation
     failure retains the already-durable higher generation, conservatively
     invalidating in-flight work and ensuring a changed key can never remain on
     the old generation.
2. Generic remote-output failure after credential replacement:
   - RED: a second invalid strict output raised `SceneOutputError` and the
     generic path marked the history version failed instead of recognizing the
     new credential generation.
   - GREEN: before generic failure handling, the runner now rechecks worker
     ownership and credential generation. Credential change wins over
     `SceneOutputError`/Pydantic validation failures, clears unpublished scene
     checkpoints, returns the history item to pending, and pauses its batch as
     `paused_credential_changed`.

Targeted RED was `4 failed`; the same four tests passed in `0.33s` after the
fix. A dedicated failed-confirmation regression also passed (`1 passed in
0.19s`).

### Final-triage ledger (non-blocking observations)

- Lease recovery/release can leave a `ReanalysisItem` marked `running` until a
  later claim reconciles it. This was outside the two scoped blockers and needs
  an explicit lifecycle decision in final triage.
- Retry-analysis accepted-state validation currently excludes
  `credential_changed` and `fixed_rules_changed`. This was outside the scoped
  blockers and should be reconciled with the intended retry UX in final triage.

The final verification results below supersede intermediate counts above.

- migration suite:
  - `6 passed in 0.60s`
- full Task 5 focused regression set:
  - `63 passed in 2.98s`
- complete backend suite:
  - `326 passed in 5.25s`
- `python -m compileall -q src tests`:
  - exit 0, no output
- `git diff --check`:
  - exit 0, no output
