from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from audio_memory.prompts.direct_report_schema import RESERVED_SECTION_TITLES, ReportOverview
from audio_memory.prompts.report_schema import (
    ReportTodo,
    StrictReportModel,
    validate_report_text,
    validate_unique_ids,
)


class LightReportSection(StrictReportModel):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=30_000)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_section(self):
        if self.title.strip() in RESERVED_SECTION_TITLES:
            raise ValueError("reserved report section title")
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        validate_report_text(self.title)
        validate_report_text(self.content)
        return self


class DirectReportLightDocument(StrictReportModel):
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=240)
    overview: ReportOverview
    sections: list[LightReportSection] = Field(min_length=1, max_length=12)
    todos: list[ReportTodo] = Field(default_factory=list, max_length=30)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=500)
    external_source_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_document(self):
        titles = [section.title.strip() for section in self.sections]
        if len(titles) != len(set(titles)):
            raise ValueError("section titles must be unique")
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        validate_unique_ids(self.external_source_ids, field_name="external_source_ids")
        validate_report_text(self.title)
        validate_report_text(self.overview.summary)
        return self
