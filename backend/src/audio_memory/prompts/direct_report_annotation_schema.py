from __future__ import annotations

from typing import Literal

from pydantic import Field

from audio_memory.prompts.report_schema import StrictReportModel


ReportAnnotationType = Literal[
    "page_title",
    "overview",
    "section_heading",
    "subheading",
    "paragraph",
    "quote",
    "bullet_list",
    "numbered_list",
    "table",
]


class ReportAnnotation(StrictReportModel):
    block_id: str = Field(pattern=r"^block_[0-9]{3,}$")
    type: ReportAnnotationType


class DirectReportAnnotations(StrictReportModel):
    annotations: list[ReportAnnotation] = Field(min_length=1, max_length=1_000)
