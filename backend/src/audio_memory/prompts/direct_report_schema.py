from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from audio_memory.prompts.report_schema import (
    ReportTodo,
    StrictReportModel,
    validate_report_text,
    validate_unique_ids,
)


RESERVED_SECTION_TITLES = frozenset(
    {"核心结论", "完整报告", "今天发生了什么", "今天发生了什么，重点改进什么"}
)


class ParagraphBlock(StrictReportModel):
    type: Literal["paragraph"]
    text: str = Field(min_length=1, max_length=8_000)


class SourceQuoteBlock(StrictReportModel):
    type: Literal["source_quote"]
    text: str = Field(min_length=1, max_length=2_000)
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_quote(self):
        stripped = self.text.strip()
        wrappers = (("“", "”"), ('"', '"'), ("'", "'"))
        if any(stripped.startswith(left) and stripped.endswith(right) for left, right in wrappers):
            raise ValueError("source_quote must omit wrapping quotation marks")
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        return self


class SuggestedWordingBlock(StrictReportModel):
    type: Literal["suggested_wording"]
    text: str = Field(min_length=1, max_length=3_000)


class BulletListBlock(StrictReportModel):
    type: Literal["bullet_list"]
    items: list[str] = Field(min_length=1, max_length=20)


class NumberedListBlock(StrictReportModel):
    type: Literal["numbered_list"]
    items: list[str] = Field(min_length=1, max_length=20)


class TableBlock(StrictReportModel):
    type: Literal["table"]
    columns: list[str] = Field(min_length=2, max_length=6)
    rows: list[list[str]] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_row_widths(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("table row width must match columns")
        return self


LeafReportBlock = Annotated[
    ParagraphBlock
    | SourceQuoteBlock
    | SuggestedWordingBlock
    | BulletListBlock
    | NumberedListBlock
    | TableBlock,
    Field(discriminator="type"),
]


class ReportSubsection(StrictReportModel):
    type: Literal["subsection"]
    title: str = Field(min_length=1, max_length=160)
    blocks: list[LeafReportBlock] = Field(min_length=1, max_length=30)


ReportBlock = Annotated[
    ParagraphBlock
    | SourceQuoteBlock
    | SuggestedWordingBlock
    | BulletListBlock
    | NumberedListBlock
    | TableBlock
    | ReportSubsection,
    Field(discriminator="type"),
]


class OverviewRow(StrictReportModel):
    phase: str = Field(min_length=1, max_length=80)
    event: str = Field(min_length=1, max_length=1_000)
    improvement: str = Field(min_length=1, max_length=1_000)
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_evidence(self):
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        return self


class ReportOverview(StrictReportModel):
    summary: str = Field(min_length=1, max_length=4_000)
    rows: list[OverviewRow] = Field(min_length=1, max_length=6)


class ReportSection(StrictReportModel):
    title: str = Field(min_length=1, max_length=160)
    blocks: list[ReportBlock] = Field(min_length=1, max_length=40)


class DirectReportDocument(StrictReportModel):
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=240)
    overview: ReportOverview
    sections: list[ReportSection] = Field(min_length=1, max_length=12)
    todos: list[ReportTodo] = Field(default_factory=list, max_length=30)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=500)
    external_source_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_document(self):
        titles: list[str] = []
        report_texts = [(self.title, True), (self.overview.summary, True)]
        for row in self.overview.rows:
            report_texts.extend(
                ((row.phase, True), (row.event, True), (row.improvement, True))
            )
        for section in self.sections:
            titles.append(section.title.strip())
            report_texts.append((section.title, True))
            for block in section.blocks:
                if isinstance(block, ReportSubsection):
                    titles.append(block.title.strip())
                    report_texts.append((block.title, True))
                    report_texts.extend(_block_texts(block.blocks))
                else:
                    report_texts.extend(_block_texts([block]))
        if any(title in RESERVED_SECTION_TITLES for title in titles):
            raise ValueError("reserved report section title")
        if len(titles) != len(set(titles)):
            raise ValueError("section and subsection titles must be unique")
        validate_unique_ids(self.evidence_segment_ids, field_name="evidence_segment_ids")
        validate_unique_ids(self.external_source_ids, field_name="external_source_ids")
        for text, enforce_voice in report_texts:
            validate_report_text(text, enforce_voice=enforce_voice)
        return self


def _block_texts(blocks: list[LeafReportBlock] | list[ReportBlock]) -> list[tuple[str, bool]]:
    texts: list[tuple[str, bool]] = []
    for block in blocks:
        if isinstance(block, (ParagraphBlock, SuggestedWordingBlock)):
            texts.append((block.text, True))
        elif isinstance(block, SourceQuoteBlock):
            texts.append((block.text, False))
        elif isinstance(block, (BulletListBlock, NumberedListBlock)):
            texts.extend((item, True) for item in block.items)
        elif isinstance(block, TableBlock):
            texts.extend((cell, True) for cell in block.columns)
            texts.extend((cell, True) for row in block.rows for cell in row)
    return texts
