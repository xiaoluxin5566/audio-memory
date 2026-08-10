# Local Fast V0.1 DeepSeek Acceptance Evidence

**Date:** 2026-08-10  
**Scope:** Stage 1 analysis-only retry; no local transcription stages  
**Result:** Completed, pending user page acceptance

## Frozen source snapshot

| Field | Aggregate value |
|---|---:|
| Source job | `d29475e4-f148-4b99-9b7e-1e5751da1e48` |
| Total transcript rows | 4,117 |
| Reliable rows used by analysis | 3,442 |
| Discarded unreliable rows | 675 |
| Reliable text characters | 28,470 |
| Prior failed version retained | `c65e86d7-5dc7-401f-90e0-96d92b01e866` |
| Prior failure code retained | `model_analysis_failed` |

The analysis-only retry did not run VAD, Whisper, the risk gate, selective
refinement, or diarization. Their call counts were all zero. The source row
counts and reliable text character count were unchanged after completion.

## Frozen DeepSeek contract

| Field | Value |
|---|---|
| Provider | DeepSeek |
| Model | `deepseek-v4-flash` |
| Parameter fingerprint | `9ea1c86908a19a5f3c517c68035545ec1c9418196c88bb013d369a613cd4fae3` |
| Thinking | disabled |
| Temperature | 0 |
| Response format | JSON object |
| Event-map output / timeout | 32,768 tokens / 180 seconds |
| Scene output / timeout | 16,384 tokens / 120 seconds |
| Profile output / timeout | 8,192 tokens / 120 seconds |
| Scene concurrency | 1 |
| Transient retry bound | at most one extra attempt |
| Schema repair bound | at most one repair attempt |

The pipeline requires eight logical requests: one event map, six serialized
scenes, and one profile extraction. All eight logical stages completed. Exact
HTTP attempt count, request/response byte totals, input/output token totals,
finish reasons, repair flags, and per-request elapsed times were held by the
running worker but were not exported by the production logger used for this
run. They are therefore deliberately recorded as unavailable rather than
estimated. A tested logger-routing correction is included after this run so a
future full-chain acceptance can capture those aggregate-only fields without
persisting request or response content. No second paid analysis retry was made
to reconstruct missing telemetry.

## Coverage and publication

| Field | Result |
|---|---:|
| New analysis version | `fa0c5b48-b2c6-445b-ae91-0b78d5ffc7f6` |
| Final version status | `completed` |
| Final job status | `completed` |
| Final error code | empty |
| Total elapsed | 252.806 seconds |
| Event-map JSON bytes | 55,520 |
| Event count | 1 |
| Assigned reliable segments | 2,352 |
| Server-completed unassigned segments | 1,090 |
| Assigned/unassigned overlap | 0 |
| Unknown evidence references | 0 |
| Coverage union | 3,442 / 3,442 |
| Completed scene keys | 6 / 6 |
| Profile candidates | 0 |
| Published cards | 0 |
| Published todos | 0 |

The zero-card and zero-todo result is a successful atomic publication with no
items that passed the strict evidence rules, not a pipeline failure. Because
there are no published items, evidence playback has no entry to render for this
run. No partial card or todo batch was published before all required stages
completed.

## Page verification

The local product page was opened in the app browser and left on Audio History
for user acceptance. The completed audio appears in local history, the active
provider is `deepseek-v4-flash`, and the page emitted no console errors. The
feed empty state matches the zero-card and zero-todo publication above.

## Code evidence

Stage 1 implementation commits through the formal run:

- `ee8e107 fix: bound deepseek analysis requests`
- `a9b9427 fix: complete event map coverage locally`
- `3640710 fix: surface specific analysis failures`
- `d4eb487 test: verify deepseek analysis recovery`
- `ec5817e chore: log safe deepseek acceptance metrics`
- `1a9318b fix: route analysis metrics to server log`

No database, audio, transcript content, browser profile, screenshot, API key,
request body, or provider response body is included in this report or in Git.
