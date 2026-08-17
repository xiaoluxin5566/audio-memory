# Report Audit and Targeted Revision Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model UI annotation with a scored full V1 audit, evidence-bounded revision, and bounded V2 final audit that always publishes the latest usable report, then compare the new and old flows on the same historical transcript.

**Architecture:** `SingleReportRunner` remains the production entry point and persists four resumable artifacts in `AnalysisVersion.staged_results_json`. `PromptComposer` exposes explicit generation, full-audit, targeted-revision, and final-audit request builders; the two audit builders share one prompt and one strict schema. Publication receives a `MarkdownReportResult` carrying truthful pipeline metadata, while Markdown presentation types are inferred deterministically without a model call.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy async, pytest/pytest-asyncio, existing provider adapters, React prototype, DeepSeek historical evaluation script.

**Spec:** `docs/superpowers/specs/2026-08-17-report-audit-revision-pipeline-design.md`

## Global Constraints

- Preserve all unrelated existing working-tree changes; inspect diffs before every edit and stage only intended paths.
- Only V1 generation and `full_v1_audit` may receive the full transcript.
- Targeted revision and `revision_final_audit` receive bounded issue evidence and must not receive the full transcript.
- V1 generation failure is fatal; every later technical or quality failure publishes the latest usable report.
- A technically completed final audit publishes V2 even when `passed=false`.
- Remove the production model call for UI annotations; retain deterministic Markdown parsing.
- Use the same model, transcript, profile, and user goal for old/new historical comparison whenever available.
- Never persist provider secrets or raw provider responses in publication metadata.

---

### Task 1: Define strict audit and revision contracts

**Files:**
- Create: `backend/src/audio_memory/prompts/direct_report_audit_schema.py`
- Create: `backend/src/audio_memory/prompts/direct_report_revision_schema.py`
- Test: `backend/tests/unit/prompts/test_direct_report_audit_schema.py`
- Test: `backend/tests/unit/prompts/test_direct_report_revision_schema.py`

**Interfaces:**
- Produces: `ReportAudit`, `AuditMode`, `AuditIssue`, `AuditScores`, `AuditCoverage`, `EvidenceExcerpt`.
- Produces: `TargetedReportRevision`, `TargetedSectionRevision`.
- Consumers: `PromptComposer`, `SingleReportRunner`, historical comparison script.

- [ ] **Step 1: Write failing audit-schema tests**

```python
def test_full_audit_requires_complete_coverage_and_score_sum() -> None:
    payload = valid_audit_payload(mode="full_v1_audit")
    payload["scores"]["total"] = 99
    with pytest.raises(ValidationError):
        ReportAudit.model_validate(payload)


def test_critical_issue_caps_score_and_requires_revision_evidence() -> None:
    payload = valid_audit_payload(mode="full_v1_audit")
    payload["scores"] = score_payload(total=80)
    payload["issues"] = [material_issue(severity="critical")]
    with pytest.raises(ValidationError):
        ReportAudit.model_validate(payload)


def test_final_audit_does_not_claim_full_transcript_coverage() -> None:
    payload = valid_audit_payload(mode="revision_final_audit")
    payload["coverage"]["full_transcript_reviewed"] = True
    with pytest.raises(ValidationError):
        ReportAudit.model_validate(payload)
```

- [ ] **Step 2: Run audit-schema tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_audit_schema.py -q`

Expected: FAIL because `direct_report_audit_schema` does not exist.

- [ ] **Step 3: Implement the audit schema**

Implement frozen, `extra="forbid"` Pydantic models with:

```python
AuditMode = Literal["full_v1_audit", "revision_final_audit"]
Severity = Literal["critical", "major", "minor"]

class AuditScores(BaseModel):
    factual_accuracy: int = Field(ge=0, le=30)
    important_coverage: int = Field(ge=0, le=25)
    analysis_depth: int = Field(ge=0, le=20)
    actionability: int = Field(ge=0, le=15)
    expression_structure: int = Field(ge=0, le=10)
    total: int = Field(ge=0, le=100)

class ReportAudit(BaseModel):
    audit_mode: AuditMode
    rubric_version: Literal[1]
    passed: bool
    scores: AuditScores
    coverage: AuditCoverage
    issues: list[AuditIssue]
    unresolved_issue_ids: list[str]
```

Add `model_validator` rules for score sum, severity caps, `passed`, unique issue IDs, material-issue evidence packets, and mode-specific coverage claims.

- [ ] **Step 4: Run audit-schema tests and verify GREEN**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_audit_schema.py -q`

Expected: PASS.

- [ ] **Step 5: Write failing revision-schema tests**

```python
def test_revision_requires_every_material_issue_resolved_or_unresolved() -> None:
    payload = valid_revision_payload()
    payload["revisions"][0]["issues_resolved"] = []
    with pytest.raises(ValidationError):
        TargetedReportRevision.model_validate(payload)


def test_revision_rejects_duplicate_section_ids() -> None:
    payload = valid_revision_payload()
    payload["revisions"].append(dict(payload["revisions"][0]))
    with pytest.raises(ValidationError):
        TargetedReportRevision.model_validate(payload)
```

- [ ] **Step 6: Run revision-schema tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_revision_schema.py -q`

Expected: FAIL because `direct_report_revision_schema` does not exist.

- [ ] **Step 7: Implement the revision schema and verify GREEN**

Implement `TargetedSectionRevision` with `section_id`, `title`, `revised_markdown`, `issues_resolved`, `evidence_segment_ids`, `removes_repetition`, and `repetition_reason`; implement `TargetedReportRevision` with `revisions`, `unresolved_issue_ids`, and `revision_summary` plus uniqueness validation.

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_revision_schema.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the contract task**

Before staging, run `git diff -- backend/src/audio_memory/prompts backend/tests/unit/prompts` and confirm no unrelated hunks are included.

```bash
git add backend/src/audio_memory/prompts/direct_report_audit_schema.py backend/src/audio_memory/prompts/direct_report_revision_schema.py backend/tests/unit/prompts/test_direct_report_audit_schema.py backend/tests/unit/prompts/test_direct_report_revision_schema.py
git commit -m "feat: define audited report pipeline contracts"
```

---

### Task 2: Add the three production Prompt artifacts and composer requests

**Files:**
- Create: `backend/src/audio_memory/prompts/direct-report-generation.md`
- Create: `backend/src/audio_memory/prompts/direct-report-audit.md`
- Create: `backend/src/audio_memory/prompts/direct-report-revision.md`
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Modify: `backend/tests/unit/prompts/test_direct_report_prompt.py`

**Interfaces:**
- Consumes: `ReportAudit.model_json_schema()`, `TargetedReportRevision.model_json_schema()`.
- Produces: `compose_direct_report_markdown`, `compose_full_report_audit`, `compose_targeted_report_revision`, `compose_revision_final_audit`.

- [ ] **Step 1: Replace old prompt assertions with failing request-boundary tests**

Add tests proving:

```python
def test_full_audit_receives_full_transcript_and_v1() -> None:
    request = PromptComposer().compose_full_report_audit(...)
    assert request.scene_id == "direct-report-audit-v1"
    assert "seg_0_0" in request.user_data
    assert "full_v1_audit" in request.instructions


def test_targeted_revision_receives_issue_evidence_but_not_full_transcript() -> None:
    request = PromptComposer().compose_targeted_report_revision(...)
    assert request.scene_id == "direct-report-revision"
    assert "UNRELATED_TRANSCRIPT_MARKER" not in request.user_data
    assert "seg_0_0" in request.user_data


def test_final_audit_receives_diff_but_not_full_transcript() -> None:
    request = PromptComposer().compose_revision_final_audit(...)
    assert request.scene_id == "direct-report-audit-final"
    assert "revision_final_audit" in request.instructions
    assert "UNRELATED_TRANSCRIPT_MARKER" not in request.user_data
```

- [ ] **Step 2: Run prompt tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_prompt.py -q`

Expected: FAIL because the new composer methods and prompt files do not exist.

- [ ] **Step 3: Add the approved V1 Prompt text**

Write the complete approved first versions from the design conversation into the three prompt files. Keep safety rules in `direct-report-system.md`; generation must explicitly exclude UI schema; audit must define both modes and the 100-point rubric; revision must limit edits to allowed sections and evidence.

- [ ] **Step 4: Implement explicit composer methods**

Add methods with explicit arguments rather than a generic untyped payload:

```python
def compose_full_report_audit(
    self, *, transcript_markdown: str, profile: list[dict[str, object]],
    user_analysis_prompt: str, v1_markdown: str, sections,
    gate_failures: tuple[str, ...], segment_count: int,
) -> DirectReportRequest: ...

def compose_targeted_report_revision(
    self, *, v1_title: str, section_outline: list[dict[str, str]],
    editable_sections: list[dict[str, str]], adjacent_sections: list[dict[str, str]],
    audit: ReportAudit, allowed_segment_ids: set[str],
) -> DirectReportRequest: ...

def compose_revision_final_audit(
    self, *, v2_markdown: str, section_diffs: list[dict[str, object]],
    v1_audit: ReportAudit, revision: TargetedReportRevision,
) -> DirectReportRequest: ...
```

Ensure only the first method includes `transcript_markdown`.

- [ ] **Step 5: Run prompt tests and verify GREEN**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_prompt.py tests/unit/prompts/test_direct_report_audit_schema.py tests/unit/prompts/test_direct_report_revision_schema.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the Prompt task**

Inspect `git diff -- backend/src/audio_memory/prompts/composer.py` because this file was already modified before this feature; stage it only after confirming the combined diff is intentional.

```bash
git add backend/src/audio_memory/prompts/direct-report-generation.md backend/src/audio_memory/prompts/direct-report-audit.md backend/src/audio_memory/prompts/direct-report-revision.md backend/src/audio_memory/prompts/composer.py backend/tests/unit/prompts/test_direct_report_prompt.py
git commit -m "feat: compose report audit and revision prompts"
```

---

### Task 3: Add bounded revision validation and report-quality metadata

**Files:**
- Modify: `backend/src/audio_memory/analysis/direct_report_sections.py`
- Modify: `backend/src/audio_memory/analysis/markdown_report.py`
- Create: `backend/src/audio_memory/analysis/direct_report_pipeline.py`
- Test: `backend/tests/unit/analysis/test_direct_report_sections.py`
- Create: `backend/tests/unit/analysis/test_direct_report_pipeline.py`

**Interfaces:**
- Produces: `build_revision_packet`, `apply_targeted_revisions`, `build_section_diffs`.
- Produces: `ReportQualityMetadata` and `AuditStatus`.
- Consumes: strict audit/revision models and existing `ReportSection` splitting.

- [ ] **Step 1: Write failing bounded-revision tests**

Cover unknown evidence, unauthorized sections, unresolved material issues, abnormal compression, preserved untouched sections, and deterministic V1-to-V2 diffs.

```python
def test_apply_targeted_revision_preserves_unauthorized_sections_byte_for_byte():
    result = apply_targeted_revisions(V1, revision, audit, valid_segment_ids={"seg_0_0"})
    assert extract_section(result, "section_003") == extract_section(V1, "section_003")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_direct_report_sections.py tests/unit/analysis/test_direct_report_pipeline.py -q`

Expected: FAIL because the bounded pipeline helpers do not exist.

- [ ] **Step 3: Implement the smallest bounded pipeline helpers**

Reuse current section parsing and replacement behavior. Do not add a second Markdown parser. Introduce metadata values:

```python
AuditStatus = Literal[
    "completed", "completed_unaudited", "completed_v1_revision_failed",
    "completed_v2_final_audit_degraded",
]
ScoreScope = Literal["v1_full_audit", "v1_pre_revision", "v2_final_audit"]
```

Extend `MarkdownReportResult` with optional `quality_metadata` and keep `report_annotations` only for backward-compatible reads, not new production writes.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_direct_report_sections.py tests/unit/analysis/test_direct_report_pipeline.py tests/unit/analysis/test_markdown_report.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the bounded validation task**

```bash
git add backend/src/audio_memory/analysis/direct_report_sections.py backend/src/audio_memory/analysis/direct_report_pipeline.py backend/src/audio_memory/analysis/markdown_report.py backend/tests/unit/analysis/test_direct_report_sections.py backend/tests/unit/analysis/test_direct_report_pipeline.py backend/tests/unit/analysis/test_markdown_report.py
git commit -m "feat: validate bounded report revisions"
```

---

### Task 4: Replace the production runner state machine

**Files:**
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Modify: `backend/tests/integration/test_single_report_runner.py`

**Interfaces:**
- Consumes: new composer methods, schemas, bounded revision helpers, and quality metadata.
- Produces staged keys: `direct_report_v1_markdown`, `direct_report_v1_audit`, `direct_report_v2_revisions`, `direct_report_v2_markdown`, `direct_report_v2_final_audit`, `direct_report_publication_metadata`.

- [ ] **Step 1: Rewrite the fake provider around four named stages**

The fake should dispatch by scene ID: `direct-report`, `direct-report-audit-v1`, `direct-report-revision`, and `direct-report-audit-final`. Remove annotation expectations from production-flow tests.

- [ ] **Step 2: Write failing happy-path and early-exit tests**

```python
async def test_no_material_issue_publishes_v1_after_two_calls(): ...
async def test_material_issue_runs_four_calls_and_publishes_v2(): ...
async def test_only_generation_and_v1_audit_receive_transcript(): ...
```

- [ ] **Step 3: Run the focused integration tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_single_report_runner.py -q`

Expected: FAIL against the old review-plus-annotation state machine.

- [ ] **Step 4: Implement generation and full-audit checkpoints**

Generate or reuse V1, evaluate the deterministic gate for diagnostic input, call or reuse V1 audit, and immediately publish V1 for a clean audit.

- [ ] **Step 5: Implement targeted revision and bounded final audit**

Build the revision packet from audit issues, merge or reuse V2, call or reuse final audit, and publish V2 regardless of final quality pass/fail.

- [ ] **Step 6: Write failing degradation tests**

```python
async def test_v1_audit_failure_publishes_unaudited_v1(): ...
async def test_revision_failure_publishes_scored_v1(): ...
async def test_final_audit_transport_failure_publishes_v2_with_v1_score_scope(): ...
async def test_final_audit_quality_failure_still_publishes_scored_v2(): ...
async def test_resume_reuses_every_completed_stage(): ...
```

- [ ] **Step 7: Run degradation tests and verify RED**

Run the five named tests with `pytest -q`; expected failures must show missing degradation publication behavior.

- [ ] **Step 8: Implement degradation publication and resume**

Catch technical/validation errors separately at each post-generation stage, store normalized type/message diagnostics, construct truthful metadata, and call the existing publisher with V1 or V2. Never persist raw provider bodies.

- [ ] **Step 9: Run the complete runner test file and verify GREEN**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_single_report_runner.py -q`

Expected: PASS with no production annotation call.

- [ ] **Step 10: Commit the runner task**

Inspect the pre-existing diff in both modified files before staging.

```bash
git add backend/src/audio_memory/analysis/single_report_runner.py backend/tests/integration/test_single_report_runner.py
git commit -m "feat: run scored audit and targeted report revision"
```

---

### Task 5: Publish and expose report status and score

**Files:**
- Modify: `backend/src/audio_memory/analysis/publisher.py`
- Modify: `backend/src/audio_memory/api/content.py`
- Modify: `backend/tests/integration/test_content_api.py`
- Modify: `backend/tests/integration/test_imported_analysis_publication.py`
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/tests/api-state.test.mjs`
- Modify: `prototype/tests/detail-layout.test.mjs`

**Interfaces:**
- Consumes: `MarkdownReportResult.quality_metadata`.
- Produces API fields: `reportVersion`, `auditStatus`, `qualityScore`, `qualityScoreScope`, `qualityPassed`, `qualityScores`, and `auditIssueCounts`.

- [ ] **Step 1: Write failing backend publication/API tests**

Assert all four status mappings, null score for unaudited V1, scoped V1 score for degraded V2 final audit, and V2 score for completed-but-failed quality audit.

- [ ] **Step 2: Run backend tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/integration/test_content_api.py tests/integration/test_imported_analysis_publication.py -q`

Expected: FAIL because quality metadata is not published.

- [ ] **Step 3: Persist and expose normalized metadata**

Store metadata alongside the Markdown card payload, return camelCase API fields, and retain compatibility for cards created before these fields existed.

- [ ] **Step 4: Run backend tests and verify GREEN**

Run the same command; expected PASS.

- [ ] **Step 5: Write failing frontend normalization and rendering tests**

Assert exact labels:

```javascript
'已完成，85分'
'已完成（未审计）'
'已完成（V1），72分'
'已完成（V2），V1审计72分'
```

- [ ] **Step 6: Run frontend tests and verify RED**

Run: `cd prototype && node --test tests/api-state.test.mjs tests/detail-layout.test.mjs`

Expected: FAIL because the UI does not normalize or render audit status.

- [ ] **Step 7: Implement minimal status normalization and display**

Add a single helper that derives user-facing text from normalized metadata; render it near the existing report runtime metrics without introducing new visual components.

- [ ] **Step 8: Run frontend tests and verify GREEN**

Run the same command; expected PASS.

- [ ] **Step 9: Commit publication and UI status**

Inspect all pre-existing diffs before staging these already-modified files.

```bash
git add backend/src/audio_memory/analysis/publisher.py backend/src/audio_memory/api/content.py backend/tests/integration/test_content_api.py backend/tests/integration/test_imported_analysis_publication.py prototype/src/api/state.js prototype/src/App.jsx prototype/tests/api-state.test.mjs prototype/tests/detail-layout.test.mjs
git commit -m "feat: expose report audit status and score"
```

---

### Task 6: Remove the production annotation call while preserving compatibility

**Files:**
- Modify: `backend/src/audio_memory/prompts/composer.py`
- Modify: `backend/src/audio_memory/analysis/single_report_runner.py`
- Modify: `backend/tests/unit/prompts/test_direct_report_prompt.py`
- Modify: `backend/tests/unit/analysis/test_direct_report_annotations.py`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/tests/report-presentation.test.mjs`

**Interfaces:**
- Production writes no `direct_report_annotations` checkpoint.
- Existing historical `reportAnnotations` remain readable.
- Markdown rendering falls back to syntax-derived block types.

- [ ] **Step 1: Add a failing test that no production request uses `direct-report-annotations`**

Assert the happy path scene IDs are generation and audit stages only, with no annotation scene.

- [ ] **Step 2: Run focused tests and verify RED**

Run backend runner and prompt tests; expected failure against remaining annotation production code.

- [ ] **Step 3: Remove annotation composition from the production manifest and runner**

Keep legacy schema/parser modules only where required to read existing saved cards. Do not delete historical output data.

- [ ] **Step 4: Verify deterministic rendering and historical compatibility**

Run:

```bash
cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_direct_report_annotations.py tests/unit/prompts/test_direct_report_prompt.py tests/integration/test_single_report_runner.py -q
cd ../prototype && node --test tests/report-presentation.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit annotation retirement**

```bash
git add backend/src/audio_memory/prompts/composer.py backend/src/audio_memory/analysis/single_report_runner.py backend/tests/unit/prompts/test_direct_report_prompt.py backend/tests/unit/analysis/test_direct_report_annotations.py prototype/src/App.jsx prototype/tests/report-presentation.test.mjs
git commit -m "refactor: retire model report annotations"
```

---

### Task 7: Add reproducible old-versus-new historical evaluation

**Files:**
- Modify: `tests/real-single-report-eval.py`
- Create: `tests/compare-report-pipelines.py`
- Create after run: `docs/working/report-audit-revision-quality-comparison.md`
- Create after run: `outputs/deepseek-audited-report/<run-id>/metrics.json`
- Create after run: `outputs/deepseek-audited-report/<run-id>/quality.json`

**Interfaces:**
- Consumes frozen transcript/profile inputs and old/new run artifacts.
- Produces redacted per-stage metrics and a Markdown comparison.

- [ ] **Step 1: Write failing comparison-unit tests**

Create `backend/tests/unit/analysis/test_report_pipeline_comparison.py` covering incomparable inputs, missing old timings, score-scope labels, and per-stage latency totals.

- [ ] **Step 2: Run comparison tests and verify RED**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/analysis/test_report_pipeline_comparison.py -q`

Expected: FAIL because comparison helpers do not exist.

- [ ] **Step 3: Implement redacted evaluation and comparison output**

Record for each model call: scene ID, elapsed seconds, input/output tokens, request/response bytes, and completion status. Record transcript fingerprint, profile fingerprint, prompt hashes, and model ID to prove comparability. Never store keys or unredacted provider headers.

- [ ] **Step 4: Run comparison tests and verify GREEN**

Run the same command; expected PASS.

- [ ] **Step 5: Run the new pipeline on the historical transcript**

Run the opt-in real evaluation command documented by the updated script. If sandboxed network access fails, rerun only after requesting approval. Preserve all generated artifacts.

- [ ] **Step 6: Evaluate the old and new final reports with the same audit rubric**

Use `audit_mode=full_v1_audit` as a comparison judge over the same full transcript and each final report. Label judge calls separately from production latency and call counts.

- [ ] **Step 7: Generate the comparison document**

Include exact measured values, missing-baseline caveats, side-by-side scores, material issues, report length, content retained/lost, and a reasoned recommendation. Do not infer exact old per-stage latency where the old metrics did not preserve it.

- [ ] **Step 8: Commit scripts and comparison artifacts**

```bash
git add tests/real-single-report-eval.py tests/compare-report-pipelines.py backend/tests/unit/analysis/test_report_pipeline_comparison.py docs/working/report-audit-revision-quality-comparison.md outputs/deepseek-audited-report
git commit -m "test: compare audited and legacy report pipelines"
```

---

### Task 8: Full verification and requirements audit

**Files:**
- Modify only if verification reveals defects in files already in scope.

**Interfaces:**
- Confirms the complete spec and regression surface.

- [ ] **Step 1: Run focused backend Prompt and report tests**

```bash
cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest tests/unit/prompts/test_direct_report_audit_schema.py tests/unit/prompts/test_direct_report_revision_schema.py tests/unit/prompts/test_direct_report_prompt.py tests/unit/analysis/test_direct_report_pipeline.py tests/unit/analysis/test_direct_report_sections.py tests/integration/test_single_report_runner.py tests/integration/test_content_api.py tests/integration/test_imported_analysis_publication.py -q
```

- [ ] **Step 2: Run the full backend suite**

Run: `cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q`

Expected: exit 0 with no failures.

- [ ] **Step 3: Run frontend unit tests and production build**

Run: `cd prototype && node --test tests/*.test.mjs && npm run build`

Expected: exit 0 with no failures or build errors.

- [ ] **Step 4: Run offline Prompt release gates**

```bash
cd backend && UV_CACHE_DIR=../.uv-cache uv run python ../scripts/evaluate-prompts.py --fixture tests/fixtures/prompt-eval/multi-scene.json --fixture tests/fixtures/prompt-eval/negative-cases.json --fixture tests/fixtures/prompt-eval/injection.json
```

Expected: exit 0, schema validity 100%, and all contamination/leak counters zero.

- [ ] **Step 5: Audit spec coverage and final diff**

Check every acceptance criterion in the spec against a test or measured artifact. Run `git diff --check` and inspect `git status --short`; identify pre-existing unrelated changes separately from feature changes.

- [ ] **Step 6: Commit any verification-only fixes**

Stage only files changed to fix a verified failure and commit with a message naming that failure. Do not bundle unrelated pre-existing worktree changes.
