from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


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

    stage: ReportPipelineStage
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class PipelineMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local_duration_ms: int = Field(default=0, ge=0)
    model_duration_ms: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)
    web_search_performed: bool = False
    web_search_degraded_reason: str | None = None
    model_calls: tuple[ModelCallMetric, ...] = ()

    def with_model_call(
        self,
        *,
        stage: ReportPipelineStage,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
    ) -> "PipelineMetrics":
        call = ModelCallMetric(
            stage=stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )
        return self.model_copy(
            update={
                "model_duration_ms": self.model_duration_ms + duration_ms,
                "input_tokens": self.input_tokens + input_tokens,
                "output_tokens": self.output_tokens + output_tokens,
                "model_call_count": self.model_call_count + 1,
                "model_calls": (*self.model_calls, call),
            }
        )
