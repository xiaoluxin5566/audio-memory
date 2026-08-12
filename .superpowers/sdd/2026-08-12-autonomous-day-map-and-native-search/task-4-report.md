# Task 4 report — batch overview and search provenance persistence

## Status

Implemented durable publication for the canonical batch overview, structured
native-search rounds and sources, and the position-zero feed item. New Day Map
publications contain exactly one `batch_overview` card before ordinary cards;
historic batches retain their prior feed shape.

## Implementation

- Added nullable `batch_overview_json`, `search_rounds_json`, and
  `external_sources_json` snapshots to `AnalysisVersion`. Nullable columns make
  pre-0012 versions distinguishable without rewriting historic rows.
- Added Alembic revision `0012`, including reversible upgrade/downgrade steps,
  and advanced the release doctor migration chain/schema check to 0012.
- The publisher validates staged Day Map/search data before moving audio or
  opening the publication transaction. Search-round sources and accumulated
  sources must canonicalize to the same provenance records; identical repeated
  results retain their earliest originating round, while conflicts fail closed.
- A Day Map publication atomically replaces version-scoped cards with one
  deterministic `scene_id="batch_overview"`, `position=0` compatibility card
  followed by deterministic ordinary autonomous cards at positions 1..N.
- `published_card_count` and the returned outcome include the overview row.
- A completed-version retry returns the stored immutable outcome without
  inserting duplicates. A forced failure after card replacement proves that
  cards, overview, sources, rounds, batch linkage, and completion state all
  roll back together.
- Feed cards resolve only their explicitly referenced `external_source_ids` to
  persisted source objects. New versions expose a `sources` list (including an
  empty list for the overview); historic versions with `NULL` source storage do
  not gain that field and therefore retain their exact response shape.

## RED evidence

Initial focused command:

```text
PYTHONPATH=backend/src backend/.venv/bin/pytest \
  backend/tests/unit/analysis/test_day_map_publisher.py \
  backend/tests/unit/content/test_feed_service.py -q
```

Result: `3 failed`. The failures showed that no overview card was published,
`AnalysisVersion` had no external-source storage, and new/legacy feed behavior
was unavailable.

During self-review, changing the expected publication count to include the
position-zero overview produced the intended regression failure: returned
`card_count` was 2 rather than the 3 persisted rows. The implementation now
counts the overview.

A repeated-provider-result test also initially failed because the first
consistency check compared raw round-source sets rather than canonical source
identity. It now canonicalizes identical repeats to the earliest round in the
same manner as the prompt/parser boundary.

## Verification evidence

Latest focused publisher/feed command:

```text
PYTHONPATH=backend/src backend/.venv/bin/pytest \
  backend/tests/unit/analysis/test_day_map_publisher.py \
  backend/tests/unit/content/test_feed_service.py -q
```

Result: `5 passed in 0.43s`.

Relevant publisher/content/migration regressions passed before the final
rollback-only test was added: `39 passed in 2.20s`. The final rollback test is
also included in the complete-suite result below.

The full backend suite was run using importlib mode to avoid the repository's
two existing same-basename `test_events.py` files colliding under pytest's
default import mode:

```text
PYTHONPATH=backend/src backend/.venv/bin/pytest --import-mode=importlib \
  backend/tests -q
```

Final result: `726 passed, 28 skipped in 14.99s`. All skips are explicitly
marked compatibility-only Event Map cases.

Migration validation used a fresh temporary SQLite database and the project's
Alembic configuration:

- upgraded from an empty database through 0012;
- asserted all three nullable columns;
- downgraded 0012 to 0011;
- upgraded back to head and asserted version `0012`.

All steps exited successfully. `alembic command.check` reports only the
pre-existing `transcripts.segment_uid` unique index/constraint representation
difference; no 0012 schema difference was reported.

## Independent review and self-review

Independent read-only review found no Critical or Important issues. It noted
two minor gaps: fresh focused verification after the duplicate-source test and
direct rollback coverage. Both are now addressed by the five-test focused run
and the forced post-replacement failure test.

Mutation/self-review checks covered missing overview, wrong position/count,
duplicate overview on retry, lost search rounds, unknown/conflicting/repeated
source IDs, source leakage between cards, partial transaction visibility, and
historic feed shape changes.

## Concerns

- Running all tests in pytest's default import mode still stops during
  collection because `backend/tests/unit/analysis/test_events.py` and
  `backend/tests/unit/uploads/test_events.py` share a module basename. Importlib
  mode runs the complete suite successfully; this is an existing repository
  test-layout issue outside Task 4.
- The task brief named `backend/alembic/versions`; the repository's configured
  migration path is `backend/migrations/versions`, which is where 0012 was added.

## External review fix — round 1

Both Important review findings were reproduced before implementation.

### Overview presentation compatibility

The position-zero backend payload uses the canonical nested `overview` object,
while the existing feed fallback previously read only `payload.card`. A new
test calls the real `normalizeFeed` with the exact persisted overview shape.
Its RED result was `未命名结果 !== 本次概览`. The fallback now accepts
`payload.overview` when `payload.card` is absent, preserving legacy cards while
displaying the overview title exactly as `本次概览` and retaining its summary.

### Source provenance re-derivation

The publisher now validates each search round against independently
re-derived `ExternalSource` objects from that round's raw `SearchResultItem`
records through `normalize_search_results`. This enforces:

- the source provider equals the analysis version provider;
- `source_id` is the deterministic SHA-256 identity of provider plus provider
  result ID;
- provider result ID, title, URL, publisher, published time, support statement,
  and originating round all match the raw same-round result;
- accumulated sources still equal the canonical union of verified rounds.

RED tests proved the old publisher accepted both an arbitrary source ID and a
wrong provider. GREEN coverage also rejects rewritten title and URL values.

### Fresh verification

Focused publisher provenance tests:

```text
PYTHONPATH=backend/src backend/.venv/bin/pytest \
  backend/tests/unit/analysis/test_day_map_publisher.py -q
```

Result: `7 passed in 0.47s`.

Full backend regression:

```text
PYTHONPATH=backend/src backend/.venv/bin/pytest --import-mode=importlib \
  backend/tests -q
```

Result: `730 passed, 28 skipped in 15.19s`.

Frontend state and build verification from `prototype`:

```text
node --test tests/api-state.test.mjs tests/product-state.test.mjs
npm run build
```

Result: `15 passed`; Vite production build completed successfully with 37
modules transformed, followed by successful Sites build preparation.
