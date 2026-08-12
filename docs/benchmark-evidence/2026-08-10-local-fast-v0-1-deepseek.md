# Local Fast V0.1 DeepSeek Acceptance Evidence

**Date:** 2026-08-10  
**Scope:** Stage 1 analysis-only retry; no local transcription stages  
**Result:** Completed, pending user page acceptance

## Frozen source snapshot

| Field | Aggregate value |
|---|---:|
| Total transcript rows | 4,117 |
| Reliable rows used by analysis | 3,442 |
| Discarded unreliable rows | 675 |
| Reliable text characters | 28,470 |
| Analysis windows | 23 |

The analysis-only retry did not run VAD, Whisper, the risk gate, selective
refinement, or diarization. Their call counts were all zero. The source row
counts and reliable text character count were unchanged after completion.

## Frozen DeepSeek contract

| Field | Value |
|---|---|
| Provider | DeepSeek |
| Model | `deepseek-v4-flash` |
| Thinking | disabled |
| Temperature | 0 |
| Response format | JSON object |
| Event-map output / timeout | 32,768 tokens / 180 seconds |
| Scene output / timeout | 16,384 tokens / 120 seconds |
| Profile output / timeout | 8,192 tokens / 120 seconds |
| Event-map concurrency | 1 |
| Scene concurrency | 1 |
| Transient retry bound | at most one extra attempt |
| Schema repair bound | at most one repair attempt |

The preview estimated 29–60 calls: 23 event windows plus six scene calls at
minimum, with one schema-repair allowance per stage and one optional profile
stage at maximum. Exact HTTP attempt and token counts were not persisted, so
they are deliberately recorded as unavailable rather than reconstructed.

## Recovery during the formal run

The first attempt failed safely with `event_map_coverage_invalid` before
publication. DeepSeek had returned event time bounds that did not fully contain
the valid evidence segments it cited. The server now derives event bounds from
verified evidence IDs and expands parent bounds to include child events. The
unknown-evidence check remains strict. The focused regression suite passed 68
tests before the second attempt.

The second attempt completed in 157.247 seconds. The prior published version
remained active until the replacement version completed, then the batch pointer
changed atomically.

## Coverage and publication

| Field | Result |
|---|---:|
| New analysis version | `0029970e-eb50-49a7-b683-3b53b7e931a7` |
| Final version status | `completed` |
| Final error code | empty |
| Event-map JSON bytes | 66,442 |
| Event count | 24 |
| Assigned reliable segments | 1,314 |
| Server-completed unassigned segments | 2,128 |
| Assigned/unassigned overlap | 0 |
| Unknown evidence references | 0 |
| Coverage union | 3,442 / 3,442 |
| Completed scene keys | 6 / 6 |
| Reliable user speaker | `speaker_0`, confidence 0.90 |
| Profile candidates | 0 |
| Published scene containers | 2 |
| Frontend-visible result cards | 3 |
| Published global todos | 0 |

Event types were: eight `other`, seven `casual_chat`, four `discussion`, two
`interview`, two `media`, and one `conversation`. Meeting analysis produced two
independent interview cards; inspiration analysis produced one AI/industry
insight card. The remaining four scenes correctly completed with empty output.
Todo analysis explicitly reported that it found no user commitment, accepted
assignment, or definite execution plan, so no global todo was published.

## Historical-quality comparison

This run closes the productization failures that caused the prior one-event,
zero-card result:

- long input is divided into 23 bounded evidence windows;
- every reliable segment is covered exactly once as assigned or unassigned;
- 24 local events survive merge instead of one dominant low-confidence event;
- the six scene analyses receive event-scoped evidence rather than one oversized
  transcript packet;
- useful work/career cards are allowed even when ownership is not inferred;
- a long, valuable recording cannot silently publish an all-empty result.

The output is materially useful and traceable, but it is not yet the same
artifact shape as the historical hand-written analysis. The historical chain
produced one dense cross-scene daily report with deduplication, conclusions,
risks, and recommendations. The current product publishes separate scene cards,
so several organization and career discussions are compressed into one
inspiration card. Whether this reaches the user's historical quality bar is an
explicit page-acceptance checkpoint before compact-chain work begins.

## Verification

- Backend: `590 passed`.
- Frontend unit tests: `43 passed`, `0 failed`.
- Recovery Playwright tests: `5 passed`.
- Production build: passed, 38 Vite modules.
- Focused boundary and analysis/reanalysis suites after the live failure fix:
  `68 passed`.
- In-app page: three visible result cards; first meeting detail includes
  background, participants, core conclusions, and discussion topics.

No database, audio, transcript text, browser profile, screenshot, API key,
request body, or provider response body is included in Git.
