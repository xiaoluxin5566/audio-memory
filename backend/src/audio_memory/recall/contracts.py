from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .semantics import validity


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("invalid_timezone") from error


class Segment(Contract):
    uid: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    text: str
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    speaker_local_id: str | None = None
    text_locator: dict[str, Any] = Field(default_factory=dict)
    gap_reason: str | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_ms is not None and (
            self.start_ms is None or self.end_ms < self.start_ms
        ):
            raise ValueError("invalid_segment_range")
        if not self.text and not self.gap_reason:
            raise ValueError("empty_segment_requires_gap_reason")
        return self


class SourceInput(Contract):
    source_id: str = Field(min_length=1)
    source_kind: Literal["audio", "markdown", "feishu"]
    file_id: str | None = None
    import_id: str | None = None
    origin_locator: str
    recorded_date: date | None = None
    date_confirmed: bool = False
    date_source: str = "unknown"
    recorded_start_at: datetime | None = None
    time_precision: Literal["day", "second", "minute"] = "day"
    timezone: str = "Asia/Shanghai"
    segments: list[Segment] = Field(default_factory=list)
    parser_version: str = "1"
    pipeline_version: str = "1"
    raw_artifact_path: str | None = None

    @model_validator(mode="after")
    def valid_source(self):
        validate_timezone(self.timezone)
        if len({s.uid for s in self.segments}) != len(self.segments):
            raise ValueError("duplicate_segment_uid")
        if [s.ordinal for s in self.segments] != sorted(
            {s.ordinal for s in self.segments}
        ):
            raise ValueError("invalid_segment_ordinals")
        if self.time_precision == "day" and self.recorded_start_at is not None:
            raise ValueError("day_precision_has_no_start_time")
        if self.time_precision != "day" and self.recorded_start_at is None:
            raise ValueError("precise_time_required")
        if self.recorded_start_at is not None and (
            self.recorded_start_at.tzinfo is None
            or self.recorded_date
            != self.recorded_start_at.astimezone(ZoneInfo(self.timezone)).date()
        ):
            raise ValueError("recording_time_date_mismatch")
        return self


class SourceSnapshot(Contract):
    source_revision_id: str
    source_id: str
    source_kind: Literal["audio", "markdown", "feishu"]
    recorded_date: date
    recorded_start_at: datetime | None = None
    time_precision: str
    timezone: str
    segments: list[Segment]


class SourceRange(Contract):
    source_revision_id: str
    start_segment_uid: str
    end_segment_uid: str


class SourceFragment(Contract):
    kind: Literal["other"] = "other"

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_legacy_kind(cls, value):
        return "other" if value in {"transition", "pending"} else value

    reason: str = Field(min_length=1)
    source_ranges: list[SourceRange] = Field(min_length=1)


class EventDraft(Contract):
    event_id: str
    local_key: str
    title: str
    topics: list[str] = Field(default_factory=list)
    kind: str = "conversation"
    scene: Literal["live", "content_consumption", "unknown"] = "unknown"
    recording_kind: Literal["recording", "reported", "other_fragment"] = "recording"
    source_ranges: list[SourceRange]
    statement_date: date
    time_precision: str = "day"
    continuity_evidence: list[SourceRange] = Field(default_factory=list)
    reporting_event_ids: list[str] = Field(default_factory=list)
    boundary_reason: str | None = None


class EvidenceRef(SourceRange):
    quote: str | None = None
    recorded_date: str | None = None
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=0)


class ExtractedFact(Contract):
    statement_anchor: EvidenceRef | None = None
    local_subject: str
    local_property: str
    typed_value: Any
    unit: str | None = None
    local_object_entities: list[str] = Field(default_factory=list)
    raw_expression: str
    source_note: str | None = Field(default=None, description="来源说明：表达者、观点/转述性质及原文中的保留意见；不替代证据或值中的必要条件。")
    raw_time_expression: str | None = None
    derivation_type: Literal["explicit", "inferred"]
    confidence: float = Field(ge=0, le=1)
    valid_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    valid_precision: str = "unknown"
    evidence: list[EvidenceRef] = Field(min_length=1)


    @model_validator(mode="after")
    def validate_validity(self):
        precision = validity(self.valid_at, self.valid_from, self.valid_to, self.valid_precision)
        object.__setattr__(self, "valid_precision", precision)
        return self


class ResolvedExtraction(Contract):
    event_id: str
    fact_extraction_state: Literal["complete", "empty", "failed"]
    semantic_review_status: Literal['passed','pending'] | None = None
    entities: list[dict] = Field(default_factory=list)
    properties: list[dict] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    occurrences: list[dict] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)
    tasks: list[dict] = Field(default_factory=list)
    unresolved: list[dict] = Field(default_factory=list)
    resolutions: list[dict] = Field(default_factory=list)


class EventContext(Contract):
    event_id: str
    entries: list[dict] = Field(default_factory=list)


class CandidateHit(Contract):
    id: str
    text: str
    score: float
    source: str
    version: str


class CandidateIndex(Protocol):
    async def lookup(
        self, query: str, kind: str, top_k: int, scope: Any
    ) -> list[CandidateHit]: ...


class QueryScope(Contract):
    reference_datetime: datetime
    user_timezone: str = "Asia/Shanghai"
    intent: Literal["single_event", "overview", "all", "change"] = "single_event"
    time_target: Literal["statement", "valid", "both", "unknown"] = "unknown"
    statement_range: tuple[date, date] | None = None
    valid_range: tuple[date, date] | None = None
    as_of_statement_time: datetime | None = None
    constraints: dict = Field(default_factory=dict)
    clues: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_scope(self):
        validate_timezone(self.user_timezone)
        for stamp in [self.reference_datetime, self.as_of_statement_time]:
            if stamp is not None and stamp.utcoffset() is None:
                raise ValueError("query_clock_timezone_required")
        for window in [self.statement_range, self.valid_range]:
            if window and window[0] > window[1]:
                raise ValueError("invalid_query_range")
        return self


class QueryContext(Contract):
    session_id: str
    turn_id: str
    scope: QueryScope
    snapshot_id: str
    tool_count: int = 0
    budget: int = 10
    schema_version: str = "1"


class ToolResult(Contract):
    status: Literal["ok", "zero_hits", "processing", "error"]
    snapshot_id: str
    data: Any = None
    has_more: bool = False
    cursor: str | None = None
    coverage: dict = Field(default_factory=dict)
    error_code: str | None = None


class ReadReceipt(Contract):
    segment_ordinals: dict[str, int] = Field(default_factory=dict)
    segment_lengths: dict[str, int] = Field(default_factory=dict)
    event_version_id: str
    source_revision_ids: list[str]
    core_ranges: list[SourceRange]
    core_segment_uids: list[str]
    gap_ranges: list[dict] = Field(default_factory=list)
    input_hash: str
    question_key: str
    complete: bool
    usage: list[dict] = Field(default_factory=list)


class EventReadResult(Contract):
    recording_kind: str = "recording"
    source_metadata: list[dict] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)
    related_event_version_ids: list[str] = Field(default_factory=list)
    entities: list[dict] = Field(default_factory=list)
    facts: list[dict] = Field(default_factory=list)
    receipt: ReadReceipt
    required_recording_event_ids: list[str] = Field(default_factory=list)
    content: str
    evidence_refs: list[EvidenceRef]
    usage: list[dict] = Field(default_factory=list)


class RecallAnswer(Contract):
    timings: dict = Field(default_factory=dict)
    answer_status: Literal[
        "complete", "partial", "insufficient_evidence", "needs_clarification"
    ]
    text: str
    selected_event_ids: list[str] = Field(default_factory=list)
    citations: list[EvidenceRef] = Field(default_factory=list)
    scope: QueryScope
    read_receipts: list[ReadReceipt] = Field(default_factory=list)
    tool_count: int
    usage: list[dict] = Field(default_factory=list)
    error_code: str | None = None
