from __future__ import annotations

import asyncio
import pytest

from audio_memory.analysis.segmented_report_audit import (
    parallel_map_audit_chunks,
    partition_transcript_for_audit,
    validate_atomic_audit_issues,
)
from audio_memory.analysis.full_transcript import build_full_transcript_markdown
from audio_memory.prompts.direct_report_audit_schema import ReportAudit


def _segment(index: int, text: str) -> dict[str, object]:
    return {
        "segment_id": f"seg_0_{index}",
        "file_id": "file-1",
        "file_name": "day.mp3",
        "recording_started_at": None,
        "timezone": "Asia/Shanghai",
        "start_ms": index * 1_000,
        "end_ms": (index + 1) * 1_000,
        "speaker_id": "unknown",
        "text": text,
    }


def test_partition_transcript_preserves_order_and_all_segments() -> None:
    transcript = [_segment(index, "x" * 25) for index in range(7)]

    chunks = partition_transcript_for_audit(transcript, max_markdown_chars=250)

    assert len(chunks) >= 3
    assert [item["segment_id"] for chunk in chunks for item in chunk.segments] == [
        f"seg_0_{index}" for index in range(7)
    ]
    assert [chunk.index for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(chunk.total == len(chunks) for chunk in chunks)
    assert all(len(build_full_transcript_markdown(list(chunk.segments))) <= 250
               for chunk in chunks)


def test_partition_keeps_oversized_single_segment_intact() -> None:
    chunks = partition_transcript_for_audit(
        [_segment(0, "x" * 300)], max_markdown_chars=250
    )

    assert len(chunks) == 1
    assert chunks[0].segment_count == 1


def test_partition_fills_each_nonfinal_chunk_as_much_as_possible() -> None:
    transcript = [_segment(index, "x" * 80) for index in range(30)]
    chunks = partition_transcript_for_audit(transcript, max_markdown_chars=700)

    for chunk, next_chunk in zip(chunks, chunks[1:], strict=False):
        extended = [*chunk.segments, next_chunk.segments[0]]
        assert len(build_full_transcript_markdown(extended)) > 700


def test_parallel_map_starts_all_chunk_calls_before_releasing_them() -> None:
    chunks = partition_transcript_for_audit(
        [_segment(index, "x" * 25) for index in range(4)],
        max_markdown_chars=170,
    )
    started: list[int] = []
    release = asyncio.Event()

    async def run_chunk(chunk):
        started.append(chunk.index)
        if len(started) == len(chunks):
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)
        return chunk.index

    results = asyncio.run(parallel_map_audit_chunks(chunks, run_chunk))

    assert started == [1, 2, 3, 4]
    assert results == (1, 2, 3, 4)


def test_merged_audit_rejects_one_issue_covering_multiple_locations() -> None:
    payload = {
        "audit_mode": "full_v1_audit",
        "rubric_version": 1,
        "passed": False,
        "scores": {
            "factual_accuracy": 20, "important_coverage": 20,
            "analysis_depth": 15, "actionability": 9,
            "expression_structure": 5, "total": 69,
        },
        "deductions": [],
        "coverage": {
            "full_transcript_reviewed": True,
            "reviewed_segment_count": 2,
            "total_segment_count": 2,
            "unreviewed_ranges": [], "summary": "complete",
        },
        "issues": [{
            "issue_id": "issue_process",
            "severity": "major", "issue_type": "process_leakage",
            "section_id": "section_001",
            "related_section_ids": ["section_004"],
            "problem": "Two locations are bundled.",
            "importance": "Cannot verify each occurrence.",
            "required_change": "Delete both.",
            "affected_claims": ["claim one", "claim two"],
            "evidence_segment_ids": ["report_section_001"],
            "evidence_excerpts": [{
                "segment_id": "report_section_001", "text": "claim one"
            }],
            "context_excerpts": [],
            "allow_deletion_or_compression": False,
        }],
        "unresolved_issue_ids": ["issue_process"],
        "summary": "issues",
    }
    audit = ReportAudit.model_validate(payload)

    with pytest.raises(ValueError, match="one report location"):
        validate_atomic_audit_issues(audit)


def test_atomic_audit_accepts_multiple_claims_in_one_section() -> None:
    payload = {
        "audit_mode": "full_v1_audit",
        "rubric_version": 1,
        "passed": False,
        "scores": {
            "factual_accuracy": 20, "important_coverage": 20,
            "analysis_depth": 15, "actionability": 9,
            "expression_structure": 5, "total": 69,
        },
        "deductions": [],
        "coverage": {
            "full_transcript_reviewed": True,
            "reviewed_segment_count": 2,
            "total_segment_count": 2,
            "unreviewed_ranges": [], "summary": "complete",
        },
        "issues": [{
            "issue_id": "issue_process",
            "severity": "major", "issue_type": "process_leakage",
            "section_id": "section_001",
            "related_section_ids": [],
            "problem": "One process problem has several phrases in one section.",
            "importance": "All phrases need the same bounded correction.",
            "required_change": "Delete the process language.",
            "affected_claims": ["claim one", "claim two"],
            "evidence_segment_ids": ["report_section_001"],
            "evidence_excerpts": [{
                "segment_id": "report_section_001", "text": "claim one and claim two"
            }],
            "context_excerpts": [],
            "allow_deletion_or_compression": False,
        }],
        "unresolved_issue_ids": ["issue_process"],
        "summary": "issues",
    }

    validate_atomic_audit_issues(ReportAudit.model_validate(payload))
