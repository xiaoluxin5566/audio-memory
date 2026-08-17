from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from audio_memory.prompts.direct_report_schema import (
    RESERVED_SECTION_TITLES,
    BulletListBlock,
    NumberedListBlock,
    ParagraphBlock,
    ReportOverview,
    TableBlock,
)
from audio_memory.prompts.report_schema import (
    ReportTodo,
    StrictReportModel,
    validate_report_text,
    validate_unique_ids,
)


class SubheadingBlock(StrictReportModel):
    type: Literal["subheading"]
    title: str = Field(min_length=1, max_length=160)


class QuoteBlock(StrictReportModel):
    type: Literal["quote"]
    text: str = Field(min_length=1, max_length=2_000)
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_quote(self):
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        return self


MarkedReportBlock = Annotated[
    ParagraphBlock
    | SubheadingBlock
    | QuoteBlock
    | BulletListBlock
    | NumberedListBlock
    | TableBlock,
    Field(discriminator="type"),
]


class MarkedReportSection(StrictReportModel):
    title: str = Field(min_length=1, max_length=160)
    blocks: list[MarkedReportBlock] = Field(min_length=1, max_length=80)


class DirectReportMarkedDocument(StrictReportModel):
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=240)
    overview: ReportOverview
    sections: list[MarkedReportSection] = Field(min_length=1, max_length=16)
    todos: list[ReportTodo] = Field(default_factory=list, max_length=30)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=500)
    external_source_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_document(self):
        titles = [section.title.strip() for section in self.sections]
        titles.extend(
            block.title.strip()
            for section in self.sections
            for block in section.blocks
            if isinstance(block, SubheadingBlock)
        )
        if any(title in RESERVED_SECTION_TITLES for title in titles):
            raise ValueError("reserved report section title")
        if len(titles) != len(set(titles)):
            raise ValueError("section and subheading titles must be unique")
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        validate_unique_ids(self.external_source_ids, field_name="external_source_ids")
        validate_report_text(self.title)
        validate_report_text(self.overview.summary)
        return self
