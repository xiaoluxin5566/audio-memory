from __future__ import annotations

from dataclasses import dataclass
import re

from audio_memory.prompts.direct_report_annotation_schema import (
    DirectReportAnnotations,
    ReportAnnotation,
    ReportAnnotationType,
)


@dataclass(frozen=True, slots=True)
class ReportBlock:
    block_id: str
    markdown: str
    inferred_type: ReportAnnotationType


def parse_report_blocks(markdown: str) -> tuple[ReportBlock, ...]:
    chunks: list[str] = []
    position = 0
    for separator in re.finditer(r"\n[ \t]*\n", markdown):
        end = separator.end()
        chunks.append(markdown[position:end])
        position = end
    if position < len(markdown):
        chunks.append(markdown[position:])
    return tuple(
        ReportBlock(
            block_id=f"block_{index + 1:03d}",
            markdown=chunk,
            inferred_type=_infer_type(chunk),
        )
        for index, chunk in enumerate(chunks)
        if chunk
    )


def validate_annotations(
    blocks: tuple[ReportBlock, ...], annotations: DirectReportAnnotations
) -> tuple[ReportAnnotation, ...]:
    expected = [item.block_id for item in blocks]
    actual = [item.block_id for item in annotations.annotations]
    if len(actual) != len(set(actual)):
        raise ValueError("annotation block IDs must be unique")
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise ValueError("annotations must cover every report block exactly once")
    by_id = {item.block_id: item for item in annotations.annotations}
    return tuple(by_id[item] for item in expected)


def _infer_type(chunk: str) -> ReportAnnotationType:
    stripped = chunk.strip()
    first = stripped.splitlines()[0] if stripped else ""
    if first.startswith("# "):
        return "page_title"
    if first == "## 今天发生了什么，重点改进什么":
        return "overview"
    if first.startswith("## "):
        return "section_heading"
    if first.startswith("### "):
        return "subheading"
    if first.startswith(">"):
        return "quote"
    if re.match(r"^[-*]\s+", first):
        return "bullet_list"
    if re.match(r"^\d+[.)]\s+", first):
        return "numbered_list"
    if first.startswith("|") and len(stripped.splitlines()) >= 2:
        return "table"
    return "paragraph"
