from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from audio_memory.analysis.full_transcript import build_full_transcript_markdown
from audio_memory.prompts.direct_report_audit_schema import ReportAudit


@dataclass(frozen=True, slots=True)
class TranscriptAuditChunk:
    index: int
    total: int
    segments: tuple[dict[str, object], ...]

    @property
    def segment_count(self) -> int:
        return len(self.segments)


def partition_transcript_for_audit(
    transcript: Sequence[dict[str, object]],
    *,
    max_markdown_chars: int = 70_000,
) -> tuple[TranscriptAuditChunk, ...]:
    if max_markdown_chars <= 0:
        raise ValueError("max_markdown_chars must be positive")
    if not transcript:
        return ()
    groups: list[list[dict[str, object]]] = []
    start = 0
    while start < len(transcript):
        low, high, best = start + 1, len(transcript), start + 1
        while low <= high:
            midpoint = (low + high) // 2
            candidate = list(transcript[start:midpoint])
            if (
                len(candidate) == 1
                or len(build_full_transcript_markdown(candidate)) <= max_markdown_chars
            ):
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        groups.append(list(transcript[start:best]))
        start = best
    total = len(groups)
    return tuple(
        TranscriptAuditChunk(
            index=index,
            total=total,
            segments=tuple(group),
        )
        for index, group in enumerate(groups, start=1)
    )


T = TypeVar("T")


async def parallel_map_audit_chunks(
    chunks: Sequence[TranscriptAuditChunk],
    operation: Callable[[TranscriptAuditChunk], Awaitable[T]],
) -> tuple[T, ...]:
    return tuple(await asyncio.gather(*(operation(chunk) for chunk in chunks)))


def validate_atomic_audit_issues(audit: ReportAudit) -> None:
    for issue in audit.issues:
        if issue.related_section_ids:
            raise ValueError(
                f"audit issue must target one report location: {issue.issue_id}"
            )
