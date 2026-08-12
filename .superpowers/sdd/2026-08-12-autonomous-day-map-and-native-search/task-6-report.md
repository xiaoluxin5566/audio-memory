# Task 6 report: batch overview and sourced cards

## Outcome

Implemented the frontend presentation boundary for the new autonomous feed
shape.

- A `batch_overview` payload is normalized separately from normal cards. One
  overview is retained per batch, inserted at position zero, and always uses
  the visible title `本次概览`.
- The feed renders that overview as a dedicated batch entry panel rather than a
  clickable `.result-card`; historic cards retain their existing order and
  appearance.
- Card `sources` are resolved only when the card's own
  `external_source_ids` references a backend-supplied source object. The
  client exposes only title, URL, and URL domain, and does not create search or
  recommendation links.
- A compact `外部资料` section appears in card detail, separate from the
  existing `回听证据` playback section. It uses the exact backend source URLs
  for outbound links.

## TDD evidence

The initial state test run failed as expected because normalized cards had no
`kind: "batch_overview"` and historic cards had no `sources` field:

```text
12 tests: 10 passed, 2 failed
```

The focused browser test also failed before implementation because the dedicated
`.batch-overview` panel did not exist:

```text
Expected locator('.batch-overview') to contain "本次概览"; element not found
```

Final focused browser verification:

```text
npx playwright test tests/e2e/batch-overview-sources.spec.js
1 passed
```

## Final verification

```text
node --test tests/api-state.test.mjs tests/detail-layout.test.mjs tests/product-state.test.mjs
22 passed

npm run build
vite build completed; Sites build prepared
```

`git diff --check` completed without whitespace errors.

## Self-review

- The normalization path ignores duplicate overview payloads after retaining
  the first, so a malformed server response cannot render multiple batch entry
  points.
- The source filter is card-local: an item-level source returned for another
  card is not exposed here unless its ID is explicitly referenced.
- Existing non-strict/historic feed cards receive an empty `sources` array and
  preserve their prior display data.
