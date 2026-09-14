# Beta 8 V1 Call Architecture A/B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run an isolated paid A/B evaluation that compares one-call unified V1 generation with two-call planning plus unified writing on the same real transcript, then present a blinded side-by-side report for product selection.

**Architecture:** Add experiment-only schemas, prompts, runner, deterministic review, and HTML rendering without wiring them into `Beta8ReportRunner` or any production route. Variant A makes one full-transcript writing call; Variant B makes one full-transcript planning call followed by one full-transcript writing call. Both return the same seven-scene V1 schema and persist raw responses, parsed outputs, diagnostics, hashes, and a blinded X/Y comparison.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, existing `ProviderAnalysisClient`, DeepSeek V4 Pro, Markdown, static HTML, pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-beta8-v1-call-architecture-experiment-design.md`

## Global Constraints

- Use the same transcript bytes, model, generation parameters, global quality rules, scene rules, and final V1 schema for both variants.
- Do not call Kimi, execute search, publish cards, or write any development or production database.
- Do not change `Beta8ReportRunner`, `SingleReportRunner`, pipeline routing, provider configuration, or existing report artifacts.
- Every paid run requires the exact CLI confirmation `I_AUTHORIZE_PAID_BETA8_V1_AB`.
- Create a new output directory and fail if it already exists; resume may only reuse artifacts whose input and prompt hashes match.
- Preserve the dirty worktree. Do not reset, clean, commit, merge, push, tag, or publish.
- Do not set a business card-count limit. Technical output truncation is recorded as a failed hard gate.
- Search candidate semantics remain those of the approved seven scene prompts, but no search is executed in this experiment.

---

### Task 1: Experiment schemas and prompt assets

**Files:**
- Create: `backend/src/audio_memory/experiments/__init__.py`
- Create: `backend/src/audio_memory/experiments/beta8_v1_ab.py`
- Create: `backend/src/audio_memory/prompts/beta8/experiments/shared-quality.md`
- Create: `backend/src/audio_memory/prompts/beta8/experiments/variant-a-unified.md`
- Create: `backend/src/audio_memory/prompts/beta8/experiments/variant-b-plan.md`
- Create: `backend/src/audio_memory/prompts/beta8/experiments/variant-b-write.md`
- Test: `backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py`

**Interfaces:**
- Produces: `Beta8UnifiedV1Result`, `Beta8WritingPlan`, `ExperimentPromptSet`, `validate_unified_v1(result, known_segment_ids)`.
- Consumes: the seven approved packaged scene prompts and `Beta8ModelRequest` conventions.

- [ ] **Step 1: Write failing schema tests**

Cover exactly seven ordered scene IDs, no duplicates, valid work-communication basis, nullable independent-value basis fields, unique communication keys, real segment IDs, card/no-card `skip_reason`, and rejection of manually numbered Markdown headings.

```python
def test_unified_result_requires_exactly_seven_scenes():
    payload = valid_unified_payload()
    payload["scene_results"].pop()
    with pytest.raises(ValueError, match="exactly seven"):
        Beta8UnifiedV1Result.model_validate(payload)

def test_work_result_rejects_unknown_segment_ids():
    result = Beta8UnifiedV1Result.model_validate(valid_unified_payload())
    with pytest.raises(ValueError, match="Unknown transcript segment IDs"):
        validate_unified_v1(result, known_segment_ids={"seg_0_1"})
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `PYTHONPATH=backend/src uv run --project backend pytest backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py -q`

Expected: collection or import failure because the experiment module does not exist.

- [ ] **Step 3: Implement minimal strict Pydantic schemas and validators**

Define card basis, unified card, seven-scene result, plan brief, plan result, prompt manifest, canonical JSON, untrusted-data wrapping, and prompt hashing. Reuse the existing scene IDs and todo/search candidate field semantics instead of inventing aliases.

- [ ] **Step 4: Write the four prompt assets**

`shared-quality.md` carries the approved global rules: one substantive work communication per card, value-only admission elsewhere without quantity cap, semantic subject attribution, rich content, content-matched structure, actionable help, direct reader-facing prose, no case-specific rules, and no manual heading numbers.

Variant A performs selection and writing in one response. B1 emits only briefs and evidence. B2 receives the plan plus full transcript, may correct the plan against transcript evidence, and emits the same V1 schema as A.

- [ ] **Step 5: Run focused tests**

Run: `PYTHONPATH=backend/src uv run --project backend pytest backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py -q`

Expected: all tests pass.

### Task 2: Isolated A/B runner and artifact persistence

**Files:**
- Modify: `backend/src/audio_memory/experiments/beta8_v1_ab.py`
- Create: `tests/real-beta8-v1-architecture-ab.py`
- Test: `backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py`

**Interfaces:**
- Produces: `Beta8V1ABRunner.run(source, output) -> ExperimentManifest` and CLI preflight/resume/paid-run behavior.
- Consumes: `ProviderAnalysisClient.generate`, Keychain credentials, `parse_merged_transcript`, prompt set, and Task 1 schemas.

- [ ] **Step 1: Write failing fake-provider tests**

```python
@pytest.mark.asyncio
async def test_variant_a_uses_one_call_and_variant_b_uses_two(tmp_path):
    provider = FakeProvider([valid_v1_json(), valid_plan_json(), valid_v1_json()])
    manifest = await Beta8V1ABRunner(provider, prompts()).run(rows(), tmp_path)
    assert [call.scene_id for call in provider.calls] == [
        "beta8_v1_ab_a", "beta8_v1_ab_b_plan", "beta8_v1_ab_b_write"
    ]
    assert manifest.variant_a.model_response_count == 1
    assert manifest.variant_b.model_response_count == 2
```

Also test refusal to overwrite, hash-checked resume, raw-response persistence before parsing, and independent failure recording for A, B1, and B2.

- [ ] **Step 2: Run focused tests and confirm failure**

Run the Task 1 pytest command and confirm the new runner tests fail because the runner is not implemented.

- [ ] **Step 3: Implement the runner**

Execute A, B1, and B2 sequentially so provider diagnostics map unambiguously to stages. Persist each raw response immediately. Parse and validate after persistence. Snapshot new `ProviderRequestDiagnostic` entries after every stage and write per-variant metrics without counting earlier process diagnostics.

- [ ] **Step 4: Implement the guarded CLI**

Required options:

```text
--source PATH
--output PATH
--model deepseek-v4-pro
--preflight
--resume
--confirm-paid-calls I_AUTHORIZE_PAID_BETA8_V1_AB
```

Preflight prints source hash, bytes, segment count, transcript characters, prompt hashes, schema hash, intended model, and expected response counts without reading credentials or making network calls.

- [ ] **Step 5: Run focused tests**

Expected: all experiment unit tests pass with a fake provider and no network access.

### Task 3: Deterministic review and blinded HTML

**Files:**
- Modify: `backend/src/audio_memory/experiments/beta8_v1_ab.py`
- Create: `tests/render-beta8-v1-architecture-ab.py`
- Test: `backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py`

**Interfaces:**
- Produces: `review_variant(result) -> DeterministicReview`, `build_blind_map(seed)`, and `comparison.html`.
- Consumes: validated A/B results and stage metrics.

- [ ] **Step 1: Write failing review tests**

Test card/scene counts, Markdown characters, heading counts, table counts, long-paragraph flags, report-tone pattern flags, manual-number flags, duplicated normalized titles, duplicated evidence sets, and missing core-information regions. Phrase patterns are diagnostic flags, not production deletion rules.

- [ ] **Step 2: Implement deterministic review**

The review must report observations and hard structural failures separately. It must not claim semantic correctness from keyword checks.

- [ ] **Step 3: Write renderer tests**

Verify that X/Y ordering follows a stored random seed, architecture names never appear in visible HTML, all cards are rendered, Markdown tables are readable, and `blind-map.json` is not embedded in the page.

- [ ] **Step 4: Implement static HTML rendering**

Show X and Y side by side with scene navigation and cards. Add a neutral review form area for the user to record X/Y/tie plus reasons. Do not show calls, cost, prompt names, or architecture until the separate decision summary is opened.

- [ ] **Step 5: Run focused tests**

Run the Task 1 pytest command and expect all tests to pass.

### Task 4: Local verification before paid execution

**Files:**
- Verify only; no new production files.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: a clean local verification record and preflight JSON.

- [ ] **Step 1: Run experiment unit tests**

Run: `PYTHONPATH=backend/src uv run --project backend pytest backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py -q`

- [ ] **Step 2: Run affected existing contract tests**

Run:

```text
PYTHONPATH=backend/src uv run --project backend pytest \
  backend/tests/unit/prompts/test_beta8_scene_schema.py \
  backend/tests/unit/prompts/test_beta8_composer.py \
  backend/tests/integration/test_beta8_report_runner.py -q
```

Expected: all pass, demonstrating the isolated experiment did not change the current runner.

- [ ] **Step 3: Run CLI preflight**

Run the real script with `--preflight` and the approved merged transcript. Verify source hash, segment count, transcript characters, prompt/schema hashes, `deepseek-v4-pro`, A response count 1, and B response count 2.

- [ ] **Step 4: Run formatting checks**

Run `git diff --check` on experiment files and scan for placeholders, embedded secrets, architecture labels in visible blind HTML, and accidental production imports.

### Task 5: Paid A/B execution and review handoff

**Files:**
- Generate only under: `outputs/beta8-v1-architecture-ab/<timestamp>/`

**Interfaces:**
- Consumes: verified CLI and configured DeepSeek credential.
- Produces: the complete artifact tree from experiment spec section 9 and an opened blind comparison page.

- [ ] **Step 1: Record provider balance if a read-only helper is available**

Persist the before balance outside visible blind content. Failure to read balance does not block token/duration evaluation but must be recorded as unavailable.

- [ ] **Step 2: Execute the paid experiment once**

Run with a new timestamped output directory and exact confirmation phrase. Do not use `--resume` unless an interrupted stage has a matching source, prompt, schema, and completed artifact hash.

- [ ] **Step 3: Validate artifacts**

Require raw responses, parsed JSON, cards, metrics, deterministic review, manifest, blind map, comparison HTML, and an unresolved decision summary. Any missing artifact blocks handoff.

- [ ] **Step 4: Open the blind comparison page in the development environment**

Present X/Y without revealing the mapping. Ask the user for X, Y, or tie and concrete reasons. Do not select an architecture before this response unless only one variant has failed a hard gate.

- [ ] **Step 5: Resolve the architecture after human review**

Reveal the map, combine hard gates, deterministic evidence, human reasons, time, tokens, and cost, then update the parent design spec so only the selected topology remains normative. Production implementation is a separate plan and does not begin automatically from this experiment.
