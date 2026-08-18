from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import TypeVar

from audio_memory.analysis.audit_model_policy import (
    AuditModelPolicy,
    audit_transcript_budget_chars,
)
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


def audit_chunk_id(
    chunk: TranscriptAuditChunk,
    *,
    report_fingerprint: str,
    prompt_fingerprint: str,
    policy_version: str,
) -> str:
    payload = {
        "first_segment_id": str(chunk.segments[0]["segment_id"]),
        "last_segment_id": str(chunk.segments[-1]["segment_id"]),
        "segment_count": chunk.segment_count,
        "report_fingerprint": report_fingerprint,
        "prompt_fingerprint": prompt_fingerprint,
        "policy_version": policy_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def split_audit_chunk(
    chunk: TranscriptAuditChunk,
) -> tuple[TranscriptAuditChunk, TranscriptAuditChunk]:
    if chunk.segment_count < 2:
        raise ValueError("cannot split a single-segment audit chunk")
    midpoint = chunk.segment_count // 2
    return (
        TranscriptAuditChunk(1, 2, chunk.segments[:midpoint]),
        TranscriptAuditChunk(2, 2, chunk.segments[midpoint:]),
    )


def partition_transcript_for_audit(
    transcript: Sequence[dict[str, object]],
    *,
    max_markdown_chars: int | None = None,
    model_policy: AuditModelPolicy | None = None,
    fixed_prompt_chars: int = 0,
) -> tuple[TranscriptAuditChunk, ...]:
    if model_policy is not None:
        if max_markdown_chars is not None:
            raise ValueError(
                "max_markdown_chars and model_policy are mutually exclusive"
            )
        max_markdown_chars = audit_transcript_budget_chars(
            model_policy, fixed_prompt_chars=fixed_prompt_chars
        )
    elif max_markdown_chars is None:
        max_markdown_chars = 70_000
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
