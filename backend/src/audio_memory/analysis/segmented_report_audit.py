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


def promote_single_chunk_audit(
    audit: ReportAudit, *, total_segment_count: int
) -> ReportAudit:
    """Promote a complete one-chunk audit to the equivalent full audit."""
    if audit.audit_mode != "chunk_v1_audit":
        raise ValueError("only a chunk V1 audit can be promoted")
    if (
        audit.coverage.reviewed_segment_count != total_segment_count
        or audit.coverage.total_segment_count != total_segment_count
        or audit.coverage.unreviewed_ranges
    ):
        raise ValueError("single chunk audit does not cover the full transcript")
    payload = audit.model_dump(mode="json")
    payload["audit_mode"] = "full_v1_audit"
    payload["coverage"] = {
        **payload["coverage"],
        "full_transcript_reviewed": True,
    }
    return ReportAudit.model_validate(payload)


def merge_chunk_audits_deterministically(
    audits: Sequence[ReportAudit], *, total_segment_count: int
) -> ReportAudit:
    """Build a conservative full audit without another provider request."""
    if len(audits) < 2:
        raise ValueError("deterministic merge requires multiple chunk audits")
    if any(item.audit_mode != "chunk_v1_audit" for item in audits):
        raise ValueError("deterministic merge only accepts chunk V1 audits")
    reviewed = sum(item.coverage.reviewed_segment_count or 0 for item in audits)
    if reviewed != total_segment_count:
        raise ValueError("chunk audits do not cover the full transcript")

    dimensions = (
        "factual_accuracy",
        "important_coverage",
        "analysis_depth",
        "actionability",
        "expression_structure",
    )
    scores = {
        name: min(getattr(item.scores, name) for item in audits)
        for name in dimensions
    }
    issues: list[dict[str, object]] = []
    unresolved_issue_ids: list[str] = []
    opportunities: list[dict[str, object]] = []
    deductions: list[dict[str, object]] = []
    for chunk_index, audit in enumerate(audits, start=1):
        unresolved = set(audit.unresolved_issue_ids)
        for issue_index, issue in enumerate(audit.issues, start=1):
            if len(issues) >= 100:
                break
            payload = issue.model_dump(mode="json")
            original_id = issue.issue_id
            payload["issue_id"] = (
                f"issue_chunk{chunk_index:03d}_{issue_index:03d}"
            )
            issues.append(payload)
            if original_id in unresolved:
                unresolved_issue_ids.append(str(payload["issue_id"]))
        for opportunity_index, opportunity in enumerate(
            audit.value_opportunities, start=1
        ):
            if len(opportunities) >= 100:
                break
            payload = opportunity.model_dump(mode="json")
            payload["opportunity_id"] = (
                f"opportunity_chunk{chunk_index:03d}_{opportunity_index:03d}"
            )
            opportunities.append(payload)
        remaining = 100 - len(deductions)
        if remaining > 0:
            deductions.extend(
                item.model_dump(mode="json")
                for item in audit.deductions[:remaining]
            )

    total = sum(scores.values())
    unresolved_by_id = {
        str(item["issue_id"]): item for item in issues
        if str(item["issue_id"]) in unresolved_issue_ids
    }
    has_material = any(
        item["severity"] in {"critical", "major"}
        for item in unresolved_by_id.values()
    )
    return ReportAudit.model_validate({
        "audit_mode": "full_v1_audit",
        "rubric_version": 1,
        "passed": total >= 75 and not has_material,
        "scores": {**scores, "total": total},
        "deductions": deductions,
        "coverage": {
            "full_transcript_reviewed": True,
            "reviewed_segment_count": total_segment_count,
            "total_segment_count": total_segment_count,
            "unreviewed_ranges": [],
            "summary": f"已完整合并 {len(audits)} 个分块审核。",
        },
        "issues": issues,
        "value_opportunities": opportunities,
        "unresolved_issue_ids": unresolved_issue_ids,
        "summary": "已按最保守分项分数合并全量审核。",
    })


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
