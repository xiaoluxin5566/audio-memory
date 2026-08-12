from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidencedModel(StrictModel):
    evidence_segment_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_evidence(self):
        if len(self.evidence_segment_ids) != len(set(self.evidence_segment_ids)):
            raise ValueError("evidence_segment_ids must be unique")
        return self


class AutonomousSection(EvidencedModel):
    type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(default="", max_length=16_000)
    items: list[str] = Field(default_factory=list)


class AutonomousQuote(EvidencedModel):
    quote: str = Field(min_length=1, max_length=1_200)
    context: str = Field(default="", max_length=4_000)
    analysis: str = Field(min_length=1, max_length=8_000)


class AutonomousRecommendation(EvidencedModel):
    title: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=8_000)
    actions: list[str] = Field(default_factory=list)
    suggested_language: str | None = Field(default=None, max_length=4_000)
    success_signal: str | None = Field(default=None, max_length=4_000)
    caveat: str | None = Field(default=None, max_length=4_000)


class AutonomousCard(EvidencedModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=4_000)
    external_source_ids: list[str] = Field(default_factory=list)
    content: list[AutonomousSection] = Field(
        min_length=2,
        json_schema_extra={
            "prefixItems": [
                {
                    "properties": {
                        "type": {"const": "scene_reconstruction"}
                    }
                }
            ],
            "contains": {"properties": {"type": {"const": "analysis"}}},
            "minContains": 1,
        },
    )
    quotes: list[AutonomousQuote] = Field(default_factory=list)
    recommendations: list[AutonomousRecommendation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_three_stage_structure(self):
        if not self.content or self.content[0].type != "scene_reconstruction":
            raise ValueError("first content section must be scene_reconstruction")
        if not any(section.type == "analysis" for section in self.content[1:]):
            raise ValueError("card requires an analysis section after reconstruction")
        if len(self.external_source_ids) != len(set(self.external_source_ids)):
            raise ValueError("external_source_ids must be unique")
        return self


class AutonomousAnalysisResult(StrictModel):
    cards: list[AutonomousCard] = Field(default_factory=list)


class InformationNote(EvidencedModel):
    topic: str = Field(min_length=1, max_length=240)
    details: list[str] = Field(min_length=1)


class InformationNotebook(StrictModel):
    window_id: str = Field(min_length=1, max_length=80)
    notes: list[InformationNote] = Field(default_factory=list)


class AutonomousCardPlan(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    analysis_task: str = Field(min_length=1, max_length=2_000)
    required_segment_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_segments(self):
        if len(self.required_segment_ids) != len(set(self.required_segment_ids)):
            raise ValueError("required_segment_ids must be unique")
        return self


class AutonomousRetrievalPlan(StrictModel):
    cards: list[AutonomousCardPlan] = Field(min_length=1)
