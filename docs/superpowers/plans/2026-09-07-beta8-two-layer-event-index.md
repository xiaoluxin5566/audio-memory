# Beta 8 Two-Layer Event Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Do not dispatch subagents unless the user explicitly requests delegation.

**Goal:** Replace the model-authored overlapping range index with a two-layer index containing a deterministic, mutually exclusive coverage ledger and a factual event catalog whose embedded evidence may overlap safely.

**Architecture:** The model returns a `Beta8EventIndexDraft` containing primary-session metadata, embedded events, and ordered per-file block starts. A local pure normalizer validates the starts against transcript order, derives all end ranges, adds system exclusions, and produces a canonical `Beta8EventIndex` consumed by V1 and later gates. Ground Truth remains evaluation-only and is never sent to the model or used to repair output.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, existing `Beta8ReportRunner`, JSON Schema generated from Pydantic, Markdown runtime prompts.

**Spec:** `docs/superpowers/specs/2026-09-07-beta8-two-layer-event-index-design.md`

## Global Constraints

- Do not call a paid model, access the network, run search, publish, commit, push, merge, reset, clean, or overwrite historical artifacts while implementing this plan.
- Preserve the dirty isolated worktree and all existing quarantined provider responses.
- The model must not receive Ground Truth or any sample-specific expected count.
- Production code and Prompt must accept any input-derived number of work communications.
- V1 must continue to receive the complete reliable transcript; the index remains navigation-only and is not the factual input boundary.
- Coverage ownership is mutually exclusive; embedded-event evidence may overlap only inside its parent primary session.
- `--stop-after-index` and `--stop-after-v1` must remain single-request paid checkpoints with no hidden repair or transport retry.
- Each production behavior is implemented with a failing test first and the minimum code required to pass it.
- Do not update hashes or manifests until all behavioral tasks are green.

## File Map

- `backend/src/audio_memory/prompts/beta8_event_index_schema.py`: model draft schema and canonical normalized index schema only.
- `backend/src/audio_memory/analysis/beta8_event_index.py`: transcript topology, draft normalization, derived ranges, coverage and reference validation only.
- `backend/src/audio_memory/prompts/beta8/event-index.md`: model instructions for primary sessions, embedded events and ordered block starts.
- `docs/beta8/pipeline-prompts-v2/01-event-index-prompt.md`: byte-identical approved copy of the runtime index Prompt.
- `backend/src/audio_memory/prompts/beta8_composer.py`: bind the draft JSON Schema to the model request and include its policy in `fixed_rules_hash`.
- `backend/src/audio_memory/analysis/beta8_runner.py`: quarantine raw draft, parse, normalize, gate and persist canonical index.
- `backend/src/audio_memory/prompts/beta8_scene_schema.py`: V1 closure over primary sessions and embedded events.
- `backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`: explain the two-layer navigation contract to V1.
- `docs/beta8/pipeline-prompts-v2/02-all-scenes-v1-prompt.md`: byte-identical approved V1 Prompt copy.
- `backend/src/audio_memory/analysis/beta8_evaluation.py`: evaluation-only communication boundary matching against primary sessions.
- `backend/tests/fixtures/beta8/`: compact fixtures for new draft/canonical contracts; do not copy an entire paid raw response into source control.
- `tests/evaluate-beta8-indexed-v1.py`: dry-run and paid-acceptance wiring only.
- `docs/beta8/INDEX-BOUNDARY-FIX-2026-09-07.md`: append implementation and free-verification evidence after tests pass.

---

### Task 1: Define the model draft and canonical two-layer schemas

**Files:**
- Modify: `backend/src/audio_memory/prompts/beta8_event_index_schema.py`
- Modify: `backend/tests/unit/prompts/test_beta8_event_index_schema.py`

**Interfaces:**
- Produces: `Beta8EventIndexDraft`, `Beta8PrimarySessionDraft`, `Beta8EmbeddedEventDraft`, `Beta8FileTimeline`, `Beta8TimelineSessionBlock`, `Beta8TimelineExcludedBlock`.
- Produces: canonical `Beta8EventIndex` with `primary_sessions`, `embedded_events`, `coverage_ranges`, `system_excluded_ranges`.
- Produces: `Beta8EventIndex.routeable_unit_ids` and `Beta8EventIndex.work_communication_session_ids`.

- [ ] **Step 1: Replace the old fixture helper with a minimal valid two-layer draft helper**

```python
def valid_draft_payload() -> dict[str, object]:
    return {
        "input_complete": True,
        "input_error": None,
        "primary_sessions": [{
            "session_id": "session-call",
            "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "contact_started",
            "start_boundary_evidence_segment_ids": ["seg-1"],
            "end_boundary": "contact_ended",
            "end_boundary_evidence_segment_ids": ["seg-2"],
            "subject": "project call",
            "description": "The user joined a project call.",
        }],
        "embedded_events": [],
        "file_timelines": [{
            "source_file": "file-a",
            "blocks": [{
                "start_segment_id": "seg-1",
                "disposition": "session",
                "session_id": "session-call",
            }],
        }],
    }
```

- [ ] **Step 2: Add failing Schema tests for the discriminated timeline union**

```python
def test_timeline_block_requires_exactly_one_disposition_payload() -> None:
    payload = valid_draft_payload()
    block = payload["file_timelines"][0]["blocks"][0]
    block["excluded_reason"] = "noise"
    with pytest.raises(ValidationError):
        Beta8EventIndexDraft.model_validate(payload)


def test_excluded_block_forbids_session_id() -> None:
    payload = valid_draft_payload()
    payload["file_timelines"][0]["blocks"][0] = {
        "start_segment_id": "seg-1",
        "disposition": "excluded",
        "excluded_reason": "noise",
        "session_id": "session-call",
    }
    with pytest.raises(ValidationError):
        Beta8EventIndexDraft.model_validate(payload)
```

- [ ] **Step 3: Run the two tests and verify RED**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_event_index_schema.py -q`

Expected: collection/import failure because `Beta8EventIndexDraft` and timeline block types do not exist.

- [ ] **Step 4: Implement the strict discriminated block models and draft containers**

```python
class Beta8TimelineSessionBlock(_StrictModel):
    start_segment_id: str = Field(min_length=1, max_length=500)
    disposition: Literal["session"]
    session_id: str = Field(min_length=1, max_length=500)


class Beta8TimelineExcludedBlock(_StrictModel):
    start_segment_id: str = Field(min_length=1, max_length=500)
    disposition: Literal["excluded"]
    excluded_reason: Literal["noise", "duplicate", "unintelligible", "empty"]


Beta8TimelineBlock = Annotated[
    Beta8TimelineSessionBlock | Beta8TimelineExcludedBlock,
    Field(discriminator="disposition"),
]
```

Implement `Beta8PrimarySessionDraft`, `Beta8EmbeddedEventDraft`, `Beta8FileTimeline`, and `Beta8EventIndexDraft` with the exact fields and bounds from the approved spec.

```python
class Beta8PrimarySessionDraft(_StrictModel):
    session_id: str = Field(min_length=1, max_length=500)
    activity_kind: ActivityKind
    communication_purpose: CommunicationPurpose
    participation_mode: ParticipationMode
    start_boundary: StartBoundary
    start_boundary_evidence_segment_ids: list[str] = Field(min_length=1, max_length=8)
    end_boundary: EndBoundary
    end_boundary_evidence_segment_ids: list[str] = Field(min_length=1, max_length=8)
    subject: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)


class Beta8EmbeddedEventDraft(_StrictModel):
    event_id: str = Field(min_length=1, max_length=500)
    parent_session_id: str = Field(min_length=1, max_length=500)
    event_kind: EmbeddedEventKind
    expression_mode: ExpressionMode
    subject: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)
    evidence_ranges: list[Beta8SegmentRange] = Field(min_length=1, max_length=32)


class Beta8FileTimeline(_StrictModel):
    source_file: str = Field(min_length=1, max_length=1_000)
    blocks: list[Beta8TimelineBlock] = Field(min_length=1, max_length=256)


class Beta8EventIndexDraft(_StrictModel):
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=4_000)
    primary_sessions: list[Beta8PrimarySessionDraft] = Field(default_factory=list, max_length=128)
    embedded_events: list[Beta8EmbeddedEventDraft] = Field(default_factory=list, max_length=256)
    file_timelines: list[Beta8FileTimeline] = Field(default_factory=list)
```

- [ ] **Step 5: Run the Schema test file and verify GREEN**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_event_index_schema.py -q`

Expected: PASS.

- [ ] **Step 6: Add failing tests for canonical routing and work-communication properties**

```python
def test_only_primary_live_work_conversations_enter_work_closure() -> None:
    index = canonical_index_with_primary_and_embedded_events()
    assert index.work_communication_session_ids == ("session-call",)
    assert index.routeable_unit_ids == ("session-call", "embedded-demo")
```

- [ ] **Step 7: Run the property test and verify RED**

Expected: FAIL because the canonical two-layer model and properties do not exist.

- [ ] **Step 8: Implement the canonical models and properties, then verify GREEN**

Use immutable tuples returned in deterministic primary-then-embedded order. Work closure must require primary role, `conversation`, live participation and `work|mixed` purpose.

```python
class Beta8PrimarySession(Beta8PrimarySessionDraft):
    ranges: list[Beta8SegmentRange] = Field(min_length=1, max_length=32)


class Beta8CoverageRange(_StrictModel):
    range: Beta8SegmentRange
    disposition: Literal["session", "excluded"]
    session_id: str | None = None
    excluded_reason: ExcludedReason | None = None
    origin: Literal["model", "system"]


class Beta8EventIndex(_StrictModel):
    input_complete: bool
    input_error: str | None
    primary_sessions: list[Beta8PrimarySession] = Field(max_length=128)
    embedded_events: list[Beta8EmbeddedEventDraft] = Field(max_length=256)
    coverage_ranges: list[Beta8CoverageRange] = Field(max_length=512)
    system_excluded_ranges: list[Beta8CoverageRange] = Field(max_length=128)

    @property
    def routeable_unit_ids(self) -> tuple[str, ...]:
        return tuple(
            [session.session_id for session in self.primary_sessions]
            + [event.event_id for event in self.embedded_events]
        )

    @property
    def work_communication_session_ids(self) -> tuple[str, ...]:
        return tuple(
            session.session_id
            for session in self.primary_sessions
            if session.is_work_communication
        )
```

Run the full Schema test file again. Expected: PASS.

- [ ] **Step 9: Add RED tests for incomplete input and every model-visible bound**

Assert that `input_complete=true` requires `input_error=null`, `input_complete=false` requires a non-empty error and empty content lists, and the Schema rejects 129 primary sessions, 257 embedded events, 257 blocks in one file, 513 blocks globally, 33 embedded evidence ranges and oversized text.

- [ ] **Step 10: Implement the draft-level model validator and verify GREEN**

```python
@model_validator(mode="after")
def validate_completion_and_global_bounds(self) -> "Beta8EventIndexDraft":
    if self.input_complete and self.input_error is not None:
        raise ValueError("input_error must be null when input_complete is true")
    if not self.input_complete:
        if not (self.input_error or "").strip():
            raise ValueError("input_error is required when input_complete is false")
        if self.primary_sessions or self.embedded_events or self.file_timelines:
            raise ValueError("incomplete input cannot contain index content")
    if sum(len(timeline.blocks) for timeline in self.file_timelines) > 512:
        raise ValueError("event index cannot contain more than 512 timeline blocks")
    return self
```

Run the Schema suite. Expected: PASS.

---

### Task 2: Normalize ordered starts into complete, exclusive coverage

**Files:**
- Modify: `backend/src/audio_memory/analysis/beta8_event_index.py`
- Modify: `backend/tests/unit/analysis/test_beta8_event_index.py`

**Interfaces:**
- Consumes: `Beta8EventIndexDraft` from Task 1 and transcript rows.
- Produces: `normalize_event_index_draft(draft: Beta8EventIndexDraft, transcript: Sequence[Mapping[str, object]]) -> Beta8EventIndex`.
- Produces: `EventIndexNormalizationError`, a subtype of `EventIndexCoverageError`.

- [ ] **Step 1: Add a failing test proving end ranges are derived, not model-authored**

```python
def test_normalizer_derives_exclusive_ranges_from_ordered_starts() -> None:
    draft = draft_with_blocks(
        session_block("seg-1", "session-a"),
        excluded_block("seg-3", "noise"),
        session_block("seg-4", "session-b"),
    )
    normalized = normalize_event_index_draft(draft, transcript("seg-1", "seg-2", "seg-3", "seg-4"))
    assert normalized.coverage_ranges == (
        coverage("seg-1", "seg-2", session_id="session-a"),
        exclusion("seg-3", "seg-3", reason="noise"),
        coverage("seg-4", "seg-4", session_id="session-b"),
    )
```

- [ ] **Step 2: Run the test and verify RED**

Expected: FAIL because `normalize_event_index_draft` is not defined.

- [ ] **Step 3: Implement the minimum pure normalizer**

For each file, resolve aliases once, require the first reliable segment as the first start, convert starts to transcript positions, require strict increase, and derive each end as `next_start_position - 1`; the final block ends at the final reliable segment.

```python
def normalize_event_index_draft(
    draft: Beta8EventIndexDraft,
    transcript: Sequence[Mapping[str, object]],
) -> Beta8EventIndex:
    topology = _transcript_topology(transcript)
    resolved = _resolve_file_timelines(draft.file_timelines, topology)
    coverage_ranges = tuple(
        derived
        for timeline in resolved
        for derived in _derive_coverage_ranges(timeline, topology)
    )
    primary_sessions = _materialize_primary_sessions(
        draft.primary_sessions, coverage_ranges
    )
    _validate_embedded_event_containment(
        draft.embedded_events, primary_sessions, topology
    )
    return Beta8EventIndex(
        input_complete=True,
        input_error=None,
        primary_sessions=primary_sessions,
        embedded_events=draft.embedded_events,
        coverage_ranges=coverage_ranges,
        system_excluded_ranges=_system_exclusions(topology),
    )
```

- [ ] **Step 4: Run the test and verify GREEN**

Expected: PASS.

- [ ] **Step 5: Add parameterized RED tests for invalid topology**

Cover unknown file, ambiguous alias, missing file timeline, duplicate file timeline, unknown start, first-start gap, duplicate start, reversed start order, missing session ID target, unreferenced primary session, and a draft timeline for a file with no reliable segments.

```python
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (duplicate_start, "strictly increasing"),
        (unknown_session, "unknown primary session"),
        (missing_first_start, "first reliable segment"),
    ],
)
def test_normalizer_rejects_invalid_timeline_topology(mutate, message: str) -> None:
    draft = valid_draft()
    mutate(draft)
    with pytest.raises(EventIndexNormalizationError, match=message):
        normalize_event_index_draft(draft, valid_transcript())
```

- [ ] **Step 6: Run parameterized tests and verify RED for each new branch**

Expected: failures identify the missing validations, not fixture errors.

- [ ] **Step 7: Implement topology validations one branch at a time**

After each branch, rerun only its parameterized case. Do not silently sort, deduplicate, trim, merge or invent starts; malformed model output must fail closed.

- [ ] **Step 8: Add RED tests for embedded evidence containment and legitimate overlap**

```python
def test_embedded_events_may_overlap_inside_parent_session() -> None:
    normalized = normalize_event_index_draft(draft_with_overlapping_embedded_events(), valid_transcript())
    assert len(normalized.embedded_events) == 2


def test_embedded_event_cannot_escape_parent_session() -> None:
    with pytest.raises(EventIndexNormalizationError, match="outside parent session"):
        normalize_event_index_draft(draft_with_escaped_embedded_event(), valid_transcript())
```

- [ ] **Step 9: Implement containment against the derived parent coverage and verify GREEN**

Every expanded embedded evidence segment must belong to at least one coverage range owned by its `parent_session_id`. Embedded-to-embedded overlap is not an error.

```python
parent_segments = _segments_owned_by_session(primary_sessions, topology)
for event in embedded_events:
    evidence = _expand_ranges(event.evidence_ranges, topology)
    if not evidence <= parent_segments[event.parent_session_id]:
        raise EventIndexNormalizationError(
            f"embedded event {event.event_id} is outside parent session"
        )
```

- [ ] **Step 10: Add RED tests for adjacent conversation boundary evidence**

Require evidence IDs to exist, belong to the corresponding session, and fall within eight reliable segments of the transition between two adjacent, different conversation sessions. Cross-file continuation using the same session ID must not require a synthetic boundary.

- [ ] **Step 11: Implement boundary-evidence proximity checks and verify GREEN**

```python
if previous.is_conversation and current.is_conversation and previous.session_id != current.session_id:
    _require_nearby_boundary_evidence(
        previous.end_boundary_evidence_segment_ids,
        current.start_boundary_evidence_segment_ids,
        transition_position=current_start,
        radius=8,
    )
```

- [ ] **Step 12: Add and implement system-exclusion tests**

Assert that `is_reliable=False` transcript rows are absent from model coverage requirements, are grouped into deterministic `system_excluded_ranges`, and never cause a fabricated ID for a wall-clock interval with no segment.

- [ ] **Step 13: Run the complete event-index unit suite**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_event_index.py -q`

Expected: PASS.

---

### Task 3: Bind the two-layer draft contract into the index Prompt

**Files:**
- Modify: `backend/src/audio_memory/prompts/beta8/event-index.md`
- Modify: `docs/beta8/pipeline-prompts-v2/01-event-index-prompt.md`
- Modify: `backend/src/audio_memory/prompts/beta8_composer.py`
- Modify: `backend/tests/unit/prompts/test_beta8_composer.py`

**Interfaces:**
- Consumes: `Beta8EventIndexDraft.model_json_schema()`.
- Produces: `Beta8PromptComposer.compose_event_index()` whose response contract is the draft, not the canonical index.

- [ ] **Step 1: Add a failing composer test for the two layers and prohibited end ranges**

```python
def test_event_index_prompt_separates_coverage_from_event_evidence() -> None:
    request = Beta8PromptComposer().compose_event_index(transcript_markdown=TRANSCRIPT)
    assert "primary_sessions" in request.schema_json
    assert "embedded_events" in request.schema_json
    assert "file_timelines" in request.schema_json
    assert '"end_segment_id"' not in timeline_block_schema(request.schema_json)
    assert "Ground Truth" not in request.user_data
    assert "5次工作沟通" not in request.instructions
```

- [ ] **Step 2: Run the test and verify RED**

Expected: FAIL because the composer still binds `Beta8EventIndex.model_json_schema()`.

- [ ] **Step 3: Change the composer to bind `Beta8EventIndexDraft`**

Keep `max_tokens=32_000`, `timeout_seconds=300`, and `thinking_enabled=False` unchanged.

```python
schema_json = _canonical_json(Beta8EventIndexDraft.model_json_schema())
return Beta8ModelRequest(
    scene_id="event_index",
    instructions=_with_schema(self._read("event_index"), schema_json),
    user_data=_untrusted(
        "beta8_event_index_data",
        {"transcript_markdown": transcript_markdown},
    ),
    schema_json=schema_json,
    max_tokens=int(policy["max_tokens"] if max_tokens is None else max_tokens),
    timeout_seconds=float(
        policy["timeout_seconds"] if timeout_seconds is None else timeout_seconds
    ),
    segment_count=transcript_markdown.count("<segment "),
    thinking_enabled=False,
)
```

- [ ] **Step 4: Rewrite the runtime Prompt around the approved two-layer responsibilities**

The Prompt must explicitly cover: full factual coverage; primary versus embedded events; no report value selection; file timelines list starts only; exclusion blocks; cross-file shared session ID; topic/file/phase changes do not split conversations; new contact evidence can split adjacent calls; work count is input-derived.

- [ ] **Step 5: Copy the finished runtime Prompt byte-for-byte to the approved source**

Use `apply_patch` for both files. Do not use a shell redirection or a formatting tool that may change bytes unexpectedly.

- [ ] **Step 6: Run composer tests and byte comparison**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_composer.py -q`

Run: `cmp backend/src/audio_memory/prompts/beta8/event-index.md docs/beta8/pipeline-prompts-v2/01-event-index-prompt.md`

Expected: tests PASS and `cmp` exits 0 with no output.

---

### Task 4: Make the Runner quarantine, normalize and persist the canonical index

**Files:**
- Modify: `backend/src/audio_memory/analysis/beta8_runner.py`
- Modify: `backend/tests/integration/test_beta8_report_runner.py`

**Interfaces:**
- Consumes: raw JSON -> `Beta8EventIndexDraft` -> `normalize_event_index_draft(...)` -> `Beta8EventIndex`.
- Persists: canonical `beta8_event_index` only after structural, coverage and injected evaluation gates pass.

- [ ] **Step 1: Add a failing call-order integration test**

```python
@pytest.mark.asyncio
async def test_runner_quarantines_draft_then_normalizes_before_gate_and_checkpoint(tmp_path) -> None:
    events: list[str] = []
    runner = runner_for_two_layer_index(tmp_path, events=events)
    with pytest.raises(Beta8CheckpointPause):
        await runner.run(VERSION_ID, "worker")
    assert events == ["provider", "quarantine", "normalize", "gate", "checkpoint"]
```

- [ ] **Step 2: Run the test and verify RED**

Expected: FAIL because `_parse_event_index` returns the old canonical model and never normalizes a draft.

- [ ] **Step 3: Implement `_parse_event_index_draft` and normalize in `_run_event_index`**

Do not change the order of quarantine: raw provider output must be captured before JSON/Schema/normalization/evaluation failures.

```python
@staticmethod
def _parse_event_index_draft(raw: str) -> Beta8EventIndexDraft:
    try:
        return Beta8EventIndexDraft.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ProviderAnalysisError(
            "Event index draft response is invalid",
            code="model_response_invalid",
        ) from error


raw = await self._generate(version, request)
self._quarantine_response("event_index", raw)
draft = self._parse_event_index_draft(raw)
index = normalize_event_index_draft(draft, transcript)
self._run_event_index_gate(index)
```

- [ ] **Step 4: Run the call-order test and verify GREEN**

Expected: PASS.

- [ ] **Step 5: Add RED tests for failure and checkpoint behavior**

Cover normalization failure: raw retained, no checkpoint, no V1, no repair under `stop_after_event_index_checkpoint`. Cover valid result: canonical checkpoint saved and controlled pause. Cover cached canonical checkpoint: no provider call, canonical validation and injected gate rerun.

- [ ] **Step 6: Implement the minimum Runner changes and verify each case GREEN**

The normal full production path may retain its explicitly configured bounded repair policy, but the acceptance stop must stay at exactly one underlying request.

- [ ] **Step 7: Run the full Runner integration test file**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/integration/test_beta8_report_runner.py -q`

Expected: PASS.

---

### Task 5: Update V1 routing and closure without changing its factual boundary

**Files:**
- Modify: `backend/src/audio_memory/prompts/beta8_scene_schema.py`
- Modify: `backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`
- Modify: `docs/beta8/pipeline-prompts-v2/02-all-scenes-v1-prompt.md`
- Modify: `backend/tests/unit/prompts/test_beta8_scene_schema.py`
- Modify: `backend/tests/unit/prompts/test_beta8_composer.py`

**Interfaces:**
- Consumes: canonical `Beta8EventIndex.routeable_unit_ids` and `work_communication_session_ids`.
- Produces: V1 closure in which every primary session and embedded event is referenced by a card or omitted exactly once.

- [ ] **Step 1: Add failing closure tests**

```python
def test_embedded_event_is_routeable_but_does_not_create_work_card_obligation() -> None:
    index = canonical_index_with_workshop_and_embedded_demo()
    result = v1_result_with_one_work_card(
        source_unit_ids=["session-workshop", "embedded-demo"]
    )
    validate_unified_v1_against_index(result, index)


def test_every_routeable_fact_must_be_used_or_omitted() -> None:
    with pytest.raises(ValueError, match="coverage mismatch"):
        validate_unified_v1_against_index(v1_missing_embedded_fact(), index_with_embedded_fact())
```

- [ ] **Step 2: Run the tests and verify RED**

Expected: FAIL because closure reads `event_index.units` under the old single-layer semantics.

- [ ] **Step 3: Implement routeable-fact closure and primary-only work closure**

Do not infer work obligations from embedded media, third-party examples, quoted questions, work topics or content playback.

```python
routeable_ids = set(event_index.routeable_unit_ids)
work_session_ids = set(event_index.work_communication_session_ids)
used_ids = {
    unit_id
    for scene in result.scene_results
    for card in scene.cards
    for unit_id in card.basis.source_unit_ids
}
omitted_ids = {item.unit_id for item in result.omitted_units}
if used_ids | omitted_ids != routeable_ids or used_ids & omitted_ids:
    raise ValueError("V1 routeable fact coverage mismatch")
_validate_one_work_card_per_primary_session(result, work_session_ids)
```

- [ ] **Step 4: Update the V1 Prompt**

State that `primary_sessions` are real principal activities, `embedded_events` may share evidence with parents, and full transcript evidence overrides index summaries. Preserve all seven scene capability text and the shared report-quality contract.

- [ ] **Step 5: Keep runtime and approved V1 Prompt byte-identical**

Run `cmp` and expect exit 0.

- [ ] **Step 6: Run V1 Schema and composer suites**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_scene_schema.py backend/tests/unit/prompts/test_beta8_composer.py -q`

Expected: PASS.

---

### Task 6: Adapt Ground Truth evaluation without leaking it into production

**Files:**
- Modify: `backend/src/audio_memory/analysis/beta8_evaluation.py`
- Modify: `backend/tests/unit/analysis/test_beta8_evaluation.py`
- Modify: `tests/evaluate-beta8-indexed-v1.py`

**Interfaces:**
- Consumes: canonical primary sessions only.
- Produces: sample-bound post-generation pass/fail metrics; no mutation or corrected checkpoint.

- [ ] **Step 1: Add a failing evaluation test proving embedded work-like content is ignored**

```python
def test_ground_truth_boundary_gate_counts_only_primary_work_sessions() -> None:
    index = index_with_five_primary_work_sessions_and_embedded_work_media()
    metrics = validate_event_index_communication_boundaries(index, ground_truth_with_five_events())
    assert metrics["actual_work_communication_count"] == 5
```

- [ ] **Step 2: Add a failing test proving arbitrary sample counts are supported**

Use Ground Truth fixtures with zero, one and three communications. Zero must be valid rather than rejected as an empty Ground Truth list.

- [ ] **Step 3: Run the evaluation tests and verify RED**

Expected: failures show old `.units` counting and the old non-empty-list assumption.

- [ ] **Step 4: Match only canonical primary work sessions and accept an empty annotated list**

Expected count must always be `len(ground_truth["work_communications"])`; do not add a numeric constant or transmit the list in composer user data.

```python
expected = ground_truth.get("work_communications")
if not isinstance(expected, list):
    raise ValueError("Ground truth work_communications must be a list")
actual = [
    session
    for session in event_index.primary_sessions
    if session.is_work_communication
]
if len(actual) != len(expected):
    raise ValueError(
        "Ground truth communication boundary mismatch: "
        f"expected {len(expected)} work communications, got {len(actual)}"
    )
```

- [ ] **Step 5: Add a wiring assertion for evaluation-only injection**

Verify the production `main.py` Runner still has `event_index_gate=None`, while `tests/evaluate-beta8-indexed-v1.py` injects the hash-bound Ground Truth lambda only after normalization.

- [ ] **Step 6: Run evaluation and acceptance-script unit tests**

Run the narrow evaluation suite and `--dry-run --stop-after-index`. Expected: PASS, `network_accessed=false`, `writes_performed=false`, `provider_transient_total_attempts=1`.

---

### Task 7: Add free regression fixtures for the exact paid failures

**Files:**
- Create: `backend/tests/fixtures/beta8/two-layer-index-cases.json`
- Modify: `backend/tests/unit/analysis/test_beta8_event_index.py`
- Modify: `backend/tests/integration/test_beta8_report_runner.py`

**Interfaces:**
- Produces: compact, synthetic cases derived from failure shapes without copying private transcript prose.

- [ ] **Step 1: Add five named cases to the fixture**

The exact cases are:

1. game primary session with overlapping embedded game/media evidence;
2. two adjacent vehicle calls with distinct boundary evidence;
3. one workshop referenced across two files and several phases;
4. non-work lunch conversation containing career and salary topics;
5. model-excluded noise between two unrelated valid sessions.

- [ ] **Step 2: Add failing parameterized normalization and semantic tests for all five cases**

Each case must assert both the derived canonical ranges and the resulting work-session IDs. The expected work counts come from fixture contents, not a shared constant.

- [ ] **Step 3: Run and verify RED before adding any fixture-specific adapter code**

If a failure suggests special-casing a fixture ID, stop and fix the general schema or normalizer instead.

- [ ] **Step 4: Make only general-purpose fixes required by the cases**

No code may inspect `file2`, `vehicle`, `workshop`, the Doubao source hash, or the expected number five.

- [ ] **Step 5: Verify the latest old paid raw response is rejected as an old contract**

Load `outputs/beta8-indexed-v1/run-2c5b6376-e12f-4c58-94bd-8c47968ad654/quarantine/event_index-07eb01a5-99c9-4bf6-b14d-1efea9eb73ff.provider-response.json` in a read-only test or diagnostic command. Assert it cannot parse as `Beta8EventIndexDraft`; do not modify or copy it.

- [ ] **Step 6: Run both event-index and Runner suites**

Expected: PASS with no network access.

---

### Task 8: Rebind hashes, manifests, dry-runs and documentation

**Files:**
- Modify: `backend/tests/fixtures/beta8/doubao-long-audio-2-manifest.json`
- Modify: `docs/beta8/INDEX-BOUNDARY-FIX-2026-09-07.md`
- Modify only if assertions require it: `backend/tests/e2e/test_beta8_pipeline_contract.py`

**Interfaces:**
- Produces: a single coherent hash boundary for draft Schema, canonical Schema, normalizer policy, Prompts and request policy.

- [ ] **Step 1: Add a failing fixed-rules hash test**

Monkeypatch or substitute each of the following independently and assert the hash changes: draft Schema, canonical Schema, normalizer algorithm version, index Prompt, V1 Prompt, thinking policy, output budget and timeout.

- [ ] **Step 2: Run and verify RED for any missing hash input**

Expected: only omitted dependencies fail.

- [ ] **Step 3: Add an explicit normalizer policy version to the composer hash payload**

Use a stable constant such as `beta8_two_layer_index_normalizer_v1`; do not hash Python source files or absolute paths.

```python
_TWO_LAYER_INDEX_NORMALIZER_POLICY = "beta8_two_layer_index_normalizer_v1"

payload = {
    "prompts": prompt_hashes,
    "schemas": schema_hashes,
    "request_policies": _DEFAULT_REQUEST_POLICIES,
    "event_index_normalizer_policy": _TWO_LAYER_INDEX_NORMALIZER_POLICY,
}
```

- [ ] **Step 4: Recompute current hashes and update the manifest once**

Do this only after all Prompt and Schema changes are final. Record the old and new hashes in the boundary-fix document.

- [ ] **Step 5: Run both no-network dry-runs**

Run:

```text
env PYTHONPATH=backend/src backend/.venv/bin/python tests/evaluate-beta8-indexed-v1.py --dry-run --stop-after-index
env PYTHONPATH=backend/src backend/.venv/bin/python tests/evaluate-beta8-indexed-v1.py --dry-run --stop-after-v1
```

Expected for both: `network_accessed=false`, `writes_performed=false`, correct mutually exclusive stop flag, `provider_transient_total_attempts=1`.

- [ ] **Step 6: Append implementation and verification evidence to the boundary-fix document**

Include exact hashes, exact test counts, fixture coverage, old raw incompatibility, and the statement that no paid model was called.

---

### Task 9: Final verification gate before requesting any paid authorization

**Files:**
- Verify only; change files only to fix a demonstrated regression through a new RED/GREEN cycle.

**Interfaces:**
- Produces: evidence that the implementation is ready for user review, not authorization to spend.

- [ ] **Step 1: Run focused two-layer index suites**

Run Schema, normalizer, composer, V1 closure, evaluation and Runner test files together. Expected: all PASS.

- [ ] **Step 2: Run all Beta 8 tests**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests -k beta8 -q`

Expected: all selected tests PASS.

- [ ] **Step 3: Run provider retry/capture tests**

Run: `env PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_provider.py -q`

Expected: PASS and the single-attempt acceptance test remains green.

- [ ] **Step 4: Run the complete backend suite**

Record exact results. Separate known Flash catalog expectation conflicts and sandbox-only loopback binding restrictions from any new Beta 8 regression; do not fix Flash as part of this plan.

- [ ] **Step 5: Compile and compare approved Prompt copies**

Use `PYTHONPYCACHEPREFIX=/private/tmp/beta8-two-layer-index-pycache` for compilation. Compare event-index, all-scenes V1 and shared-quality runtime/approved pairs byte-for-byte.

- [ ] **Step 6: Run repository hygiene checks**

Run `git diff --check`, inspect `git status --short`, and confirm no historical output changed, no credentials were printed, and no paid/network operation occurred.

- [ ] **Step 7: Perform spec-to-implementation audit**

Check every requirement in sections 3 through 12 of the approved spec against a passing test or an explicit code path. Add no undocumented exception.

- [ ] **Step 8: Stop and report to the user**

Report files changed, tests and hashes, remaining semantic risks, and why the new structure prevents primary coverage overlap while allowing legitimate embedded evidence overlap. Do not run a paid index until the user gives a new explicit authorization after reading that report.
