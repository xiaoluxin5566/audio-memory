from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol


_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_PAGE_TITLE = re.compile(r"^#(?!#)\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ReportSection:
    section_id: str
    title: str
    markdown: str
    start: int
    end: int


class SectionRevisionLike(Protocol):
    section_id: str
    title: str
    revised_markdown: str
    evidence_segment_ids: object
    removes_repetition: bool
    repetition_reason: str | None


def replace_report_title(markdown: str, title: str | None) -> str:
    if title is None:
        return markdown
    normalized = title.strip()
    if not normalized or "\n" in normalized or normalized.startswith("#"):
        raise ValueError("invalid revised report title")
    match = _PAGE_TITLE.search(markdown)
    if match is None:
        raise ValueError("report page title not found")
    return markdown[: match.start()] + f"# {normalized}" + markdown[match.end() :]


def split_report_sections(markdown: str) -> tuple[ReportSection, ...]:
    matches = list(_SECTION_HEADING.finditer(markdown))
    return tuple(
        ReportSection(
            section_id=f"section_{index + 1:03d}",
            title=match.group(1).strip(),
            markdown=markdown[match.start() : matches[index + 1].start() if index + 1 < len(matches) else len(markdown)],
            start=match.start(),
            end=matches[index + 1].start() if index + 1 < len(matches) else len(markdown),
        )
        for index, match in enumerate(matches)
    )


def apply_section_revisions(
    markdown: str,
    revisions: tuple[SectionRevisionLike, ...],
    valid_segment_ids: set[str],
) -> str:
    sections = {item.section_id: item for item in split_report_sections(markdown)}
    seen: set[str] = set()
    replacements: list[tuple[int, int, str]] = []
    for revision in revisions:
        if revision.section_id in seen:
            raise ValueError(f"duplicate section revision: {revision.section_id}")
        seen.add(revision.section_id)
        section = sections.get(revision.section_id)
        if section is None:
            raise ValueError(f"unknown section: {revision.section_id}")
        if revision.title.strip() != section.title:
            raise ValueError(f"section title mismatch: {revision.section_id}")
        expected_heading = f"## {section.title}"
        if revision.revised_markdown.splitlines()[0].strip() != expected_heading:
            raise ValueError(f"section title mismatch: {revision.section_id}")
        evidence_ids = tuple(str(item) for item in revision.evidence_segment_ids)
        unknown_ids = set(evidence_ids) - valid_segment_ids
        if unknown_ids:
            raise ValueError(f"unknown evidence segment ids: {sorted(unknown_ids)}")
        minimum_length = int(len(section.markdown) * 0.70)
        if len(revision.revised_markdown) < minimum_length:
            allowed = revision.removes_repetition and bool(
                revision.repetition_reason and revision.repetition_reason.strip()
            )
            if not allowed:
                raise ValueError(f"abnormally short section revision: {revision.section_id}")
        replacement = revision.revised_markdown.rstrip()
        if section.end < len(markdown):
            replacement += "\n\n"
        elif markdown.endswith("\n"):
            replacement += "\n"
        replacements.append((section.start, section.end, replacement))

    result = markdown
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result
