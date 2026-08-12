# Scene Director + Meeting Context Acceptance Evidence

**Date:** 2026-08-10  
**Branch:** `codex/local-fast-v0-1`  
**Implementation HEAD before this evidence record:** `14b652a`  
**Scope:** shared scene director, bounded dossiers, dossier-scoped evidence, Meeting Prompt V3, real-history reanalysis, and card evidence playback. No retranscription, web verification, Compact work, new scene, or new card type.

## Privacy boundary

This record contains only identifiers, hashes, counts, statuses, timings, and normalized error codes. It intentionally excludes source paths, filenames, transcript text, generated card text, model response bodies, credentials, and screenshots.

## Frozen preview

- Source batches: 1
- Audio files: 1
- Source audio duration: 12,685,248 ms
- Transcript characters: 28,470
- Reliable transcript segments reused: 3,442
- Whisper calls: 0
- Diarization calls: 0
- Estimated model calls: 46–106
- Preview blockers: none
- Provider/model: DeepSeek / `deepseek-v4-flash`
- Meeting prompt: V3

## Successful run

- Reanalysis batch: `bc0ee4ce-8c53-4d96-8a67-548c6d604548`
- Source batch: `4d3c808f-743f-5922-808e-b6b9ba5ebd0e`
- Source job: `d29475e4-f148-4b99-9b7e-1e5751da1e48`
- Analysis version: `974bbcb8-4085-40e3-9ac8-ef3e398711b3`
- Status: `completed`
- Created: `2026-08-10T13:05:08.590729+00:00`
- Completed: `2026-08-10T13:08:47.790386+00:00`
- Wall time: 219.2 seconds
- Snapshot hash: `4e0e7b81ba8e7f9f3ebee42a5dc0823b9d29b0b61a123a288346eda3105541d2`
- Fixed-rules hash: `4586018faba83f2a8d9e1dcb32df7fb0be4d1caa5f2bbf47ca91d50235861dbc`
- Event Map hash: `cb65c86b89245477d67988f5e0d5cd5f5fe6ca0e09efd8bc59adb02f4c083581`
- Actual request-token totals: unavailable because usage diagnostics are not persisted with the analysis version

## Coverage and routing

- Stable transcript clusters: 23
- Director calls: 23, one per cluster
- Normalized selections: 16
- Scene dossiers: 16
- Unique reliable segments inside selected dossier scopes: 3,343 / 3,442 (97.1%)
- Dossier segment references including overlap: 5,715
- Routed dossier counts: meeting 9, growth 7, inspiration 4, content 1, todo 0, parenting 0
- Final Event Map: 22 events
- Event-assigned segments: 1,745
- Compatibility `unassigned_segment_ids`: 1,697
- Coverage identity: 1,745 + 1,697 = 3,442
- Director input included all 3,442 reliable segments. Compatibility `unassigned_segment_ids` did not control director input, dossier selection, scene input, or dossier evidence admission.

## Published result

- Published scene rows: 2
- Visible cards: 4 (meeting 3, inspiration 1)
- Published global todos: 0
- Reliable user speaker: unknown (`speaker_id=null`, confidence 0.0, no evidence IDs)
- Meeting participants: 7
- Meeting core conclusions: 7
- Explicit decisions: 0
- Open questions: 3
- Nested meeting actions: 0
- Discussion topics: 8

The page inspection recovered three distinct work-communication contexts, including recruitment, product/business, and organization/career discussions. Media content was not published as a live meeting. Unknown identity remained role-based, no global user/shared todo was produced, and the model did not invent decisions.

## Evidence playback

- The first inspected meeting card exposed 19 chronologically ordered evidence segments.
- Evidence metadata contains only segment ID, start/end time, and a card-scoped playback URL; it contains no transcript text or local source path.
- A real evidence request returned `206 Partial Content`, `Content-Type: audio/mpeg`, `Accept-Ranges: bytes`, and the requested 4,096-byte range from the 202,963,968-byte local audio.
- The browser player reached `readyState=4` with no media error and sought to the evidence start time.
- A segment not referenced by the current published card returned 404 in integration coverage.
- Playback authorization also requires a reliable, risk-classified transcript segment and an audio path contained by the application audio directory.

## Failed-attempt diagnostics and recovery

No failed attempt replaced the published result; atomic publication remained intact until the successful version completed.

| Reanalysis batch | Normalized error | Resolution |
|---|---|---|
| `8f10607e-1175-42c0-89bc-c57a0fb1375b` | `event_map_unknown_segment` | Added one bounded semantic Event Map repair attempt, then fail closed. |
| `b7946c3b-7989-4e16-b745-668c1a8ee606` | `model_analysis_failed` | Surfaced scene evidence failures separately and added one bounded dossier-evidence repair attempt. |
| `1d89ac51-2717-4fa1-8862-6d9324ebe633` | `scene_evidence_invalid` | Distinguished director-routed live interviews from ordinary recorded-media interviews without weakening the media todo guard. |
| `e21aa792-8634-4104-81c9-d94c8428befc` | `model_response_invalid` | A later non-meeting scene returned invalid model structure after its repair attempt; the next full retry completed without a code-policy relaxation. |

## Final verification

- Backend full suite: 642 passed in 23.23 seconds
- Frontend unit/integration suite: 44 passed
- Frontend production build: passed
- Critical Playwright flows: 21 passed in 10.0 seconds
- Live page console errors after playback integration: 0
- Real published feed and card detail: inspected in the in-app browser

## Acceptance handoff

The live service is running from this worktree on `http://127.0.0.1:8765`. Automated and agent-side acceptance is complete. The final gate is direct user review of meeting quality in the live page; subsequent web verification, the other five scene enhancements, and Compact remain out of scope until that review passes.
