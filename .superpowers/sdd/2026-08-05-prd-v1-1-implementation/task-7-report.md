# Task 7 implementation report

## Status

Implemented the loopback web trust boundary for every `/api` request and the
session/idempotency boundary for every API `POST`, `PUT`, `PATCH`, and `DELETE`.
The browser fetch and XHR upload paths obtain a 256-bit page token, retain the
raw token only in JavaScript memory, and attach action-scoped idempotency keys.

The implementation preserves the Task 5/6 worker, lease, credential-generation,
version-publication, and immutable-outcome code unchanged. No external provider
request or real credential was used.

## Threat model and trust decisions

- **DNS rebinding:** every `/api` request, including read-only data, requires an
  exact configured-port `Host` of `127.0.0.1`, `localhost`, or IPv6 `[::1]`.
  Localhost suffixes, other loopback aliases, missing/duplicate hosts, wrong
  ports, and attacker domains fail with 403.
- **Cross-site mutation / CSRF:** every API mutation requires exactly one HTTP
  `Origin` equal to one of those configured loopback origins. Missing,
  malformed, HTTPS, lookalike, duplicate, or cross-origin values fail with 403.
- **Session theft/replay:** `GET /api/session` is available only through a
  trusted Host; a supplied foreign Origin is rejected. It returns a 256-bit
  random token with `Cache-Control: no-store`. Only SHA-256 token hashes are
  stored; malformed, expired, duplicate-header, non-ASCII, or unknown tokens
  fail with 401.
- **Duplicate paid/destructive execution:** each mutation also requires a
  bounded ASCII `Idempotency-Key`. The durable key is
  `(session_hash, method + path, idempotency_key)` and its record holds a
  canonical query-plus-body hash plus the exact original response status,
  headers, and body. A changed body fails with 409.
- **Concurrent/restart races:** SQLite `BEGIN IMMEDIATE` elects one owner before
  dispatch. Same-process and cross-connection duplicates see `pending` and wait
  for the durable result instead of executing. Completed outcomes and session
  hashes survive backend restart for the 24-hour page/session lifetime.
- **Bounded persistence:** expired sessions and records are pruned lazily after
  24 hours. The global ledger is capped at 1,000 live entries. At capacity it
  rejects new mutations rather than evicting still-live replay protection.
- **Large uploads:** request bodies spool to disk above 1 MiB before the route
  is dispatched. A streaming multipart parser hashes ordered part headers,
  byte counts, and content digests rather than MIME delimiter bytes, so random
  boundaries do not conflict and boundary-like bytes inside a file cannot
  collide. JSON object ordering/whitespace is canonicalized.
- **Non-API navigation:** `/`, frontend routes, assets, and `/api`-lookalike
  paths such as `/apiary` bypass this middleware. Trusted-host read-only API
  calls require neither a session token nor an idempotency key.

## Protected endpoint matrix

| Area | Protected mutations |
| --- | --- |
| Upload/job lifecycle | `POST /api/jobs`, upload file, remove file, start, resume, retry analysis, cancel job |
| Provider validation/change | validate configured, validate provider, save key, cancel candidate, activate provider |
| Prompt/history analysis controls | prompt `PUT`; future `POST /api/history/reanalysis-batches` is protected before route dispatch |
| Todos | todo `PATCH` and `DELETE` |
| Feedback / QA | card question and feedback `POST` |
| Clear/history | history `DELETE` |

Because enforcement is method-wide under the exact `/api` namespace, later
paid/destructive routes cannot accidentally omit the dependency. Read-only
health, feed, history, provider, prompt, job and event `GET`s require only the
trusted Host boundary.

## RED / GREEN evidence

1. **Backend boundary:** initial Task 7 run failed 11/11 focused tests because
   `audio_memory.security` did not exist. GREEN covered session/Origin/Host,
   loopback forms, navigation/read-only behavior, response replay, endpoint /
   body / session scope, concurrency, restart durability, and 4xx/204 replay.
2. **Main-app wiring:** RED was `TypeError: create_app() got an unexpected
   keyword argument 'local_port'`. GREEN installed the middleware and protected
   the complete product endpoint matrix before route dispatch.
3. **Body replay:** GREEN exposed a real spooled-stream defect
   (`SpooledTemporaryFile` has no `peek`). The failing real request test was
   retained; the fix tracks remaining bytes explicitly.
4. **Configured port:** RED returned 403 when `AUDIO_MEMORY_PORT=9123`. GREEN
   resolves and validates the runtime port used by the launcher.
5. **Bounded ledger:** RED executed a second mutation after the one-record test
   ledger filled. GREEN fails closed with `idempotency_capacity` and retains the
   first replay outcome.
6. **Multipart retries:** RED returned 409 for identical file content because
   two requests had different random MIME boundaries. GREEN hashes normalized
   boundaries and replays the original 201 without a second upload dispatch.
7. **Malformed token:** RED accepted a valid token with a non-ASCII byte because
   decoding silently discarded it. GREEN uses strict ASCII decoding and returns
   401 before dispatch.
8. **Static-prefix isolation:** RED returned 403 for `/apiary` because namespace
   matching used `startswith('/api')`. GREEN matches only `/api` and `/api/`.
9. **Browser fetch:** RED showed `getLocalSessionHeaders` was absent. GREEN
   fetches the session once per module/page, strips the client-only option,
   preserves explicit retry keys, generates distinct UUID keys for distinct
   actions, and leaves GET requests session-free.
10. **XHR upload:** RED made no `/api/session` call and attached no headers.
    GREEN acquires the same in-memory session and attaches session/idempotency
    headers before sending multipart data.
11. **Query fingerprint:** RED executed the same key twice when only the query
    changed because query text was treated as a separate endpoint. GREEN scopes
    by method/path and folds raw query input into the request hash, producing the
    required mismatch conflict.
12. **Unhandled route failure (review):** RED returned the outer 500 once, left
    the claim pending, then returned `409 request_in_progress`. GREEN catches
    ordinary route exceptions inside the protected boundary, logs them, durably
    publishes one sanitized 500, and replays the identical response without a
    second dispatch.
13. **Multipart collision (review):** RED replayed the first upload when two
    different files each contained their request's boundary token. GREEN uses
    the streaming multipart parser's semantic part callbacks, so delimiter
    randomness is excluded while file bytes remain part of the digest.
14. **Expired browser session (review):** RED left fetch and XHR actions on a
    cached expired token. GREEN refreshes once on structured
    `401 invalid_session`, compares the rejected token before clearing shared
    state to avoid concurrent refresh races, and retries with the original
    action key.

## Persistence, concurrency, and replay semantics

- The owner claim is committed before the protected route receives the body.
- The route response is buffered, durably completed with full synchronous
  SQLite publication, and only then released to the original client.
- Concurrent duplicates poll the durable claim and receive the published
  response bytes; they never enter the route.
- A completed response remains replayable after a new app/security-store
  instance opens the same runtime database.
- 2xx, 4xx, and empty 204 outcomes use the same publication/replay path.
- A process failure while a record is still `pending` fails safe: later callers
  do not repeat an uncertain side effect. After a bounded wait they receive
  `request_in_progress`; the stale claim expires at 24 hours.
- Ordinary provider/route exceptions are converted to a sanitized durable 500
  and therefore do not leave a pending claim. Cancellation or hard process loss
  remains the conservative pending case.
- The body hash is independent of JSON formatting and multipart boundary but
  remains sensitive to actual form metadata/file bytes.

## Exact verification commands and results

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_local_web_security.py tests/integration/test_provider_api.py tests/integration/test_content_api.py -q`
  - `32 passed in 0.92s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `379 passed in 6.43s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run python -m compileall -q src tests`
  - exit 0, no output
- `cd prototype && node --test tests/api-client.test.mjs`
  - `7 passed, 0 failed`
- `cd prototype && npm run build`
  - exit 0; Vite built 36 modules and the Sites packaging artifacts
- `cd prototype && npm run test:sites`
  - `4 passed, 0 failed`
- `git diff --check`
  - exit 0, no output

These commands are rerun at the final commit gate; later results supersede the
pre-report timings above if they differ.

## Self-review

- Confirmed no Task 5/6 analysis worker, queue, lease, provider-generation,
  publisher, migration, or content-version code changed.
- Confirmed authorization happens before request-body spooling and route
  dispatch; rejected requests cannot reach upload/provider/analysis/content
  side effects.
- Confirmed duplicate Host, Origin, token, or idempotency headers are not
  merged or accepted.
- Confirmed session tokens and API keys do not enter the security ledger;
  request records contain hashes and responses only. Existing provider tests
  continue to prove API keys are absent from responses.
- Confirmed the cap rejects new work rather than deleting a completed live key,
  preserving the 24-hour at-most-once guarantee.
- Confirmed test fixtures that exercise the real product app now use the
  configured loopback Host rather than weakening production to accept
  `testserver`.
- Independent review found three Important issues: exception-stranded claims,
  multipart boundary-token collisions, and non-refreshing expired browser
  sessions. Each received an observed RED regression and implementation fix.
  Follow-up verdict: **READY**, with no remaining Critical or Important
  findings; the reviewer independently confirmed 379 backend and 11 combined
  client/worker tests.
- `docs/HANDOFF-2026-08-06.md` remains untouched, untracked, unstaged, and
  excluded from the commit.

## Concerns and explicit limits

- The security ledger deliberately persists session **hashes** for 24 hours,
  rather than losing them at process exit, because restart-safe replay scoped
  by session cannot otherwise authenticate the browser's existing token. Raw
  tokens remain JavaScript-memory-only and never persist.
- Middleware cannot atomically commit one transaction across SQLite, Keychain,
  filesystem mutations, and remote provider calls. A crash after a side effect
  but before response publication therefore leaves a conservative pending
  claim: it prevents duplicate fees/destruction but cannot reconstruct the
  missing response. After the 24-hour safety window expires, a retry is a new
  execution and must be treated as recovery from an uncertain outcome.
- The 1,000-entry bound can temporarily reject new mutations under sustained
  volume. This is an intentional fail-safe tradeoff; expiry restores capacity.
- The developer Vite proxy must present one of the configured backend Host /
  Origin pairs. The packaged same-origin application does so directly.

## Formal review fix round 1 (2026-08-06)

Five follow-up findings were closed with observed failing tests before each
production change:

1. **Exact Host / Origin pairing.** The RED matrix showed all six cross-pairs
   among `127.0.0.1`, `localhost`, and `[::1]` were accepted. The middleware
   now resolves the exact trusted Host authority and requires Origin, when
   required or present, to equal `http://<that-authority>`. Session issuance
   remains available to originless direct clients, while demonstrable
   cross-site issuance is rejected.
2. **Ambiguous transport failure.** RED fetch and XHR tests lost the response
   and surfaced the network failure without retrying. Fetch mutations and XHR
   uploads now make at most one transport retry and preserve the original
   action idempotency key and local session token across that retry. The retry
   is deliberately bounded; a second transport failure remains visible.
3. **Bounded session issuance.** RED could issue sessions without limit. The
   session store now performs expiry cleanup, live-count enforcement, and
   insertion under `BEGIN IMMEDIATE`; the default live cap is 1,000. A
   concurrent eight-request regression with a cap of two produces exactly two
   sessions and six `429 session_capacity` responses. An indexed expiry column
   and a clock-controlled regression prove expired sessions restore capacity.
   A final RED regression also proved an originless browser request marked
   `Sec-Fetch-Site: cross-site` was accepted; it is now rejected before session
   creation, while an originless direct client without browser fetch metadata
   remains supported.
4. **Safe Vite development path.** RED could not run the browser development
   flow through the old configuration. Vite now binds loopback only, accepts
   only loopback backend targets, and proxies `/api`. It rewrites Origin only
   after validating an exact same-origin development Host/Origin pair, so an
   attacker-supplied or missing mutation Origin is never blessed. The
   integration test starts Vite plus a real FastAPI app using the production
   security middleware and proves same-origin mutation succeeds, cross-site or
   originless mutation fails, cross-site session issuance fails, and originless
   direct session bootstrap still succeeds.
5. **Semantic behavior headers.** RED reused a completed action when either
   `Content-Type` semantics or `X-Configuration-Session` changed. Both are now
   included in the request fingerprint. Content type is normalized with the
   random multipart `boundary` excluded, retaining exact replay for equivalent
   multipart submissions while preserving other media-type parameters.

### Formal-fix verification

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_local_web_security.py tests/integration/test_provider_api.py tests/integration/test_content_api.py -q`
  - `46 passed in 1.05s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `393 passed in 6.55s`
- `cd backend && PYTHONPYCACHEPREFIX=/private/tmp/audio-memory-task7-pycache python3 -m compileall -q src tests`
  - exit 0, no output (the cache prefix keeps generated bytecode in a writable
    temporary directory)
- `cd prototype && node --test tests/api-client.test.mjs`
  - `9 passed, 0 failed`
- `cd prototype && node --test tests/dev-proxy-security.test.mjs`
  - `1 passed, 0 failed`; real Vite and backend middleware over loopback
- `cd prototype && npm run build`
  - exit 0; Vite built 36 modules and prepared Sites artifacts
- `cd prototype && npm run test:sites`
  - `4 passed, 0 failed`
- `git diff --check`
  - exit 0, no output

The developer proxy is intentionally a loopback-only convenience and does not
broaden the backend origin allowlist. Session and idempotency caps intentionally
fail closed under saturation. `docs/HANDOFF-2026-08-06.md` remains untouched,
untracked, unstaged, and excluded from this fix round.

An independent read-only review found no Critical or Important issue and
returned **READY**. The reviewer independently reran the security suite (31
tests at that point) and client suite (9 tests). A follow-up delta review after
the `Sec-Fetch-Site` defense also returned **READY** and independently passed
the then-32-test security suite plus the real Vite/backend integration test.

## Formal review fix round 2 (2026-08-06)

Two remaining Important findings were addressed with strict RED / GREEN
regressions:

1. **Composed fetch retry state.** The RED sequence was: expired-session 401,
   refresh, refreshed mutation commits but its response is lost. The client
   propagated that transport error because the refreshed send sat outside the
   original retry block. Mutation fetches now use one send loop with two
   independent budgets: at most one session refresh and at most one transport
   retry across any attempt. Every attempt retains the original action key;
   transport retry after refresh also retains the refreshed session token. A
   second regression makes the post-refresh response fail twice and proves the
   third mutation attempt fails visibly rather than looping. Equivalent XHR
   success and exhaustion sequences prove its existing shared `refreshed` and
   `transportRetries` state already composes correctly, so no XHR production
   change was needed.
2. **Collision-free Content-Type structure.** The RED pair
   `application/json; a="x;b=y"` and `application/json; a=x; b=y` flattened to
   the same string and replayed under one key. The canonical representation now
   serializes a parsed marker, media type, and sorted parameter name/value
   parts with an explicit part count and length prefix for every part. Only a
   `boundary` parameter on a `multipart/*` media type is excluded. Regressions
   prove the collision now returns `409 idempotency_key_reused`, reordered
   equivalent parameters still replay, non-multipart boundary values remain
   semantic, and multipart requests with random boundary strings still replay.

### Round-2 RED evidence

- Fetch refresh followed by response loss raised `TypeError` on attempt two;
  the bounded case likewise stopped on `lost response 2` instead of consuming
  its one transport-retry budget after refresh.
- The quoted-delimiter Content-Type pair returned 201 replay instead of 409.
- Changing a non-multipart boundary parameter also returned 201 replay instead
  of 409.

### Round-2 final verification

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_local_web_security.py tests/integration/test_provider_api.py tests/integration/test_content_api.py -q`
  - `49 passed in 1.53s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `396 passed in 7.67s`
- `cd backend && PYTHONPYCACHEPREFIX=/private/tmp/audio-memory-task7-fix2-pycache python3 -m compileall -q src tests`
  - exit 0, no output
- `cd prototype && node --test tests/api-client.test.mjs`
  - `13 passed, 0 failed`
- `cd prototype && node --test tests/dev-proxy-security.test.mjs`
  - `1 passed, 0 failed`; real Vite and backend middleware over loopback
- `cd prototype && npm run build`
  - exit 0; Vite built 36 modules and prepared Sites artifacts
- `cd prototype && npm run test:sites`
  - `4 passed, 0 failed`
- `git diff --check`
  - exit 0, no output

`docs/HANDOFF-2026-08-06.md` remains untouched, untracked, unstaged, and
excluded from round 2.

## Formal review fix round 3 (2026-08-06)

The independent round-2 review returned **NOT READY** with two Important and
one Minor finding. All three were handled with additional behavior-level
coverage:

1. **Full fetch response transport boundary.** The RED regression used a real
   errored `ReadableStream`: 401, session refresh, committed 201 headers, then
   response-body stream loss. Only two action attempts occurred and the stream
   error escaped. Fetch plus complete `response.text()` consumption now happen
   inside each bounded attempt; only fetch/body-consumption failures consume
   the single transport-retry budget. JSON parsing and response interpretation
   occur after successful buffering, so invalid application payloads are not
   misclassified as network failures. The same action key and refreshed token
   are retained for replay, and the existing exhaustion regression still
   proves the loop is bounded.
2. **Total and conservative Content-Type canonicalization.** RED proved a
   legal RFC2231 value (`title*=utf-8''caf%C3%A9`) raised
   `UnicodeEncodeError`, while `not a content type` and `also invalid` collapsed
   to one replay fingerprint. Extended values are now collapsed with strict
   decoding and normalized parts use UTF-8. A standards-aware parser checks
   defects before canonicalization; invalid media types, invalid parameter
   names, parse defects, or decoding failures fall back to tagged raw bytes.
   The raw and parsed variants both retain count and per-part length prefixes.
   A further observed RED shows two whitespace variants of the same unclosed
   quoted parameter no longer normalize together after the conservative raw
   fallback.
3. **Both retry orderings.** Explicit fetch and XHR tests now cover transport
   failure followed by 401 and session refresh. Together with the inverse-order
   and exhaustion tests, they prove each state machine permits at most one of
   each recovery action in either order while preserving the action key.

### Round-3 RED evidence

- The errored response stream propagated `body stream lost after commit` after
  only two mutation attempts.
- RFC2231 `café` normalization raised `UnicodeEncodeError` during fingerprinting.
- Distinct invalid media types replayed 201 under one key instead of returning
  409; two defect-bearing unclosed-quote variants did the same.

### Round-3 final verification

- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_local_web_security.py tests/integration/test_provider_api.py tests/integration/test_content_api.py -q`
  - `52 passed in 1.32s`
- `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`
  - `399 passed in 7.18s`
- `cd backend && PYTHONPYCACHEPREFIX=/private/tmp/audio-memory-task7-fix3-pycache python3 -m compileall -q src tests`
  - exit 0, no output
- `cd prototype && node --test tests/api-client.test.mjs`
  - `16 passed, 0 failed`
- `cd prototype && node --test tests/dev-proxy-security.test.mjs`
  - `1 passed, 0 failed`; real Vite and backend middleware over loopback
- `cd prototype && npm run build`
  - exit 0; Vite built 36 modules and prepared Sites artifacts
- `cd prototype && npm run test:sites`
  - `4 passed, 0 failed`
- `git diff --check`
  - exit 0, no output

`docs/HANDOFF-2026-08-06.md` remains untouched, untracked, unstaged, and
excluded from round 3.

The independent follow-up review returned **READY** with no Critical or
Important findings. It independently passed all 16 client tests, all 52
focused backend tests, and `git diff --check`, then exercised 100,000 arbitrary
Content-Type byte inputs plus an RFC2231 percent-byte matrix without an
exception. The reviewer confirmed both retry orderings preserve one action key
and independent budgets, and the raw/parsed length-prefixed fingerprint domains
are structurally distinct.
