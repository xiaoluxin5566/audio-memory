# Task 8 Implementation Report

## Outcome

Implemented history-reanalysis preview, immutable confirmation, batch/current/control APIs, durable background feeding, stop/resume/restart behavior, provider-aware pause handling, profile-only retry, and clear-history exclusion.

The implementation reuses Task 5's single SQLite-backed `AnalysisTaskCoordinator` and priority/fencing rules, Task 6's atomic `VersionPublisher` and deterministic profile rebuild, and Task 7's local session/Host/Origin/idempotency middleware.

## Snapshot threat model

Protected decisions are the paid-work scope and model configuration the user saw in preview:

- ordered completed source Batch IDs (newest first), their owning job IDs, file counts, transcript-character counts, and aggregate counts;
- scope `all_completed_history`;
- provider ID, model ID, and durable credential generation (never the API key);
- all six full Prompt contents in the persisted batch snapshot, with version and SHA-256 summaries in the signed payload;
- individual system/event-map/common fixed-rule hashes, combined fixed-rule hash, numeric schema version, and a hash of the actual EventMap/six-scene JSON Schemas;
- frozen active-profile content and its canonical SHA-256;
- estimated call range.

`PreviewSigner` uses a process-random 256-bit HMAC secret by default, canonical UTF-8 JSON, URL-safe base64, constant-time signature comparison, and a five-minute expiry. A process restart intentionally invalidates outstanding preview tokens. Creation validates the signature/expiry, performs one saved-provider validation, then enters three coordinated fences: all Prompt scene locks, the provider coordinator's active-provider/model/generation guard, and SQLite `BEGIN IMMEDIATE`. It rereads all history/transcript/profile rows and Prompt/provider bindings under those fences, compares every signed field, inserts the batch/items, and commits before releasing any fence. Forged, expired, Prompt-changed, generation-changed, history-scope-changed, profile-changed, transcript-changed, schema-changed, and fixed-rule-changed confirmations therefore cannot create work. A concurrency regression test starts Prompt and ProfileFact mutations after the protected reread; both remain blocked through commit and the batch stores only the confirmed snapshot.

Paid/destructive mutations are all ordinary FastAPI mutations under `LocalWebSecurityMiddleware`; consequently create/stop/resume/retry-profile require exact local Host/Origin, a valid random local-session token, and an idempotency key. Task 7 stores/replays the complete response and rejects key reuse with different input. No route accepts an audio upload or API credential.

The process-local Prompt/provider fences are valid because the application already enforces one backend process with its instance lock. SQLite's immediate writer transaction fences all database-backed scope, transcript, and profile authorities through the same commit.

## Persistent state machine

Public batch states are exactly:

`pending → running → completed | completed_with_failures | content_completed_profile_failed`

`pending | running | paused → stopping → stopped`

`paused | stopped → running` after explicit resume validation.

Item states are exactly `pending | running | succeeded | failed | stopped`.

New writes persist only the exact public `paused` state. The reason remains durable on the owning item/version `error_code`, and resume derives its rule/credential/provider behavior from that reason. Migration 0007 normalizes legacy `paused_credential_changed`, `paused_rules_changed`, and `paused_error` rows; read paths retain compatibility while an old database is being upgraded. Resume behavior is deliberate:

- `credential_changed`: validate the original provider/model, adopt the new durable generation after explicit user resume, delete the unpublished old version/checkpoints, and return the item to `pending`;
- `fixed_rules_changed`, `analysis_schema_changed`, or `transcript_changed`: reject resume and require stop plus a fresh preview;
- account/auth/balance/Keychain/rate-limit conditions: keep the item pending and batch paused;
- ordinary model/schema/content failure: mark only that item failed and continue later items.

Network/timeout and provider-5xx failures retain the existing provider client's maximum of three total attempts (initial request plus at most two retries). JSON/Schema repair remains exactly one repair request through `request_with_one_repair`.

## Scheduling, stop, and restart semantics

- Creation persists only `ReanalysisBatch` and newest-first `ReanalysisItem` rows. `ReanalysisWorker` feeds at most one item into `AnalysisTaskCoordinator` at a time.
- History versions use priority 10; new-upload model work remains priority 0. The coordinator's unique active-source constraints and global worker lease/fence remain authoritative.
- `stopping` is now a coordinator-excluded state. A stop request never interrupts the current provider request; it waits for that source item to become terminal, then marks every unstarted item/version stopped. No pending remote version from a stopped/stopping batch can be claimed.
- Startup recovery runs while the single-instance lock is held and before the global coordinator starts. Production initialization explicitly reclaims every foreign predecessor lease, including unexpired ordinary-upload leases, because the instance lock proves that prior process is dead. The coordinator's default remains expiry-only so two live coordinators cannot steal each other's work. Reanalysis-owned running versions/items return to pending without changing their frozen snapshot or version ID; stale items with missing/terminal versions are repaired appropriately.
- Before scheduling after startup, the saved provider must be available. Unavailable provider state persists the batch as paused.
- A compatible EventMap is seeded only when the current source version persists and matches the exact system/event-map/common Prompt hashes, schema-version hash, actual EventMap/six-scene JSON-Schema hash, frozen-profile canonical hash, and versioned structured-transcript fingerprint. The fingerprint covers ordered file identity/position/recording metadata/speech mapping and every segment's UID, speaker, timing, text, and word JSON. The EventMap must also parse against the current strict Schema, match its stored content hash, and have assigned plus unassigned evidence IDs exactly equal the current structured-transcript segment IDs. A profile mismatch merely regenerates EventMap; it does not pause. Legacy versions without compatibility metadata also safely regenerate.
- Successful content publication remains Task 6's atomic current-version swap. Failed items leave the old current version untouched.
- All scenes use the one frozen pre-batch profile JSON. Final content publication atomically checkpoints `content_completed_profile_failed` and never rebuilds profile inside `VersionPublisher.publish`. The worker observes that checkpoint and invokes the sole automatic profile rebuild/swap under the shared profile/maintenance guard. Failure keeps the checkpoint for explicit `/retry-profile`, which creates no AnalysisVersion/model work. A blocking race test proves clear-history cannot overlap the automatic rebuild.
- Clear history and the global analysis coordinator share one maintenance fence. Queue insert/claim, profile-only rebuild, and history deletion cannot cross. Clear rejects any pending/running ordinary or history AnalysisVersion, every active/paused/stopping reanalysis batch, and any profile rebuild that overlaps the request; it holds the fence through both database and audio/staging filesystem cleanup.

## No-Whisper / no-duplication proof

- Production reanalysis modules depend on persisted `Batch`, `JobFile`, `Transcript`, `AnalysisVersion`, Prompt/schema, publisher, and coordinator interfaces. They do not import or call transcription, Whisper, diarization, upload, file-copy, or audio-move code.
- Preview reports `whisper_calls=0` and `diarization_calls=0`.
- Worker integration tests capture `JobFile` and `Transcript` row counts before/after scheduling and prove they remain unchanged while a new history AnalysisVersion is created.
- History items reference existing source Batch/job/transcript IDs. Only Task 6's first-publication path can move staged audio; reanalysis versions already have a source Batch, so that path is not entered.

## TDD evidence

Observed RED before each production slice:

1. Preview package absent: 2 failed with `ModuleNotFoundError: audio_memory.reanalysis`.
2. Batch service absent: 5 failed with `ModuleNotFoundError: audio_memory.reanalysis.service` for creation and four independent snapshot mutations.
3. API router absent: 2 failed with `ModuleNotFoundError: audio_memory.api.reanalysis`.
4. Worker/control layer absent: 6 failed (missing worker, resume, retry-profile).
5. Control routes absent: stop endpoint returned 404.
6. Provider classification absent: 2 failed because `ProviderAnalysisError` did not accept normalized code/pause semantics.
7. Clear-history exclusion absent: running and paused cases returned 204 instead of 409.
8. Production lifecycle wiring absent: app state had no `reanalysis_service`.
9. Stale-item recovery absent: stale `running` item remained running.
10. No-active-provider preview absent: preview raised `LookupError` instead of returning counts/blocker.
11. Actual Schema hash binding absent: signed fixed-rule payload lacked `analysis_schemas`.
12. In-flight failure after stop: the runner overwrote `stopping` with `running`/`paused`; the regression now proves an ordinary provider failure leaves the first item failed, transitions the batch to stopped, and never schedules the remaining item.
13. Persisted EventMap compatibility: same segment IDs with changed text and changed actual Schema hashes were initially reusable; both now pause safely before submission, and legacy metadata forces regeneration.
14. Atomic confirmation: Prompt and ProfileFact mutations launched after the protected reread now block until the batch transaction commits.
15. Clear coordination: ordinary pending/running versions were deleted and a profile-only retry could race cleanup; both cases were reproduced before the shared maintenance fence.
16. Between-check submission race: changing the actual Schema hash after Worker validation initially inserted a version and left the batch running; the coordinator transaction now persists `paused` plus item reason `analysis_schema_changed` and inserts zero versions.
17. Automatic final profile rebuild: `VersionPublisher.publish` entered the rebuilder outside the shared guard; it now performs zero rebuild calls, checkpoints content completion, and the guarded Worker performs exactly one. A clear-history race waits and returns conflict.
18. Identical-content Prompt save: version increment with unchanged SHA-256 initially accepted an old preview; signed canonical Prompt bindings now include both version and hash.
19. Profile-sensitive EventMap: a prior EventMap built with a different frozen profile was initially reused; it now regenerates without pausing.
20. Fast ordinary-upload restart: an unexpired foreign lease initially remained permanently running; instance-lock startup recovery now reclaims and claims it, while the default live-coordinator tests still fence it.
21. Exact paused state: schema/rule pauses initially persisted `paused_rules_changed`; every writer now persists `paused`, reason-driven resume remains intact, and migration 0007 normalizes all three legacy values.
22. No-provider create: confirmation of a blocked preview initially raised `AttributeError`; it now raises `PreviewBlockedError(["no_active_provider"])`, which the API maps to documented HTTP 409.

Each RED was followed by a focused GREEN before the next behavior was implemented.

## Verification results

- Required Task 8 suite (`tests/unit/reanalysis`, API, Worker): 33 passed in 3.58s.
- Focused fix-round selection (reanalysis, publisher, EventMap, coordinator, migrations): 90 passed in 5.94s.
- Full backend suite: 438 passed in 11.17s.
- Python compile check for production and Task 8 tests: exit 0.
- `git diff --check`: exit 0.
- No real provider keys or external model calls were used.

## Concerns / deliberate decisions

- Credential-changed resume updates `credential_generation` after explicit user action while retaining the original preview `snapshot_hash` as the confirmation audit hash. The durable batch generation field is the execution authority after resume.
- Fixed-rule-changed batches cannot resume in place; users must stop and create from a fresh preview. This avoids silently reconstructing a snapshot from fixed-rule bodies that Task 5 did not persist.
- Structured transcripts have no separate version column, so compatibility persists a canonical versioned content fingerprint rather than assuming immutability. Exact current evidence-segment coverage is an additional independent requirement.
- Rate-limit cooldown expiry is durable but not autonomously polled while a batch is paused. The user can resume after cooldown; resume revalidates the saved provider before any further paid work. Worker exceptions are now logged with a normalized operation label instead of being silently suppressed.
- Migration 0007 is data-only: it normalizes legacy extended pause-status values without adding schema fields; pause reasons remain on existing error-code columns.
