from __future__ import annotations

from collections import Counter
from hashlib import sha256
from ipaddress import AddressValueError, IPv6Address
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_memory.prompts.beta8_scene_schema import Beta8SceneId, Beta8TodoCandidate


AuditPhase = Literal["initial", "final"]
AuditScope = Literal["evidence_chunk", "global_report"]
IssueType = Literal[
    "unsupported_fact", "distorted_fact", "wrong_attribution", "unsupported_inference",
    "quote_error", "missed_high_value_content", "wrong_card_boundary", "wrong_scene",
    "duplicate_content", "todo_error", "search_boundary_error", "external_source_error",
    "weak_help", "structure_or_style", "wrong_subject",
    "wrong_communication_boundary", "low_independent_value", "thin_content",
    "dense_unstructured_body", "structure_content_mismatch", "non_actionable_help",
    "reporting_tone", "duplicate_heading_number", "source_registry_mismatch",
]


_TRACKING_QUERY_PARAMETERS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
})
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX = frozenset("0123456789abcdefABCDEF")
_REG_NAME_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    "!$&'()*+,;="
)


def _normalize_percent_component(component: str) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(component):
        value = component[index]
        if value != "%":
            normalized.append(value)
            index += 1
            continue
        if index + 2 >= len(component) or (
            component[index + 1] not in _HEX or component[index + 2] not in _HEX
        ):
            raise ValueError("source URL contains an invalid percent triplet")
        octet = int(component[index + 1:index + 3], 16)
        character = chr(octet)
        normalized.append(character if character in _UNRESERVED else f"%{octet:02X}")
        index += 3
    return "".join(normalized)


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    retained: list[str] = []
    for item in query.split("&"):
        raw_key = item.split("=", 1)[0]
        canonical_key = _normalize_percent_component(raw_key)
        if (
            canonical_key.lower().startswith("utm_")
            or canonical_key.lower() in _TRACKING_QUERY_PARAMETERS
        ):
            continue
        retained.append(_normalize_percent_component(item))
    return "&".join(retained)


def canonicalize_source_url(url: str) -> str:
    """Return the publication identity for an absolute HTTP(S) source URL."""
    if not isinstance(url, str) or any(
        character.isspace() or ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
        for character in url
    ):
        raise ValueError("source URL must be an absolute HTTP(S) URL without whitespace")
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError) as exc:
        raise ValueError("source URL must be an absolute HTTP(S) URL") from exc

    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if scheme not in {"http", "https"} or not host or "@" in parsed.netloc:
        raise ValueError("source URL must be an absolute HTTP(S) URL")

    if parsed.netloc.startswith("["):
        closing_bracket = parsed.netloc.find("]")
        literal = parsed.netloc[1:closing_bracket] if closing_bracket >= 0 else ""
        remainder = parsed.netloc[closing_bracket + 1:] if closing_bracket >= 0 else ""
        if remainder:
            port_text = remainder[1:]
            if (
                not remainder.startswith(":")
                or not port_text
                or not all("0" <= character <= "9" for character in port_text)
            ):
                raise ValueError("source URL IP-literal authority has an invalid port suffix")
            port = int(port_text)
            if not 0 <= port <= 65_535:
                raise ValueError("source URL port is outside the valid range")
        else:
            port = None
        try:
            normalized_host = f"[{IPv6Address(literal).compressed.lower()}]"
        except (AddressValueError, ValueError) as exc:
            raise ValueError("source URL IP-literal must be a standard IPv6 address") from exc
    else:
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("source URL has an invalid port") from exc
        if "%" in host or any(character not in _REG_NAME_CHARACTERS for character in host):
            raise ValueError("source URL host must be a valid reg-name")
        normalized_host = host.lower()
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        normalized_host = f"{normalized_host}:{port}"

    return urlunsplit((
        scheme,
        normalized_host,
        _normalize_percent_component(parsed.path) or "/",
        _canonical_query(parsed.query),
        "",
    ))


def stable_source_id(url: str) -> str:
    canonical_url = canonicalize_source_url(url)
    return "source_" + sha256(canonical_url.encode("utf-8")).hexdigest()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Beta8AuditIssue(_StrictModel):
    card_id: str | None
    suggested_scene_id: Beta8SceneId | None
    issue_type: IssueType
    severity: Literal["blocking", "important", "minor"]
    problem: str = Field(min_length=1, max_length=4_000)
    required_change: str = Field(min_length=1, max_length=4_000)
    affected_excerpt: str | None = Field(default=None, max_length=4_000)
    target_section: str | None = Field(default=None, min_length=1, max_length=500)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def validate_missing_card_fields(self) -> "Beta8AuditIssue":
        if self.card_id is None and self.issue_type != "missed_high_value_content":
            raise ValueError("only missed_high_value_content may omit card_id")
        if self.issue_type == "missed_high_value_content":
            if self.suggested_scene_id is None:
                raise ValueError("missed_high_value_content requires suggested_scene_id")
        elif self.suggested_scene_id is not None:
            raise ValueError("suggested_scene_id is only valid for missed_high_value_content")
        if self.card_id is None and self.target_section is not None:
            raise ValueError("target_section requires a card-scoped issue")
        if self.issue_type == "structure_content_mismatch" and self.target_section is None:
            raise ValueError("structure_content_mismatch requires target_section")
        return self


class Beta8RevisionTaskCheck(_StrictModel):
    requirement_id: str
    completed: bool
    reason: str = Field(min_length=1, max_length=4_000)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=2_000)


class Beta8AuditResult(_StrictModel):
    audit_phase: AuditPhase
    audit_scope: AuditScope
    audit_unit_id: str
    input_complete: bool
    input_error: str | None = Field(default=None, max_length=2_000)
    issues: list[Beta8AuditIssue] = Field(default_factory=list, max_length=1_000)
    revision_task_checks: list[Beta8RevisionTaskCheck] = Field(default_factory=list, max_length=1_000)
    passed: bool

    @model_validator(mode="after")
    def validate_passed(self) -> "Beta8AuditResult":
        expected = self.input_complete and not self.issues and all(
            check.completed for check in self.revision_task_checks
        )
        if self.passed != expected:
            raise ValueError("passed must match input completeness, issues, and revision checks")
        if self.input_complete and self.input_error is not None:
            raise ValueError("input_error must be null when input_complete is true")
        if not self.input_complete and not self.input_error:
            raise ValueError("input_error is required when input_complete is false")
        return self


class AggregatedAuditIssue(Beta8AuditIssue):
    audit_issue_id: str
    origin_audit_unit_ids: list[str] = Field(min_length=1)


class AggregatedAudit(_StrictModel):
    audit_phase: AuditPhase
    expected_audit_unit_ids: list[str]
    completed_audit_unit_ids: list[str]
    incomplete_audit_unit_ids: list[str]
    issues: list[AggregatedAuditIssue]


class AuditIssueDecision(_StrictModel):
    audit_issue_ids: list[str] = Field(min_length=1)
    decision: Literal["accepted", "combined", "rejected"]
    reason: str = Field(min_length=1, max_length=4_000)
    revision_task_key: str | None

    @model_validator(mode="after")
    def validate_decision(self) -> "AuditIssueDecision":
        if self.decision == "combined" and len(self.audit_issue_ids) < 2:
            raise ValueError("combined decision requires at least two audit issues")
        if self.decision == "rejected" and self.revision_task_key is not None:
            raise ValueError("rejected decision cannot have a revision task")
        if self.decision != "rejected" and self.revision_task_key is None:
            raise ValueError("accepted or combined decision requires a revision task")
        return self


class CardDecision(_StrictModel):
    decision_key: str
    operation: Literal["keep", "revise", "merge", "drop", "create"]
    source_card_ids: list[str]
    target_scene_id: Beta8SceneId
    reason: str = Field(min_length=1, max_length=4_000)
    revision_task_key: str | None

    @model_validator(mode="after")
    def validate_operation(self) -> "CardDecision":
        count = len(self.source_card_ids)
        if self.operation == "keep" and (count != 1 or self.revision_task_key is not None):
            raise ValueError("keep requires one source and no revision task")
        if self.operation == "revise" and count != 1:
            raise ValueError("revise requires one source card")
        if self.operation == "merge" and count < 2:
            raise ValueError("merge requires at least two source cards")
        if self.operation == "drop" and (count < 1 or self.revision_task_key is not None):
            raise ValueError("drop requires sources and no revision task")
        if self.operation == "create" and count:
            raise ValueError("create cannot have source cards")
        if self.operation not in {"keep", "drop"} and self.revision_task_key is None:
            raise ValueError("changed card decisions require one revision task")
        return self


class RevisionRequirement(_StrictModel):
    requirement_id: str
    instruction: str = Field(min_length=1, max_length=4_000)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=2_000)


class Beta8RevisionTask(_StrictModel):
    revision_task_key: str
    decision_key: str
    operation: Literal["revise", "merge", "create"]
    target_scene_id: Beta8SceneId
    source_card_ids: list[str]
    audit_issue_ids: list[str]
    requirements: list[RevisionRequirement] = Field(min_length=1)
    preserve_points: list[str]
    remove_or_avoid: list[str]
    completion_criteria: list[str] = Field(min_length=1)
    search_task_keys: list[str]


class Beta8SearchTask(_StrictModel):
    search_task_key: str
    source_candidate_ids: list[str]
    question: str = Field(min_length=1, max_length=4_000)
    purpose: str = Field(min_length=1, max_length=4_000)
    target_decision_keys: list[str] = Field(min_length=1)
    related_segment_ids: list[str]
    source_requirements: list[str]
    jurisdiction: str | None = Field(default=None, max_length=500)
    freshness_requirement: str | None = Field(default=None, max_length=1_000)


class DroppedSearchCandidates(_StrictModel):
    source_candidate_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_000)


class GlobalTodoCandidate(_StrictModel):
    source_todo_candidate_ids: list[str] = Field(min_length=1)
    text: str = Field(min_length=1, max_length=2_000)
    owner_type: Literal["user", "other_requires_user_follow_up"]
    assignee_text: str | None = Field(default=None, max_length=500)
    due_at: str | None = Field(default=None, max_length=100)
    due_text: str | None = Field(default=None, max_length=500)
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=2_000)


class DroppedTodoCandidates(_StrictModel):
    source_todo_candidate_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_000)


class UnresolvedConflict(_StrictModel):
    conflict_type: str = Field(min_length=1, max_length=500)
    related_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=4_000)
    evidence_segment_ids: list[str]


class Beta8OrchestrationResult(_StrictModel):
    input_complete: bool
    input_error: str | None
    audit_issue_decisions: list[AuditIssueDecision]
    card_decisions: list[CardDecision]
    revision_tasks: list[Beta8RevisionTask]
    search_tasks: list[Beta8SearchTask]
    dropped_search_candidates: list[DroppedSearchCandidates]
    todo_candidates: list[GlobalTodoCandidate]
    dropped_todo_candidates: list[DroppedTodoCandidates]
    unresolved_conflicts: list[UnresolvedConflict]

    @model_validator(mode="after")
    def validate_internal_references(self) -> "Beta8OrchestrationResult":
        if self.input_complete and self.input_error is not None:
            raise ValueError("input_error must be null for complete orchestration")
        task_by_key = {task.revision_task_key: task for task in self.revision_tasks}
        if len(task_by_key) != len(self.revision_tasks):
            raise ValueError("revision_task_key values must be unique")
        decision_by_key = {decision.decision_key: decision for decision in self.card_decisions}
        if len(decision_by_key) != len(self.card_decisions):
            raise ValueError("decision_key values must be unique")
        for decision in self.card_decisions:
            if decision.revision_task_key:
                task = task_by_key.get(decision.revision_task_key)
                if task is None or task.decision_key != decision.decision_key:
                    raise ValueError("card decision must reference its matching revision task")
        search_keys = {task.search_task_key for task in self.search_tasks}
        for task in self.revision_tasks:
            if set(task.search_task_keys) - search_keys:
                raise ValueError("revision task references unknown search task")
        return self


class SearchSupportedPoint(_StrictModel):
    point: str = Field(min_length=1, max_length=4_000)
    source_ids: list[str] = Field(min_length=1)


class ExternalSource(_StrictModel):
    source_id: str
    title: str = Field(min_length=1, max_length=2_000)
    url: str = Field(min_length=1, max_length=8_000)
    publisher: str | None = Field(default=None, max_length=1_000)
    published_at: str | None = Field(default=None, max_length=100)
    retrieved_at: str
    source_type: str = Field(min_length=1, max_length=500)
    supports: list[int]


class Beta8SearchPacket(_StrictModel):
    search_task_id: str
    status: Literal["success", "partial", "failed"]
    answer: str | None = Field(default=None, max_length=20_000)
    supported_points: list[SearchSupportedPoint]
    unresolved_points: list[str]
    sources: list[ExternalSource]
    error_summary: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def validate_status(self) -> "Beta8SearchPacket":
        if self.status == "failed":
            if self.answer is not None or self.supported_points or self.sources:
                raise ValueError("failed search packet has no answer, supported points, or sources")
            if not self.error_summary:
                raise ValueError("failed search packet requires error_summary")
        elif not self.sources:
            raise ValueError("successful or partial search packet requires sources")
        source_ids = {source.source_id for source in self.sources}
        for index, point in enumerate(self.supported_points):
            if set(point.source_ids) - source_ids:
                raise ValueError(f"supported point {index} references unknown sources")
        for source in self.sources:
            if any(index < 0 or index >= len(self.supported_points) for index in source.supports):
                raise ValueError("source supports index is out of range")
        return self


def _source_identity(source: ExternalSource) -> tuple[str, str, str, str | None, str | None, str]:
    return (
        source.source_id,
        source.url,
        source.title,
        source.publisher,
        source.published_at,
        source.source_type,
    )


def normalize_search_packet(packet: Beta8SearchPacket) -> Beta8SearchPacket:
    """Migrate a checkpoint/provider packet onto stable source identities or reject it."""
    remapped_ids: dict[str, str] = {}
    sources_by_id: dict[str, ExternalSource] = {}
    for source in packet.sources:
        canonical_url = canonicalize_source_url(source.url)
        canonical_id = stable_source_id(canonical_url)
        prior = remapped_ids.setdefault(source.source_id, canonical_id)
        if prior != canonical_id:
            raise ValueError("one packet-local source ID maps to multiple canonical URLs")
        normalized = source.model_copy(update={
            "source_id": canonical_id,
            "url": canonical_url,
        })
        existing = sources_by_id.get(canonical_id)
        if existing is None:
            sources_by_id[canonical_id] = normalized
        elif _source_identity(existing) != _source_identity(normalized):
            raise ValueError("conflicting source registry metadata in one search packet")
        else:
            sources_by_id[canonical_id] = existing.model_copy(update={
                "supports": sorted(set(existing.supports) | set(normalized.supports)),
                "retrieved_at": min(existing.retrieved_at, normalized.retrieved_at),
            })
    supported_points = [
        point.model_copy(update={
            "source_ids": [remapped_ids[source_id] for source_id in point.source_ids],
        })
        for point in packet.supported_points
    ]
    normalized_packet = Beta8SearchPacket.model_validate({
        **packet.model_dump(mode="json"),
        "supported_points": [point.model_dump(mode="json") for point in supported_points],
        "sources": [
            source.model_dump(mode="json")
            for _, source in sorted(sources_by_id.items())
        ],
    })
    return packet if normalized_packet == packet else normalized_packet


def build_strict_source_registry(
    packets: list[Beta8SearchPacket] | tuple[Beta8SearchPacket, ...],
) -> tuple[ExternalSource, ...]:
    """Return one deterministic global entry per source, rejecting metadata conflicts."""
    registry: dict[str, ExternalSource] = {}
    for packet in packets:
        normalized = normalize_search_packet(packet)
        for source in normalized.sources:
            publication_source = source.model_copy(update={"supports": []})
            existing = registry.get(publication_source.source_id)
            if existing is None:
                registry[publication_source.source_id] = publication_source
            elif _source_identity(existing) != _source_identity(publication_source):
                raise ValueError(
                    "conflicting source registry metadata for source ID: "
                    + publication_source.source_id
                )
            else:
                registry[publication_source.source_id] = existing.model_copy(update={
                    "retrieved_at": min(existing.retrieved_at, publication_source.retrieved_at),
                })
    return tuple(registry[key] for key in sorted(registry))


class Beta8RevisedCard(_StrictModel):
    input_complete: bool
    input_error: str | None
    reserved_card_id: str
    operation: Literal["revise", "merge", "create"]
    markdown: str = Field(min_length=1, max_length=80_000)
    source_segment_ids: list[str]
    used_source_ids: list[str]
    todo_candidates: list[Beta8TodoCandidate]
    completed_requirement_ids: list[str]
    unresolved_requirement_ids: list[str]

    def validate_requirement_ids(self, expected_requirement_ids: set[str]) -> None:
        completed = Counter(self.completed_requirement_ids)
        unresolved = Counter(self.unresolved_requirement_ids)
        actual = set(completed) | set(unresolved)
        duplicates = sorted(
            key for key in actual if completed[key] + unresolved[key] != 1
        )
        missing_or_unknown = sorted(actual ^ expected_requirement_ids)
        if duplicates or missing_or_unknown:
            offending = sorted(set(duplicates + missing_or_unknown))
            raise ValueError(f"Revision requirement IDs are not an exact partition: {', '.join(offending)}")
        if self.input_complete != (not self.unresolved_requirement_ids):
            raise ValueError("input_complete must be false when requirements are unresolved")


class Beta8FinalCard(_StrictModel):
    card_id: str
    scene_id: Beta8SceneId
    position: int = Field(ge=0)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    markdown: str = Field(min_length=1, max_length=80_000)
    source_segment_ids: list[str]
    used_source_ids: list[str]
    origin_card_ids: list[str]
    v1_markdown_sha256: str | None
    final_markdown_sha256: str
    untouched: bool
    work_communication_unit_ids: list[str] = Field(max_length=1)


class Beta8PublicationBundle(_StrictModel):
    cards: list[Beta8FinalCard]
    todo_candidates: list[GlobalTodoCandidate]
    external_sources: list[ExternalSource]
    v1_card_hashes: dict[str, str]
    final_card_hashes: dict[str, str]
    untouched_card_ids: list[str]
    completed_revision_task_ids: list[str]
    search_degraded: bool
    search_degraded_reason: str | None
    expected_work_communication_unit_ids: list[str]


def _validate_exact_once(*, expected: set[str], actual: list[str], label: str) -> None:
    counts = Counter(actual)
    offending = sorted(
        (set(counts) ^ expected) | {item for item, count in counts.items() if count != 1}
    )
    if offending:
        raise ValueError(f"Invalid {label}: {', '.join(offending)}")


def validate_orchestration_against_inputs(
    result: Beta8OrchestrationResult,
    *,
    card_ids: set[str],
    audit_issue_ids: set[str],
    search_candidate_ids: set[str],
    todo_candidate_ids: set[str] | None = None,
    missed_value_issue_ids: set[str] | None = None,
    work_unit_ids_by_card_id: dict[str, tuple[str, ...]] | None = None,
) -> None:
    _validate_exact_once(
        expected=card_ids,
        actual=[item for decision in result.card_decisions for item in decision.source_card_ids],
        label="card IDs",
    )
    _validate_exact_once(
        expected=audit_issue_ids,
        actual=[item for decision in result.audit_issue_decisions for item in decision.audit_issue_ids],
        label="audit issue IDs",
    )
    _validate_exact_once(
        expected=search_candidate_ids,
        actual=(
            [item for task in result.search_tasks for item in task.source_candidate_ids]
            + [item for dropped in result.dropped_search_candidates for item in dropped.source_candidate_ids]
        ),
        label="search candidate IDs",
    )
    if todo_candidate_ids is not None:
        _validate_exact_once(
            expected=todo_candidate_ids,
            actual=(
                [item for todo in result.todo_candidates for item in todo.source_todo_candidate_ids]
                + [item for dropped in result.dropped_todo_candidates for item in dropped.source_todo_candidate_ids]
            ),
            label="todo candidate IDs",
        )
    create_tasks = [task for task in result.revision_tasks if task.operation == "create"]
    allowed = missed_value_issue_ids or set()
    if any(not (set(task.audit_issue_ids) & allowed) for task in create_tasks):
        raise ValueError("create requires a missed_high_value_content audit issue")
    work_mapping = work_unit_ids_by_card_id or {}
    for decision in result.card_decisions:
        work_unit_ids = {
            unit_id
            for card_id in decision.source_card_ids
            for unit_id in work_mapping.get(card_id, ())
        }
        if not work_unit_ids:
            continue
        if decision.operation == "drop":
            raise ValueError("work communication card cannot be dropped")
        if len(work_unit_ids) > 1:
            raise ValueError("distinct work communication units cannot be merged")
        if decision.target_scene_id != "work_communication":
            raise ValueError("work communication card must remain in work_communication scene")


def validate_publication_bundle(bundle: Beta8PublicationBundle) -> None:
    card_ids = [card.card_id for card in bundle.cards]
    if len(set(card_ids)) != len(card_ids):
        raise ValueError("publication card IDs must be unique")
    expected_positions = list(range(len(bundle.cards)))
    if [card.position for card in bundle.cards] != expected_positions:
        raise ValueError("publication card positions must be consecutive")
    source_ids = [source.source_id for source in bundle.external_sources]
    duplicate_source_ids = sorted(
        source_id for source_id, count in Counter(source_ids).items() if count > 1
    )
    if duplicate_source_ids:
        raise ValueError("publication has duplicate source IDs: " + ", ".join(duplicate_source_ids))
    for source in bundle.external_sources:
        try:
            canonical_url = canonicalize_source_url(source.url)
        except ValueError as exc:
            raise ValueError(f"publication source URL is not canonicalizable: {source.url}") from exc
        if source.url != canonical_url:
            raise ValueError(f"publication source URL must be canonical: {source.url}")
        expected_source_id = stable_source_id(canonical_url)
        if source.source_id != expected_source_id:
            raise ValueError(
                "publication source_id does not match canonical URL: "
                f"{source.source_id}"
            )
    source_id_set = set(source_ids)
    for card in bundle.cards:
        unknown_sources = sorted(set(card.used_source_ids) - source_id_set)
        if unknown_sources:
            raise ValueError(f"card references unknown source IDs: {', '.join(unknown_sources)}")
        if card.final_markdown_sha256 != bundle.final_card_hashes.get(card.card_id):
            raise ValueError(f"final hash mismatch for card {card.card_id}")
        if card.untouched:
            if (
                card.card_id not in bundle.untouched_card_ids
                or card.v1_markdown_sha256 != card.final_markdown_sha256
                or bundle.v1_card_hashes.get(card.card_id) != bundle.final_card_hashes.get(card.card_id)
                or len(card.origin_card_ids) != 1
            ):
                raise ValueError(f"untouched card hash changed: {card.card_id}")
        if card.work_communication_unit_ids and card.scene_id != "work_communication":
            raise ValueError(
                "work communication mapping must remain in work_communication scene: "
                + card.card_id
            )
    _validate_exact_once(
        expected=set(bundle.expected_work_communication_unit_ids),
        actual=list(bundle.expected_work_communication_unit_ids),
        label="expected work communication unit IDs",
    )
    _validate_exact_once(
        expected=set(bundle.expected_work_communication_unit_ids),
        actual=[
            unit_id
            for card in bundle.cards
            for unit_id in card.work_communication_unit_ids
        ],
        label="work communication unit closure",
    )
