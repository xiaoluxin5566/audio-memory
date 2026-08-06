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

Task 5 may leave durable compatibility markers `paused_credential_changed`, `paused_rules_changed`, or `paused_error`; the Task 8 API normalizes them to public `paused`. Resume behavior is deliberate:

- `paused_credential_changed`: validate the original provider/model, adopt the new durable generation after explicit user resume, delete the unpublished old version/checkpoints, and return the item to `pending`;
- `paused_rules_changed`: reject resume and require stop plus a fresh preview, because old fixed-rule bodies were never stored as replayable Prompt documents;
- account/auth/balance/Keychain/rate-limit conditions: keep the item pending and batch paused;
- ordinary model/schema/content failure: mark only that item failed and continue later items.

Network/timeout and provider-5xx failures retain the existing provider client's maximum of three total attempts (initial request plus at most two retries). JSON/Schema repair remains exactly one repair request through `request_with_one_repair`.

## Scheduling, stop, and restart semantics

- Creation persists only `ReanalysisBatch` and newest-first `ReanalysisItem` rows. `ReanalysisWorker` feeds at most one item into `AnalysisTaskCoordinator` at a time.
- History versions use priority 10; new-upload model work remains priority 0. The coordinator's unique active-source constraints and global worker lease/fence remain authoritative.
- `stopping` is now a coordinator-excluded state. A stop request never interrupts the current provider request; it waits for that source item to become terminal, then marks every unstarted item/version stopped. No pending remote version from a stopped/stopping batch can be claimed.
- Startup recovery runs while the single-instance lock is held and before the global coordinator starts. It returns reanalysis-owned running versions/items to pending without changing their frozen Prompt/profile/provider snapshot or version ID. Stale running items with missing/terminal versions are repaired to pending/succeeded/failed as appropriate.
- Before scheduling after startup, the saved provider must be available. Unavailable provider state persists the batch as paused.
- A compatible EventMap is seeded only when the current source version persists and matches the exact system/event-map/common Prompt hashes, schema-version hash, actual EventMap/six-scene JSON-Schema hash, and versioned structured-transcript fingerprint. The fingerprint covers ordered file identity/position/recording metadata/speech mapping and every segment's UID, speaker, timing, text, and word JSON. The EventMap must also parse against the current strict Schema, match its stored content hash, and have assigned plus unassigned evidence IDs exactly equal the current structured-transcript segment IDs. Legacy versions without this metadata safely regenerate EventMap.
- Successful content publication remains Task 6's atomic current-version swap. Failed items leave the old current version untouched.
- All scenes use the one frozen pre-batch profile JSON. After content items become terminal, the worker invokes one profile-only rebuild/swap. Failure leaves `content_completed_profile_failed`; `/retry-profile` calls only `VersionPublisher.retry_profile` and creates no AnalysisVersion/model work.
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
16. Between-check submission race: changing the actual Schema hash after Worker validation initially inserted a version and left the batch running; the coordinator transaction now persists `paused_rules_changed` plus `analysis_schema_changed` and inserts zero versions.

Each RED was followed by a focused GREEN before the next behavior was implemented.

## Verification results

- Focused Task 8 suite: `pytest tests/unit/reanalysis tests/integration/test_reanalysis_api.py tests/integration/test_reanalysis_worker.py -q` — 29 passed in 2.21s.
- Coordinator/clear-history regression selection: 20 passed in 1.09s.
- Full backend suite: 431 passed in 8.08s.
- Python compile check for production and Task 8 tests: exit 0.
- `git diff --check`: exit 0.
- No real provider keys or external model calls were used.

## Concerns / deliberate decisions

- Credential-changed resume updates `credential_generation` after explicit user action while retaining the original preview `snapshot_hash` as the confirmation audit hash. The durable batch generation field is the execution authority after resume.
- Fixed-rule-changed batches cannot resume in place; users must stop and create from a fresh preview. This avoids silently reconstructing a snapshot from fixed-rule bodies that Task 5 did not persist.
- Structured transcripts have no separate version column, so compatibility persists a canonical versioned content fingerprint rather than assuming immutability. Exact current evidence-segment coverage is an additional independent requirement.
- Rate-limit cooldown expiry is durable but not autonomously polled while a batch is paused. The user can resume after cooldown; resume revalidates the saved provider before any further paid work. Worker exceptions are now logged with a normalized operation label instead of being silently suppressed.
- No database migration was required: Task 5 already introduced all ReanalysisBatch/ReanalysisItem ownership and queue fields consumed here.
