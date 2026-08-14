from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class DirectMarkdownQuality:
    passed: bool
    failures: tuple[str, ...]
    report_chars: int
    minimum_report_chars: int


def evaluate_direct_markdown_quality(
    markdown: str, *, transcript_chars: int
) -> DirectMarkdownQuality:
    failures: list[str] = []
    normalized = markdown.replace("\r\n", "\n")
    overview_match = re.search(
        r"^##\s+今天发生了什么，重点改进什么\s*$", normalized, re.M
    )
    table_after_overview = False
    if overview_match:
        following = normalized[overview_match.end() :]
        next_heading = re.search(r"^##\s+", following, re.M)
        overview_body = following[: next_heading.start()] if next_heading else following
        table_after_overview = bool(
            re.search(r"^\|.+\|\s*\n\|(?:\s*:?-{3,}:?\s*\|)+", overview_body, re.M)
        )
    if not table_after_overview:
        failures.append("overview_table")

    headings = [item.strip() for item in re.findall(r"^##\s+(.+?)\s*$", normalized, re.M)]
    detail_headings = [
        item
        for item in headings
        if item not in {"今天发生了什么，重点改进什么", "数据范围与判断边界"}
    ]
    if len(detail_headings) < 2:
        failures.append("detail_depth")
    if "数据范围与判断边界" not in headings:
        failures.append("judgment_boundary")

    report_chars = len(normalized)
    minimum_report_chars = 0
    if transcript_chars >= 50_000:
        minimum_report_chars = min(6_000, max(4_000, int(transcript_chars * 0.015)))
        if report_chars < minimum_report_chars:
            failures.append("analysis_depth")
    return DirectMarkdownQuality(
        passed=not failures,
        failures=tuple(failures),
        report_chars=report_chars,
        minimum_report_chars=minimum_report_chars,
    )
