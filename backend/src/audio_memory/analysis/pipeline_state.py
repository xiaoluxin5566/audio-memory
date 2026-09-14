from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ReportPipelineStage(StrEnum):
    DIAGNOSTICS = "diagnostics"
    DEDUPLICATION = "deduplication"
    COVERAGE = "coverage"
    DISCOVERY = "discovery"
    ROUGH_SCENES = "rough_scenes"
    INVESTIGATION = "investigation"
    EVIDENCE_LEDGER = "evidence_ledger"
    FINAL_SCENES = "final_scenes"
    SCENE_TRANSCRIPTS = "scene_transcripts"
    DEEP_ANALYSIS = "deep_analysis"
    SEARCH = "search"
    PREPARE = "prepare"
    WRITING_BRIEF = "writing_brief"
    WRITER_SESSION = "writer_session"
    DRAFT_VERSIONS = "draft_versions"
    EVIDENCE_AUDITS = "evidence_audits"
    CONTENT_AUDITS = "content_audits"
    EVIDENCE_REQUESTS = "evidence_requests"
    REVISIONS = "revisions"
    FINAL_REVIEW = "final_review"


PIPELINE_STAGE_ORDER = tuple(ReportPipelineStage)


def next_stage(
    current: ReportPipelineStage, requested: ReportPipelineStage
) -> ReportPipelineStage:
    current_index = PIPELINE_STAGE_ORDER.index(current)
    requested_index = PIPELINE_STAGE_ORDER.index(requested)
    if requested_index != current_index + 1:
        raise ValueError(f"cannot skip from {current.value} to {requested.value}")
    return requested


class ReportPipelineCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: ReportPipelineStage
    sequence: int = Field(ge=0)
    parameter_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return (
            self.stage.value
            if self.sequence == 0
            else f"{self.stage.value}:{self.sequence}"
        )


def assert_resume_compatible(
    checkpoint: ReportPipelineCheckpoint,
    *,
    parameter_fingerprint: str,
    current_stage: ReportPipelineStage | None = None,
) -> None:
    if checkpoint.parameter_fingerprint != parameter_fingerprint:
        raise ValueError("parameter fingerprint changed; checkpoint is not reusable")
    if current_stage is not None and PIPELINE_STAGE_ORDER.index(
        checkpoint.stage
    ) > PIPELINE_STAGE_ORDER.index(current_stage):
        raise ValueError("checkpoint belongs to a future stage")


class ModelCallMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = "legacy"
    invocation_id: str = "legacy"
    attempt_index: int = Field(default=0, ge=0)
    stage: str
    attempt_kind: Literal[
        "normal", "index_repair", "schema_repair", "retry", "resume"
    ] = "normal"
    provider: str = "unknown"
    model: str = "unknown"
    started_at: str | None = None
    finished_at: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_usage_unavailable_reason: str | None = None
    duration_ms: int = Field(ge=0)
    cost: Decimal | None = Field(default=None, ge=0)
    cost_unavailable_reason: str | None = "legacy metric did not record cost"
    checkpoint_reused: bool = False

    @model_validator(mode="after")
    def validate_cost_availability(self) -> "ModelCallMetric":
        if self.cost is None and not self.cost_unavailable_reason:
            raise ValueError("cost_unavailable_reason is required when cost is null")
        if self.cost is not None and self.cost_unavailable_reason is not None:
            raise ValueError("cost_unavailable_reason must be null when cost is known")
        if (
            self.input_tokens is None or self.output_tokens is None
        ) and not self.token_usage_unavailable_reason:
            raise ValueError(
                "token_usage_unavailable_reason is required when token usage is null"
            )
        return self


class PipelineMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local_duration_ms: int = Field(default=0, ge=0)
    model_duration_ms: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    search_input_tokens: int | None = Field(default=0, ge=0)
    search_output_tokens: int | None = Field(default=0, ge=0)
    search_token_usage_unavailable_reason: str | None = None
    search_model_response_count: int = Field(default=0, ge=0)
    web_search_tool_call_count: int = Field(default=0, ge=0)
    web_search_performed: bool = False
    web_search_degraded_reason: str | None = None
    run_id: str | None = None
    run_started_at: str | None = None
    run_finished_at: str | None = None
    run_duration_ms: int = Field(default=0, ge=0)
    run_model_duration_ms: int = Field(default=0, ge=0)
    run_input_tokens: int | None = Field(default=0, ge=0)
    run_output_tokens: int | None = Field(default=0, ge=0)
    token_usage_unavailable_reason: str | None = None
    new_model_call_count: int = Field(default=0, ge=0)
    historical_model_call_count: int = Field(default=0, ge=0)
    run_search_input_tokens: int | None = Field(default=0, ge=0)
    run_search_output_tokens: int | None = Field(default=0, ge=0)
    run_search_model_response_count: int = Field(default=0, ge=0)
    run_web_search_tool_call_count: int = Field(default=0, ge=0)
    stage_durations_ms: dict[str, int] = Field(default_factory=dict)
    checkpoint_reused_stages: tuple[str, ...] = ()
    final_audit_score: int | None = Field(default=None, ge=0, le=100)
    cost: Decimal | None = Field(default=None, ge=0)
    cost_unavailable_reason: str | None = None
    model_calls: tuple[ModelCallMetric, ...] = ()

    def with_model_call(
        self,
        *,
        stage: ReportPipelineStage,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        run_id: str = "legacy",
        attempt_kind: Literal[
            "normal", "index_repair", "schema_repair", "retry", "resume"
        ] = "normal",
        provider: str = "unknown",
        model: str = "unknown",
        cost: Decimal | None = None,
        cost_unavailable_reason: str | None = "legacy metric did not record cost",
        checkpoint_reused: bool = False,
    ) -> "PipelineMetrics":
        call = ModelCallMetric(
            run_id=run_id,
            stage=stage,
            attempt_kind=attempt_kind,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            cost=cost,
            cost_unavailable_reason=cost_unavailable_reason,
            checkpoint_reused=checkpoint_reused,
        )
        return self.model_copy(
            update={
                "model_duration_ms": self.model_duration_ms + duration_ms,
                "input_tokens": (
                    None
                    if self.input_tokens is None
                    else self.input_tokens + input_tokens
                ),
                "output_tokens": (
                    None
                    if self.output_tokens is None
                    else self.output_tokens + output_tokens
                ),
                "model_call_count": self.model_call_count + 1,
                "model_calls": (*self.model_calls, call),
            }
        )
