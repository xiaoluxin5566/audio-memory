from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Sequence

from audio_memory.analysis.segmented_report_audit import partition_transcript_for_audit
from audio_memory.prompts.beta8_pipeline_schema import (
    AggregatedAudit,
    AuditPhase,
    AuditScope,
    Beta8AuditResult,
)


@dataclass(frozen=True, slots=True)
class AuditUnit:
    audit_unit_id: str
    phase: AuditPhase
    scope: AuditScope
    transcript_segments: Sequence[dict[str, object]]


def plan_audit_units(
    transcript: Sequence[dict[str, object]],
    *,
    phase: AuditPhase,
    max_markdown_chars: int,
) -> Sequence[AuditUnit]:
    chunks = partition_transcript_for_audit(
        transcript, max_markdown_chars=max_markdown_chars
    )
    units = [
        AuditUnit(
            audit_unit_id=f"beta8:{phase}:evidence:{index}",
            phase=phase,
            scope="evidence_chunk",
            transcript_segments=tuple(chunk.segments),
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    units.append(AuditUnit(
        audit_unit_id=f"beta8:{phase}:global",
        phase=phase,
        scope="global_report",
        transcript_segments=(),
    ))
    return tuple(units)


def _canonical_issue(issue) -> tuple[str, dict[str, object]]:
    payload = issue.model_dump(mode="json")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return sha256(encoded).hexdigest(), payload


def aggregate_audit_results(
    expected_units: Sequence[AuditUnit],
    results: Sequence[Beta8AuditResult],
    *,
    known_card_ids: set[str],
) -> AggregatedAudit:
    expected_by_id = {unit.audit_unit_id: unit for unit in expected_units}
    if len(expected_by_id) != len(expected_units):
        raise ValueError("Expected audit unit IDs must be unique")
    counts: dict[str, int] = {}
    for result in results:
        counts[result.audit_unit_id] = counts.get(result.audit_unit_id, 0) + 1
    unexpected = sorted(set(counts) - set(expected_by_id))
    if unexpected:
        raise ValueError(f"Unexpected audit units: {', '.join(unexpected)}")
    duplicates = sorted(unit_id for unit_id, count in counts.items() if count != 1)
    if duplicates:
        raise ValueError(f"Duplicate audit units: {', '.join(duplicates)}")
    missing = sorted(set(expected_by_id) - set(counts))
    if missing:
        raise ValueError(f"Missing audit units: {', '.join(missing)}")

    unknown_card_ids = sorted({
        issue.card_id
        for result in results
        for issue in result.issues
        if issue.card_id is not None and issue.card_id not in known_card_ids
    })
    if unknown_card_ids:
        raise ValueError(
            "Audit issues reference unknown card IDs: "
            + ", ".join(unknown_card_ids)
        )

    result_by_id = {result.audit_unit_id: result for result in results}
    for unit_id, unit in expected_by_id.items():
        result = result_by_id[unit_id]
        if result.audit_phase != unit.phase or result.audit_scope != unit.scope:
            raise ValueError(f"Audit unit phase or scope mismatch: {unit_id}")
        known_segments = {
            str(segment["segment_id"]) for segment in unit.transcript_segments
        }
        unknown = sorted({
            segment_id
            for issue in result.issues
            for segment_id in issue.evidence_segment_ids
            if segment_id not in known_segments
        })
        if unknown and unit.scope == "evidence_chunk":
            raise ValueError(
                f"Audit unit references unknown segment IDs: {', '.join(unknown)}"
            )
        if unit.scope == "global_report" and any(
            issue.evidence_segment_ids for issue in result.issues
        ):
            raise ValueError("Global audit issues cannot claim transcript evidence")

    ordered_results = [result_by_id[unit.audit_unit_id] for unit in expected_units]
    deduplicated: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for result in ordered_results:
        for issue in result.issues:
            fingerprint, payload = _canonical_issue(issue)
            if fingerprint not in deduplicated:
                order.append(fingerprint)
                deduplicated[fingerprint] = {
                    **payload,
                    "origin_audit_unit_ids": [result.audit_unit_id],
                }
            else:
                origins = deduplicated[fingerprint]["origin_audit_unit_ids"]
                assert isinstance(origins, list)
                if result.audit_unit_id not in origins:
                    origins.append(result.audit_unit_id)

    issues = []
    for index, fingerprint in enumerate(order, start=1):
        issues.append({
            "audit_issue_id": f"issue-{index:04d}",
            **deduplicated[fingerprint],
        })
    completed = [
        unit.audit_unit_id
        for unit in expected_units
        if result_by_id[unit.audit_unit_id].input_complete
    ]
    incomplete = [
        unit.audit_unit_id
        for unit in expected_units
        if not result_by_id[unit.audit_unit_id].input_complete
    ]
    phase: AuditPhase = expected_units[0].phase if expected_units else "initial"
    return AggregatedAudit.model_validate({
        "audit_phase": phase,
        "expected_audit_unit_ids": [unit.audit_unit_id for unit in expected_units],
        "completed_audit_unit_ids": completed,
        "incomplete_audit_unit_ids": incomplete,
        "issues": issues,
    })
