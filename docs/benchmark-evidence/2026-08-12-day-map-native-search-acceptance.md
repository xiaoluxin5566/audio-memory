# Day Map and Native Search Acceptance — 2026-08-12

## Scope and data safety

- Branch: `codex/autonomous-day-map`.
- User database was backed up to
  `~/Library/Application Support/AudioMemory/audio-memory.before-0012-2026-08-12.sqlite3`
  and migrated from schema 0011 to 0012 before new output.
- Originals were preserved. The user selected exactly five unique audio files:
  retained batch `8cc9003f-5d80-5205-9542-b9e7560f7cbf` plus the four-file
  July 30 batch `9b457294-5794-5dec-a3b7-6e285ed81799` (109,182 reliable
  transcript characters). Skipped duplicate batch
  `4d3c808f-743f-5922-808e-b6b9ba5ebd0e` remained unchanged.
- The protected selected-history preview bound the exact source IDs and counts:
  two batches, five files, 109,182 characters, zero Whisper calls and zero
  diarization calls. Reanalysis sent only the selected saved transcripts to the
  configured DeepSeek API after explicit user approval.

## Duplicate inventory

One exact original-audio duplicate was found. Both originals were retained:

```text
audio SHA-256:
e3061b4ba464e5b2b5830e00fdf0ad5ac2dde28d8e4f3021537beb06b3778a0c

skipped batch/file:
4d3c808f-743f-5922-808e-b6b9ba5ebd0e / a4b7c263-e61a-44d7-8932-9d42aed39c9d

reanalyzed batch/file:
8cc9003f-5d80-5205-9542-b9e7560f7cbf / 11025bc9-e135-4121-ad0b-43963840b039

filename: 07月29日 11-22 Pokee SE-audio.mp3
size: 202,963,968 bytes each
duration: 12,685,248 ms each
```

The stored reliable transcripts were not exact duplicates, consistent with two
separate transcription runs:

| Batch/file | Segments | Characters | Structured transcript SHA3-256 | Text-only transcript SHA3-256 |
| --- | ---: | ---: | --- | --- |
| `4d3c808f… / a4b7c263…` | 3,442 | 28,470 | `058e0b6fc36879668d812aa31a5cd09d4ddcc3c7c29066d11fd654d10010147c` | `cbcdf595e68bbc265db5db39f0b484f8916440ed62f69abc3149b42a61e5ced6` |
| `8cc9003f… / 11025bc9…` | 3,345 | 31,013 | `dde5aec7aaa51e3873b31ab71c7724c269d2fb5c39edc40654f6e697193b3483` | `f8550ef2166e7e8b5f5e38b98af6b852ad78ddffd5b94699eef42d410258d958` |

The other four files each had unique audio, structured-transcript, and
text-only transcript fingerprints.

## Configured-provider capability

Only DeepSeek `deepseek-v4-pro` was configured and active. The production
`ProviderAnalysisClient.native_search` path was exercised with a non-user test
request. Sanitized result:

```json
{
  "provider_id": "deepseek",
  "model_id": "deepseek-v4-pro",
  "tool_name": null,
  "available": false,
  "source_count": 0,
  "errors": ["Native web search is not available for this configured provider."],
  "retriable": false,
  "extra_search_key_used": false
}
```

Native search is therefore **not** claimed to work. The actual selected-audio
runs finalized directly from audio; persisted search rounds and external sources
were both empty. No third-party search service or extra search key was used.

## Real-audio run and recovery

Initial selected run: `58467dc8-7423-4da4-a749-a5c0564c27f5`.

| Item | Source batch | Result | Processing time | Published output |
| --- | --- | --- | ---: | --- |
| retained July 29 | `8cc9003f…` | failed pre-fix | 19m 55.467s | none; old current version remained |
| four-file July 30 | `9b457294…` | succeeded | 15m 53.606s | 1 overview + 5 analysis cards |

The July 29 failure saved the Day Map and terminal audio-only search state, but
the final evidence sanitizer removed all unsupported evidence and then allowed
an ordinary `ValueError` to escape. The coordinator correctly refused to publish
partial output but could only persist the generic `model_analysis_failed` code.
The Day Map final path was brought in line with the compatibility paths: it now
gets one bounded evidence-aware semantic retry and records
`autonomous_final_evidence_invalid` if that retry is exhausted. The live service
was restarted on the corrected branch.

Authorized exact one-item retry: `6e0ef2b9-c209-4679-a3bb-d09aa01820c9`.
It completed in 6m 55.119s and published 1 overview + 3 analysis cards. End to
end from the first selected run start through the successful retry was
45m 2.062s. The failed version remains retained as diagnostic history and was
not made current.

## Publication and quality inspection

| Source batch | Current version | Overview | Analysis cards | Day Map scenes | External sources |
| --- | --- | ---: | ---: | ---: | ---: |
| skipped duplicate `4d3c808f…` | unchanged `995eec2a…` | 0 legacy | 2 legacy | n/a | 0 |
| retained July 29 `8cc9003f…` | `f3430646…` | 1 | 3 | 7 | 0 |
| four-file July 30 `9b457294…` | `4a42611a…` | 1 | 5 | 8 | 0 |

Every new current version has exactly one position-zero `本次概览` and
evidence-backed analysis cards. The retained July 29 Day Map includes the
recorded child-education discussion and separates two identifiable interview
contexts. Its final cards concentrate on the strongest evidence-backed workplace
and interview material instead of forcing a weak parenting card. The July 30
comparison identifies media/program subject matter that is present in the
recording, including consumer-electronics pricing, Xiaomi N90 MAX, smart swim
goggles/pro-am athletes, AI commercialization, and Nintendo/Steam hardware
strategy. It publishes no web-enhanced claim because runtime search was
unavailable.

The skipped duplicate batch still points at its original current version and
two legacy cards. Historic card rows were not deleted. After the final profile
rebuild there are six active explicit profile facts, zero URL-bearing values,
and no external-origin facts. Thus no external-source data entered the hidden
profile.

## Automated compatibility and final regression

`backend/tests/integration/test_day_map_native_search_flow.py` covers historic
cards, unavailable search fallback, five-round exhaustion, restart/resume,
evidence/source separation, and external-profile exclusion through the real
runner, publisher, SQLite schema, and feed service. Endpoint tests also cover
exact selected-history preview/create binding and reject omitted, altered,
duplicate, unknown, or incomplete selections.

Final commands and results:

```text
cd backend
.venv/bin/pytest -q
756 passed, 28 skipped in 19.00s

cd prototype
node --test tests/*.test.mjs
50 passed, 0 failed in 1.034s

npm run test:e2e
23 passed in 11.9s

npm run build
37 modules transformed; production and Sites artifacts built in 0.386s

npm run test:sites
4 passed, 0 failed in 0.100s
```

The 28 skips are explicitly marked legacy Event Map compatibility tests. Pytest
uses importlib collection mode so the required exact backend command safely
collects two existing same-basename `test_events.py` modules. One browser
recovery assertion was tightened to the exact intended heading after new copy
introduced a second partial match.

## Remaining product choice

DeepSeek currently runs this feature audio-only. Runtime-proven native search
requires configuring a provider/model whose actual endpoint and tool probe
succeeds; adapter capability alone is not sufficient evidence.
