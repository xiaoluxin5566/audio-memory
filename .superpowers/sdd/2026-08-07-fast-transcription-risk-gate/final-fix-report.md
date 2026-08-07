# Fast transcription risk gate final-fix report

Date: 2026-08-07
Baseline: `25ebc70`

## Status

Implemented the final-review remediation for both Critical findings, all four
Important findings, and both Minor findings. No real transcription model was
invoked. The protected untracked
`scripts/benchmark-local-transcription.py` remains unmodified and untracked.

## Remediation summary

1. Classification recovery now operates on immutable file-level snapshots.
   Every previously unclassified row in a file is classified from the same
   complete snapshot, and the complete file decision set is persisted in one
   transaction. A mixed legacy/partial file is handled conservatively with
   `classification_context_incomplete` rejection instead of trusting incomplete
   repetition history. High-risk admission is included in that same atomic plan;
   persisted pending work is never refined twice after interruption.
2. Runtime SQLAlchemy and Alembic engines hide bound parameters. The aiosqlite
   DEBUG boundary is held above content-bearing log levels, and both transcription
   orchestration layers now emit only stable diagnostics (`job_id`, diagnostic,
   and `error_type`) without exception strings or tracebacks.
3. Classification accepts the owning file window, rejects head/tail overflow,
   invalid persisted timestamps, blank persisted rows, and every participant in
   an unresolved cross-segment overlap. Persisted rows are copied into a
   non-validating immutable risk input, so corruption reaches `REJECTED` rather
   than failing before classification.
4. Migration `0010` adds `job_files.vad_speech_json`; VAD now persists the raw,
   non-padded speech intervals separately from the padded processing mapping.
   Risk overlap applies the 300 ms grace exactly once, and speech-rate duration
   uses raw VAD occupancy only.
5. Post-edit repetition checks exclude the target UID's old text and count the
   proposed replacement itself. Exact normalized repetitions use a linear index;
   approximate comparisons are bounded to the nearest 256 segments and 512
   normalized characters.
6. The 20% budget is charged from `apply()` entry. Classification/persistence
   time can exhaust admission before any refinement; remaining candidates are
   atomically downgraded and their snapshot text restored. Refinement remains
   one segment per call with word timestamps only on that path.
7. Optional finite `no_speech_prob` and `avg_logprob` values are persisted for
   unsplit fast segments as calibration-only nullable signals. The design and
   evidence documents explicitly gate extremely-short and speaker-instability
   rules on future labeled/stable inputs; no uncalibrated probability threshold
   affects production risk decisions.
8. The offline evaluator now rejects duplicate anonymous `segment_id` values
   with the same non-echoing schema error. Doctor/migration-head checks now use
   `0010`.
9. Final independent review identified two upgrade/failure boundaries. VAD
   availability is now persisted explicitly: a VAD failure keeps valid fallback
   text at 0.6 `vad_unavailable` while timing/blank/conflict hard checks remain
   active. Migration `0010` resets legacy reliable, non-refined decisions for the
   corrected gate and converts legacy `POST_EDIT_PASSED` rows without raw VAD
   evidence to content-free `POST_EDIT_FAILED`, preventing a second refinement.

## TDD evidence

The initial focused RED run produced 13 expected failures: unsupported owning
window, missing conflict rejection, duplicate ID acceptance, dropped probability
signals, missing raw VAD persistence/consumption, partial file commits after
fault injection, persisted-row constructor failure, post-edit under-counting,
classification budget bypass, SQL/log secret leakage, and missing migration
columns. After implementation all 13 passed. A separate mutation check removed
the exact-repeat index: the crowded-window regression failed (`state=None`), then
passed again after restoring the index.

## Verification

- Focused risk/migration/engine suite: `136 passed` before the final exact-index
  regression; final backend full suite includes that test.
- Backend full suite: `530 passed in 14.04s`.
- Prototype suite: `43 passed`; production `vite build` and Sites preparation
  succeeded. The first sandboxed attempt could not bind a loopback test port;
  the approved local rerun completed successfully.
- Doctor/migration e2e subset: `24 passed`.
- `git diff --check`: clean.
- Core doctor against the actual local app data passed model manifests,
  migration-chain semantics, recovery imports, security, and prompt resources.
  It reported the managed sandbox's user data directory as unwritable and the
  existing local database as still at pre-`0010`; no user database was mutated.

## Residual considerations

- Applying migration `0010` through the normal install/start migration path is
  required before running the updated app against an existing local database.
- Missing raw VAD is distinguished from confirmed no-speech: unavailable VAD
  downgrades otherwise valid text to 0.6, while confirmed empty raw VAD still
  applies the hard `no_vad_support` rule.
- Probability and speaker-stability thresholds remain deliberately disabled
  until unique, de-identified labeled evidence meets the documented gate.
