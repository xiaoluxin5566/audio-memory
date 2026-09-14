# Beta 8 Multi-Scene Report Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan inline and stop at every review checkpoint. Do not dispatch subagents unless the user explicitly asks for delegation.

**Goal:** Build a resumable Beta 8 report pipeline that runs seven full-transcript scene generators, unified evidence audits, cross-card orchestration, optional search, targeted card revision, final audit, and atomic publication without replacing the current report pipeline before acceptance.

**Architecture:** Add a side-by-side `Beta8ReportRunner` selected from a pipeline kind frozen in each `AnalysisVersion`. Keep all Beta 8 model outputs behind strict Pydantic contracts, persist every expensive stage in existing JSON checkpoint columns, reuse the existing provider concurrency and native-search adapters, and extend the atomic publisher with a Beta 8 publication bundle. Existing `SingleReportRunner` remains available and is the default until an explicit release decision changes the active pipeline.

**Tech Stack:** Python 3.12, asyncio, Pydantic 2, SQLAlchemy 2, SQLite, pytest/pytest-asyncio, existing `ProviderAnalysisClient`, existing `VersionPublisher`, React/Vite presentation compatibility.

**Spec:**

- `docs/beta8/pipeline-prompts-v1/README.md`
- `docs/beta8/pipeline-prompts-v1/01-unified-audit-prompt.md`
- `docs/beta8/pipeline-prompts-v1/02-cross-card-orchestration-prompt.md`
- `docs/beta8/pipeline-prompts-v1/03-search-execution-protocol.md`
- `docs/beta8/pipeline-prompts-v1/04-targeted-revision-prompt.md`
- `docs/beta8/pipeline-prompts-v1/05-runtime-json-contracts.md`

## Global Constraints

- Work in a new isolated worktree created from committed baseline `e6b5f8c66a9f64699976b6790dcb56a724f2073b`; do not implement in the current dirty `cb74` worktree.
- Before execution, copy only the user-approved Beta 8 docs and golden Prompt files into the isolated worktree and verify their SHA-256 values.
- Do not reset, clean, overwrite, merge, commit, push, tag, publish, or run paid/cloud model calls without separate explicit user authorization.
- All automated tests use fake providers and local fixtures. A real long-audio/model evaluation is a later, separately approved gate.
- Preserve the exact seven scene IDs: `work_communication`, `parenting_family`, `health_state`, `content_consumption`, `inspiration_insight`, `self_growth`, and `life_decisions`.
- Every scene call receives the same complete reliable transcript. Summaries, maps, candidates, and evidence packets may assist later stages but never restrict the scene calls' transcript access.
- No scene-selector model call. Each scene decides and generates V1 in the same call and may return zero, one, or multiple cards.
- Keep user-visible content in complete Markdown. JSON exists only for routing, provenance, audit, search, todos, checkpoint recovery, and publication.
- Initial and final audit use the same Prompt. Transcript splitting produces multiple calls, not multiple maintained audit Prompts.
- Audit merge is deterministic code. It may only remove byte-identical duplicates; semantic issue resolution belongs to cross-card orchestration.
- Search provider/model configuration is independent from the main report provider/model. Search unavailability degrades to transcript-only output.
- Never send unaffected cards to the revision model. Their final Markdown hash must exactly equal their V1 Markdown hash.
- A required transcript-based revision failure, incomplete audit, unresolved orchestration conflict, or final-audit failure prevents publication and preserves the last valid version.
- No database migration is planned: use existing `staged_results_json`, `pipeline_checkpoints_json`, `pipeline_parameters_json`, `pipeline_metrics_json`, `search_rounds_json`, and `external_sources_json`.

---

### Task 1: Create the isolated execution workspace and freeze approved artifacts

**Files:**

- Copy without modification: `docs/beta8/work-prompt-composition-review-v3-1/03-work-composed-v1-generation-prompt.md`
- Copy without modification: `docs/beta8/seven-scene-golden-prompts/*.md`
- Copy without modification: `docs/beta8/pipeline-prompts-v1/*.md`
- Copy without modification: `docs/beta8/BETA8-REPORT-PIPELINE-IMPLEMENTATION-PLAN.md`
- Create: `docs/beta8/baselines/beta8-pipeline-approved-inputs.sha256`

**Interfaces:**

- Consumes: committed baseline `e6b5f8c66a9f64699976b6790dcb56a724f2073b` plus user-approved uncommitted design artifacts from `cb74`.
- Produces: an isolated implementation worktree whose imported design inputs are protected by a manifest.

- [ ] **Step 1: Load and follow the worktree isolation instructions**

Read `superpowers:using-git-worktrees` before creating the worktree. Present the exact target path and branch name to the user and obtain explicit approval because branch/worktree creation changes Git state.

- [ ] **Step 2: Create the new worktree from the exact baseline**

Recommended branch name: `codex/beta8-report-pipeline`. Recommended sibling path: `/Users/liujinxin/.codex/worktrees/beta8-report-pipeline/音频Always on Demo`.

Do not create it from the current working tree state and do not carry unrelated dirty files.

- [ ] **Step 3: Import only the approved Beta 8 artifacts**

Use `apply_patch` for any text-file addition. Do not copy the entire `docs/beta8` directory blindly. Compare every imported file with its approved source using `shasum -a 256`.

- [ ] **Step 4: Write and verify the approved-input manifest**

The manifest must list every imported golden scene Prompt and every `pipeline-prompts-v1` document. Run:

```bash
shasum -a 256 -c docs/beta8/baselines/beta8-pipeline-approved-inputs.sha256
```

Expected: every listed file reports `OK`.

- [ ] **Step 5: Stop for workspace review**

Show `git status --short --branch`, the manifest verification output, and the exact imported file list. Do not commit.

---

### Task 2: Add strict Beta 8 Pydantic contracts

**Files:**

- Create: `backend/src/audio_memory/prompts/beta8_scene_schema.py`
- Create: `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`
- Create: `backend/tests/unit/prompts/test_beta8_scene_schema.py`
- Create: `backend/tests/unit/prompts/test_beta8_pipeline_schema.py`

**Interfaces:**

- Consumes: field contract in `docs/beta8/pipeline-prompts-v1/05-runtime-json-contracts.md`.
- Produces: `Beta8SceneResult`, `Beta8AuditResult`, `AggregatedAudit`, `Beta8OrchestrationResult`, `Beta8SearchPacket`, `Beta8RevisedCard`, and `Beta8PublicationBundle`.

- [ ] **Step 1: Write failing scene-contract tests**

Add tests named `test_scene_result_keeps_markdown_thin_and_allows_zero_cards`, `test_scene_result_rejects_unknown_fields`, `test_scene_result_rejects_unknown_segment_ids_during_runtime_validation`, `test_all_seven_scene_ids_are_exact`, `test_markdown_requires_first_nonempty_h1`, and `test_markdown_title_and_core_summary_are_extracted_deterministically`. Construct one valid zero-card result and one valid Markdown card, then mutate exactly one field per negative assertion so every failure has a single cause.

The schema itself does not know the current transcript ID set; add a pure validator:

The runtime validator has the exact signature `validate_scene_result_ids(result: Beta8SceneResult, *, known_segment_ids: set[str]) -> None` and raises `ValueError` listing sorted unknown IDs.

- [ ] **Step 2: Run scene-contract tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/unit/prompts/test_beta8_scene_schema.py -q
```

Expected: collection or import failure because the new schema module does not exist.

- [ ] **Step 3: Implement the minimal scene contracts**

Use strict frozen models:

```python
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Beta8SceneCard(_StrictModel):
    markdown: str = Field(min_length=1, max_length=80_000)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=2_000)
    search_candidates: list[Beta8SearchCandidate] = Field(
        default_factory=list, max_length=5
    )


class Beta8SceneResult(_StrictModel):
    cards: list[Beta8SceneCard] = Field(default_factory=list, max_length=50)
    todo_candidates: list[Beta8TodoCandidate] = Field(default_factory=list, max_length=200)
    skip_reason: str | None = Field(default=None, max_length=1_000)
```

Require `skip_reason` when `cards=[]` and require `skip_reason=null` when cards exist.

Add a pure `parse_beta8_card_markdown(markdown: str) -> ParsedCardMarkdown` helper. It requires the first non-empty line to be `# <title>` and extracts the core summary from the content before the next Markdown heading, after removing an optional standalone “核心摘要” label. It does not reorganize or rewrite Markdown.

- [ ] **Step 4: Write failing pipeline-contract tests**

Cover every invariant from Sections 4–9 of the runtime contract, including:

Add tests named `test_keep_decision_has_one_source_and_no_revision_task`, `test_merge_requires_at_least_two_source_cards`, `test_create_requires_missed_value_issue`, `test_every_input_card_is_consumed_exactly_once`, `test_every_audit_issue_has_exactly_one_decision`, `test_every_todo_candidate_is_kept_or_dropped_once`, `test_revision_requirement_partition_is_exact`, `test_failed_search_packet_has_no_sources_or_answer`, and `test_publish_bundle_rejects_changed_untouched_card_hash`. Each test starts from one shared valid fixture and changes only the invariant named by the test.

- [ ] **Step 5: Implement pipeline contracts and cross-object validators**

Keep Pydantic field validation in the model and input-set validation in pure functions:

Implement `validate_orchestration_against_inputs(result: Beta8OrchestrationResult, *, card_ids: set[str], audit_issue_ids: set[str], search_candidate_ids: set[str]) -> None` and `validate_publication_bundle(bundle: Beta8PublicationBundle) -> None`. Both raise `ValueError` with the violated invariant and sorted offending IDs.

- [ ] **Step 6: Run contract tests**

```bash
cd backend
uv run pytest \
  tests/unit/prompts/test_beta8_scene_schema.py \
  tests/unit/prompts/test_beta8_pipeline_schema.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Stop for contract review**

Run `git diff --check` and show the generated model JSON Schemas. Do not commit.

---

### Task 3: Package exact Prompts and build the Beta 8 composer

**Files:**

- Create: `backend/src/audio_memory/prompts/beta8/scenes/work-communication.md`
- Create: `backend/src/audio_memory/prompts/beta8/scenes/parenting-family.md`
- Create: `backend/src/audio_memory/prompts/beta8/scenes/health-state.md`
- Create: `backend/src/audio_memory/prompts/beta8/scenes/content-consumption.md`
- Create: `backend/src/audio_memory/prompts/beta8/scenes/inspiration-insight.md`
- Create: `backend/src/audio_memory/prompts/beta8/scenes/self-growth.md`
- Create: `backend/src/audio_memory/prompts/beta8/scenes/life-decisions.md`
- Create: `backend/src/audio_memory/prompts/beta8/unified-audit.md`
- Create: `backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md`
- Create: `backend/src/audio_memory/prompts/beta8/search-execution.md`
- Create: `backend/src/audio_memory/prompts/beta8/targeted-revision.md`
- Create: `backend/src/audio_memory/prompts/beta8_composer.py`
- Create: `backend/tests/unit/prompts/test_beta8_composer.py`

**Interfaces:**

- Consumes: approved Prompt documents and Pydantic models from Task 2.
- Produces: `Beta8ModelRequest` and `Beta8PromptComposer` methods used by the runner.

- [ ] **Step 1: Write failing manifest and composition tests**

Tests must verify exact file hashes, exact seven-scene order, schema injection, untrusted-data wrapping, and complete-transcript equality. Add tests named `test_all_scene_requests_receive_identical_full_transcript`, `test_prompt_manifest_contains_every_runtime_prompt`, `test_prompt_data_is_wrapped_as_untrusted`, and `test_audit_initial_and_final_use_same_prompt_file`.

```python
SCENE_IDS = (
    "work_communication",
    "parenting_family",
    "health_state",
    "content_consumption",
    "inspiration_insight",
    "self_growth",
    "life_decisions",
)

```

- [ ] **Step 2: Run the tests and verify failure**

```bash
cd backend
uv run pytest tests/unit/prompts/test_beta8_composer.py -q
```

Expected: import failure for `beta8_composer`.

- [ ] **Step 3: Copy approved Prompt text exactly**

Do not rephrase while packaging. The work scene must match the approved V3.1 full Prompt; the other six must match `seven-scene-golden-prompts`; downstream Prompts must match `pipeline-prompts-v1`.

- [ ] **Step 4: Implement the composer**

Use one request type:

```python
@dataclass(frozen=True, slots=True)
class Beta8ModelRequest:
    scene_id: str
    instructions: str
    user_data: str
    schema_json: str
    max_tokens: int
    timeout_seconds: float
    segment_count: int


class Beta8PromptComposer:
    """Compose strict requests from packaged prompts and generated schemas."""
```

Implement methods named `compose_scene`, `compose_audit`, `compose_orchestration`, and `compose_revision`, each returning `Beta8ModelRequest`; implement class methods `prompt_manifest() -> Sequence[dict[str, str]]` and `fixed_rules_hash() -> str`. Each method accepts only the stage inputs defined in `05-runtime-json-contracts.md`, serializes them with sorted keys, and wraps them in stage-specific untrusted XML tags.

`compose_scene` must receive a single prebuilt `transcript_markdown` value; every scene request uses that exact string. Append the shared runtime formatting contract requiring one leading H1 and a non-empty core summary before the next heading; do not otherwise modify the approved golden Prompt text.

- [ ] **Step 5: Run composer tests and manifest checks**

```bash
cd backend
uv run pytest tests/unit/prompts/test_beta8_composer.py -q
```

Expected: all tests pass and every packaged Prompt hash matches its approved source.

- [ ] **Step 6: Stop for Prompt packaging review**

Show the manifest and the composed request sections. Do not commit.

---

### Task 4: Implement audit-unit planning and deterministic aggregation

**Files:**

- Create: `backend/src/audio_memory/analysis/beta8_audit.py`
- Create: `backend/tests/unit/analysis/test_beta8_audit.py`

**Interfaces:**

- Consumes: reliable transcript segments, complete current card set, `Beta8AuditResult`.
- Produces: `AuditUnit`, `AggregatedAudit`, deterministic audit IDs, and coverage validation.

- [ ] **Step 1: Write failing planning tests**

Tests must show that long transcripts split into consecutive non-overlapping chunks and always add one global unit. Name them `test_plan_audit_units_covers_every_segment_exactly_once`, `test_plan_audit_units_adds_one_global_report_unit`, and `test_global_unit_contains_all_cards_and_no_transcript_claim`.

- [ ] **Step 2: Write failing aggregation tests**

Add `test_aggregate_requires_every_expected_unit`, `test_aggregate_rejects_wrong_unit_id`, `test_aggregate_only_deduplicates_byte_identical_issues`, and `test_semantically_similar_issues_both_survive_for_orchestration`. Use two issue fixtures with identical meaning but different `affected_excerpt` values to prove that semantic similarity does not trigger deletion.

- [ ] **Step 3: Implement audit planning**

Reuse `partition_transcript_for_audit` for transcript sizing, but define Beta 8 unit identity independently:

```python
@dataclass(frozen=True, slots=True)
class AuditUnit:
    audit_unit_id: str
    phase: AuditPhase
    scope: AuditScope
    transcript_segments: Sequence[dict[str, object]]
```

Implement `plan_audit_units(transcript: Sequence[dict[str, object]], *, phase: AuditPhase, max_markdown_chars: int) -> Sequence[AuditUnit]`. IDs use `beta8:{phase}:evidence:{one_based_index}` and `beta8:{phase}:global` so restart reconstruction is deterministic.

- [ ] **Step 4: Implement deterministic aggregation**

Implement `aggregate_audit_results(expected_units: Sequence[AuditUnit], results: Sequence[Beta8AuditResult]) -> AggregatedAudit`.

Hash the canonical exact issue payload to detect byte-identical duplicates. Never use fuzzy matching or an LLM in this function.

- [ ] **Step 5: Run audit tests**

```bash
cd backend
uv run pytest tests/unit/analysis/test_beta8_audit.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Stop for audit review**

Show a fixture where two similar issues remain separate and exact duplicates collapse. Do not commit.

---

### Task 5: Implement bounded search execution and degradation

**Files:**

- Create: `backend/src/audio_memory/analysis/beta8_search.py`
- Create: `backend/tests/unit/analysis/test_beta8_search.py`
- Modify: `backend/src/audio_memory/analysis/provider.py` only if the committed `native_search` return type lacks data required by the approved search packet.

**Interfaces:**

- Consumes: validated `SearchTask` values and an injected search provider/model selection.
- Produces: one checkpointable `Beta8SearchPacket` per task without changing cards.

- [ ] **Step 1: Write failing executor tests with a fake provider**

Add async tests named `test_search_executor_skips_network_for_zero_tasks`, `test_search_executor_uses_independent_provider_and_model`, `test_search_executor_persists_success_partial_and_failed_packets`, `test_one_failed_task_does_not_discard_successful_tasks`, and `test_search_executor_never_retries_completed_task`. The fake provider records `(provider_id, model_id, queries)` and raises if it receives the report provider ID.

- [ ] **Step 2: Run tests and verify failure**

```bash
cd backend
uv run pytest tests/unit/analysis/test_beta8_search.py -q
```

- [ ] **Step 3: Implement the executor around the existing native-search interface**

Implement class `Beta8SearchExecutor` with constructor `(provider, *, provider_id: str | None, model_id: str | None)` and async method `execute(tasks: Sequence[Beta8SearchTask], *, completed: Mapping[str, Beta8SearchPacket], persist: Callable[[Beta8SearchPacket], Awaitable[None]]) -> Sequence[Beta8SearchPacket]`.

If `provider_id` is absent, return one `failed` packet per task with an internal degraded reason. Do not fall back to the report provider implicitly.

- [ ] **Step 4: Normalize real provider citations deterministically**

Reuse the existing source-ID derivation in `analysis/native_search.py`. Validate non-empty URL, provider result identity, and source/task association before producing `supported_points`.

- [ ] **Step 5: Run search tests**

```bash
cd backend
uv run pytest \
  tests/unit/analysis/test_beta8_search.py \
  tests/unit/analysis/test_native_search.py \
  tests/unit/analysis/test_native_search_adapters.py -q
```

Expected: all tests pass without external requests.

- [ ] **Step 6: Stop for search review**

Show success, partial failure, full failure, and resume fixtures. Do not commit.

---

### Task 6: Build the resumable Beta8ReportRunner

**Files:**

- Create: `backend/src/audio_memory/analysis/beta8_runner.py`
- Create: `backend/src/audio_memory/analysis/beta8_state.py`
- Create: `backend/tests/integration/test_beta8_report_runner.py`

**Interfaces:**

- Consumes: `Beta8PromptComposer`, provider client, `Beta8SearchExecutor`, publisher, database, and frozen version parameters.
- Produces: staged Beta 8 artifacts, complete pipeline metrics, and a validated publication bundle.

- [ ] **Step 1: Define and test checkpoint keys**

Use this stable namespace in `staged_results_json`:

```python
BETA8_STAGE_KEYS = (
    "beta8_scene_v1",
    "beta8_initial_audit_units",
    "beta8_initial_audit_aggregate",
    "beta8_orchestration",
    "beta8_search_packets",
    "beta8_revised_cards",
    "beta8_final_candidate",
    "beta8_final_audit_units",
    "beta8_final_audit_aggregate",
    "beta8_publication_ready",
)
```

Tests must reject checkpoints whose prompt hash, transcript fingerprint, schema version, provider generation, or upstream artifact hash no longer matches.

- [ ] **Step 2: Write failing seven-scene fan-out tests**

Add async tests named `test_runner_calls_all_seven_scenes_in_parallel`, `test_each_scene_receives_byte_identical_full_transcript`, `test_zero_card_scene_is_valid`, and `test_one_scene_failure_prevents_downstream_calls_and_is_resumable`. The concurrency test blocks all seven fake calls until every scene has registered its start.

The fake provider should block each scene on an `asyncio.Event` so the test proves concurrent start, not merely eventual completion.

- [ ] **Step 3: Implement scene generation and checkpointing**

Implement `Beta8ReportRunner.__init__(*, database: Database, provider, publisher, generation_source, search_executor: Beta8SearchExecutor) -> None` and `Beta8ReportRunner.run(version_id: str, worker_owner_id: str | None = None) -> AnalysisOutcome`.

Use `asyncio.gather` and call `provider.generate` with `allow_parallel=True`. Persist each completed scene independently so a retry only reruns missing scenes.

- [ ] **Step 4: Write failing initial-audit and orchestration tests**

Add async tests named `test_runner_uses_one_audit_prompt_for_chunk_and_global_units`, `test_runner_aggregates_without_merge_model_call`, `test_runner_calls_orchestration_once_with_all_cards_and_issues`, and `test_unresolved_orchestration_conflict_stops_pipeline`. The fake provider must fail the test if it sees a scene ID or request label for an audit-merge model call.

- [ ] **Step 5: Implement initial audit and orchestration**

Parse every model result through strict Pydantic models, then run input-set validators. Persist raw validated model output, its canonical hash, elapsed time, and provider diagnostics at every checkpoint.

- [ ] **Step 6: Write failing revision and final-audit tests**

Add async tests named `test_keep_card_is_never_sent_to_revision_model`, `test_keep_card_final_hash_equals_v1_hash`, `test_revision_receives_target_scene_golden_prompt`, `test_search_failure_still_runs_transcript_required_revision`, `test_unresolved_revision_requirement_prevents_final_audit`, `test_final_requirements_are_routed_to_units_with_their_evidence`, `test_every_requirement_is_checked_by_at_least_one_unit`, `test_final_audit_reuses_unified_audit_prompt`, and `test_any_final_issue_prevents_publication`.

- [ ] **Step 7: Implement revision, final candidate assembly, and final audit**

For every card decision:

```python
if decision.operation == "keep":
    final_card = source_card
    assert sha256(final_card.markdown.encode()).hexdigest() == v1_hash
elif decision.operation == "drop":
    continue
else:
    final_card = await revise_one_card(decision)
```

Search failures remove only external-source enrichment from the revision input. They do not cancel transcript-grounded audit requirements, merge requirements, or missing-card repairs.

Before final audit, route transcript-grounded requirements to every `evidence_chunk` containing their `evidence_segment_ids`, and route global structure, duplication, ordering, and scene-boundary requirements to `global_report`. Reject a final-audit plan if any requirement has no applicable unit.

- [ ] **Step 8: Persist metrics and recovery state**

Metrics must count each actual model/search call for this version only. Set `web_search_performed=true` only if at least one provider search call actually executes; record a degraded reason when planned search was unavailable or failed.

- [ ] **Step 9: Run runner integration tests**

```bash
cd backend
uv run pytest tests/integration/test_beta8_report_runner.py -q
```

Expected: all tests pass using fake providers and a temporary SQLite database.

- [ ] **Step 10: Stop for stage/checkpoint review**

Show a staged-results fixture for normal success, search degradation, and restart-after-scene-4. Do not commit.

---

### Task 7: Add Beta 8 atomic publication

**Files:**

- Modify: `backend/src/audio_memory/analysis/publisher.py`
- Create: `backend/tests/integration/test_beta8_publisher.py`
- Modify: `backend/src/audio_memory/content/service.py` only if Beta 8 evidence/source lookup cannot be read from the staged bundle without a special case.

**Interfaces:**

- Consumes: validated `Beta8PublicationBundle` plus existing worker lease and `AnalysisVersion`.
- Produces: ordered `Card` rows, reconciled global todos, external-source persistence, completed version state, and finalized audio in one publication transaction.

- [ ] **Step 1: Write failing publication tests**

Add async tests named `test_beta8_publish_writes_one_card_row_per_final_card`, `test_beta8_publish_preserves_scene_id_and_markdown`, `test_beta8_publish_rejects_unknown_segment_and_source_ids`, `test_beta8_publish_reconciles_global_todos_once`, `test_beta8_publish_is_atomic_on_card_failure`, `test_beta8_publish_is_idempotent_after_completion`, and `test_failed_final_audit_never_calls_publish`.

- [ ] **Step 2: Run tests and verify failure**

```bash
cd backend
uv run pytest tests/integration/test_beta8_publisher.py -q
```

- [ ] **Step 3: Add an explicit Beta 8 publisher entry point**

Prefer an explicit method over extending the old six-scene `_validated_scenes` path:

Add `VersionPublisher.publish_beta8(version_id: str, bundle: Beta8PublicationBundle, *, worker_owner_id: str | None = None) -> AnalysisOutcome`.

Reuse the existing worker fence, audio finalization, todo-candidate insertion/reconciliation, version completion, and transaction pattern. Do not make Beta 8 pretend to be the legacy `PROMPT_SCENES` list.

- [ ] **Step 4: Persist each final card in the current presentation envelope**

Each `Card.payload_json` should remain compatible with the content API:

```python
payload = {
    "scene_id": final_card.scene_id,
    "cards": [{
        "title": final_card.title,
        "summary": final_card.summary,
        "evidence_segment_ids": final_card.source_segment_ids,
        "external_source_ids": final_card.used_source_ids,
    }],
    "reportMarkdown": final_card.markdown,
    "runtimeMetrics": metrics.model_dump(mode="json"),
}
```

Title and summary extraction must be deterministic from the validated Markdown contract; do not add a model call during publication.

- [ ] **Step 5: Persist search and todo state**

Write canonical search task/packet data to `search_rounds_json`, canonical sources to `external_sources_json`, and the final todo candidates through existing reconciliation. Reject any source referenced by a card but absent from canonical sources.

- [ ] **Step 6: Run publication and existing regression tests**

```bash
cd backend
uv run pytest \
  tests/integration/test_beta8_publisher.py \
  tests/unit/analysis/test_day_map_publisher.py \
  tests/integration/test_single_report_runner.py -q
```

Expected: Beta 8 tests and legacy publication/report tests all pass.

- [ ] **Step 7: Stop for database diff review**

Show rows produced by one fixture and prove there is no migration. Do not commit.

---

### Task 8: Freeze pipeline kind and route old/new versions safely

**Files:**

- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Create: `backend/src/audio_memory/analysis/version_runner_router.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/config.py`
- Modify: `backend/src/audio_memory/reanalysis/worker.py`
- Create: `backend/tests/unit/analysis/test_version_runner_router.py`
- Modify: `backend/tests/unit/analysis/test_task_coordinator.py`
- Modify: `backend/tests/integration/test_upload_jobs.py`

**Interfaces:**

- Consumes: active pipeline configuration and both runner implementations.
- Produces: `pipeline_kind` frozen per analysis version and safe dispatch after restart.

- [ ] **Step 1: Write failing routing tests**

Add async tests named `test_legacy_version_without_pipeline_kind_uses_single_runner`, `test_beta8_version_uses_beta8_runner_after_restart`, `test_retry_keeps_original_pipeline_kind_and_prompt_manifest`, and `test_unknown_pipeline_kind_fails_closed`.

- [ ] **Step 2: Add validated configuration**

Support:

```text
AUDIO_MEMORY_REPORT_PIPELINE=single_report_v1|beta8_multi_scene_v1
AUDIO_MEMORY_BETA8_SEARCH_PROVIDER=<provider id or empty>
AUDIO_MEMORY_BETA8_SEARCH_MODEL=<model id or empty>
```

Default `AUDIO_MEMORY_REPORT_PIPELINE` to `single_report_v1` during implementation. Empty search provider means transcript-only degradation, not implicit use of the report provider.

- [ ] **Step 3: Freeze pipeline identity in the existing parameters JSON**

Add `pipeline_kind` to `AnalysisRequest`, defaulting legacy reconstructed requests to `single_report_v1`. Store these fields in `pipeline_parameters_json`:

```python
{
    "pipeline_kind": request.pipeline_kind,
    "provider_id": request.provider_id,
    "model_id": request.model_id,
    "search_provider_id": request.search_provider_id,
    "search_model_id": request.search_model_id,
    "credential_generation": request.credential_generation,
    "fixed_rules_hash": fixed_rules_hash,
    "prompt_manifest": prompt_manifest,
}
```

The pipeline-specific fixed-rules hash and manifest must come from the selected composer, not always `PromptComposer`.

- [ ] **Step 4: Implement the router**

Implement `VersionRunnerRouter.__init__(*, database: Database, runners: Mapping[str, VersionRunner]) -> None` and the following dispatch logic:

```python
async def run(self, version_id: str, worker_owner_id: str):
    pipeline_kind = await self._pipeline_kind(version_id)
    runner = self.runners.get(pipeline_kind)
    if runner is None:
        raise ValueError(f"Unknown report pipeline: {pipeline_kind}")
    return await runner.run(version_id, worker_owner_id)
```

Versions created before this field existed route to `single_report_v1`. Unknown values raise a retryable configuration error before any model call.

- [ ] **Step 5: Wire both runners without switching the default**

Instantiate `SingleReportRunner`, `Beta8ReportRunner`, `Beta8SearchExecutor`, and `VersionRunnerRouter` in `main.py`; pass the router to `AnalysisTaskCoordinator.start()`.

- [ ] **Step 6: Preserve history reanalysis identity**

Reanalysis must use the source version's pipeline kind, report/search provider snapshots, Prompt manifest, and fixed-rules hash. It must never silently upgrade an old report to Beta 8.

- [ ] **Step 7: Run routing and upload tests**

```bash
cd backend
uv run pytest \
  tests/unit/analysis/test_version_runner_router.py \
  tests/unit/analysis/test_task_coordinator.py \
  tests/integration/test_upload_jobs.py -q
```

Expected: old and new versions route deterministically; default remains old pipeline.

- [ ] **Step 8: Stop for activation review**

Show configuration defaults, version parameter snapshots, and restart routing. Do not activate Beta 8 or commit.

---

### Task 9: Add frontend compatibility for seven scene cards

**Files:**

- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css` only for labels/colors required to render new scenes legibly.
- Create: `prototype/tests/beta8-card-state.test.mjs`
- Modify: `prototype/tests/detail-layout.test.mjs`

**Interfaces:**

- Consumes: current content API payload envelope with new `scene_id` values and per-card `reportMarkdown`.
- Produces: readable list/detail cards for all seven scenes without changing Markdown content.

- [ ] **Step 1: Write failing state-normalization tests**

```javascript
test('normalizes one Beta 8 markdown card per payload row', () => {})
test('maps all seven scene ids to user-visible Chinese labels', () => {})
test('preserves reportMarkdown and external sources unchanged', () => {})
```

- [ ] **Step 2: Run tests and verify failure**

```bash
npm --prefix prototype test -- beta8-card-state.test.mjs detail-layout.test.mjs
```

- [ ] **Step 3: Add exact scene labels**

```javascript
const BETA8_SCENE_LABELS = {
  work_communication: '工作与沟通',
  parenting_family: '亲子与家庭',
  health_state: '健康状态',
  content_consumption: '内容消费',
  inspiration_insight: '灵感与洞察',
  self_growth: '自我成长',
  life_decisions: '生活决策',
}
```

Do not add fixed content sections or transform card Markdown.

- [ ] **Step 4: Run frontend tests**

```bash
npm --prefix prototype test -- beta8-card-state.test.mjs detail-layout.test.mjs
```

Expected: tests pass.

- [ ] **Step 5: Stop for UI compatibility review**

Use fixture data only. Do not launch paid analysis, package the app, or commit.

---

### Task 10: Prove recovery, degradation, atomicity, and legacy safety

**Files:**

- Create: `backend/tests/fixtures/beta8/zero-cards.json`
- Create: `backend/tests/fixtures/beta8/cross-scene-overlap.json`
- Create: `backend/tests/fixtures/beta8/audit-duplicate-and-conflict.json`
- Create: `backend/tests/fixtures/beta8/search-partial-failure.json`
- Create: `backend/tests/fixtures/beta8/targeted-revision.json`
- Create: `backend/tests/e2e/test_beta8_pipeline_contract.py`
- Modify only if required by verified regressions: existing tests named in earlier tasks.

**Interfaces:**

- Consumes: complete side-by-side Beta 8 implementation.
- Produces: local evidence that every required stage and failure boundary behaves as designed.

- [ ] **Step 1: Add an end-to-end fake-provider success test**

Assert exact call order by stage, seven concurrent scene starts, one orchestration call, zero or more searches, revisions only for affected cards, unified final audit, and one publication.

- [ ] **Step 2: Add restart tests at every expensive checkpoint**

Parameterized test stages:

```python
@pytest.mark.parametrize("checkpoint", [
    "scene_v1_partial",
    "initial_audit_partial",
    "orchestration_complete",
    "search_partial",
    "revision_partial",
    "final_audit_partial",
])
async def test_restart_resumes_without_repeating_completed_calls(checkpoint, harness):
    first_counts = await harness.run_until(checkpoint)
    final_counts = await harness.restart_and_complete()
    assert final_counts.completed_stage_calls == first_counts.completed_stage_calls
    assert final_counts.publication_calls == 1
```

- [ ] **Step 3: Add failure-boundary tests**

Prove:

- scene, audit, orchestration, required revision, and final-audit failures do not publish;
- search failure produces a complete transcript-only candidate;
- one failed search does not discard successful source packets;
- unresolved conflicts block publication;
- unknown evidence/source IDs fail closed;
- previous completed batch remains current after a failed new analysis;
- publication transaction rollback leaves no partial Beta 8 rows.

- [ ] **Step 4: Add prompt-injection fixtures**

Include transcript text and web snippets that attempt to alter scene, audit, orchestration, search, and revision instructions. Assert they remain inside untrusted input envelopes and do not become executable Prompt text.

- [ ] **Step 5: Run the focused complete suite**

```bash
cd backend
uv run pytest \
  tests/unit/prompts/test_beta8_scene_schema.py \
  tests/unit/prompts/test_beta8_pipeline_schema.py \
  tests/unit/prompts/test_beta8_composer.py \
  tests/unit/analysis/test_beta8_audit.py \
  tests/unit/analysis/test_beta8_search.py \
  tests/unit/analysis/test_version_runner_router.py \
  tests/integration/test_beta8_report_runner.py \
  tests/integration/test_beta8_publisher.py \
  tests/e2e/test_beta8_pipeline_contract.py -q
```

Expected: all focused Beta 8 tests pass with no network access.

- [ ] **Step 6: Run the complete backend regression suite**

```bash
cd backend
uv run pytest -q
```

Expected: all backend tests pass.

- [ ] **Step 7: Run the complete frontend regression suite**

```bash
npm --prefix prototype test
```

Expected: all prototype tests pass.

- [ ] **Step 8: Run static protection checks**

```bash
git diff --check
shasum -a 256 -c docs/beta8/baselines/beta8-pipeline-approved-inputs.sha256
git status --short --branch
```

Expected: no whitespace errors; all approved design/Prompt inputs report `OK`; only reviewed Beta 8 implementation files are changed.

- [ ] **Step 9: Stop before any real evaluation or activation**

Deliver test output, staged fixture examples, changed-file list, and remaining risks. Do not call real providers, switch the default pipeline, commit, package, publish, or release.

---

## Post-Implementation Gates Not Authorized by This Plan

These require separate user decisions after local implementation review:

1. run real long-audio evaluations with paid report/search models;
2. compare Prompt effects across representative seven-scene fixtures;
3. choose final search provider/model and budgets;
4. tune Prompt wording, Token limits, concurrency, retries, and card-count behavior;
5. change `AUDIO_MEMORY_REPORT_PIPELINE` default to `beta8_multi_scene_v1`;
6. package, smoke-test, commit, merge, tag, publish, or release Beta 8.
