#!/usr/bin/env python3
"""Opt-in historical evaluation for the audited report pipeline."""

from __future__ import annotations

import asyncio
import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time

import httpx
from sqlalchemy import select

from audio_memory.analysis.direct_markdown_quality import evaluate_direct_markdown_quality
from audio_memory.analysis.direct_report_pipeline import (
    apply_audited_revision, audit_title_revision_section_ids,
    audit_transcript_evidence_ids, build_section_diffs,
    canonicalize_audit_evidence, revision_target_section_ids,
    sanitize_audit_evidence,
    validate_audit_evidence,
)
from audio_memory.analysis.direct_report_sections import split_report_sections
from audio_memory.analysis.full_transcript import build_full_transcript_markdown
from audio_memory.analysis.markdown_report import (
    MarkdownReportResult,
    append_report_metrics,
)
from audio_memory.analysis.segmented_report_audit import (
    parallel_map_audit_chunks,
    partition_transcript_for_audit,
    validate_atomic_audit_issues,
)
from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.analysis.transcript_import import load_merged_markdown_transcript
from audio_memory.config import AppPaths
from audio_memory.db import Database
from audio_memory.models import JobFile, Transcript
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.direct_report_audit_schema import ReportAudit
from audio_memory.prompts.direct_report_revision_schema import TargetedReportRevision
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.providers.types import PROVIDER_CONFIGS
from audio_memory.transcript_safety import safe_active_profile_facts


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs/2026-07-29-four-audio-transcripts/2026-07-29_六段音频_合并逐字稿.md"
BASELINE = ROOT / "outputs/deepseek-three-pass-direct-report/run-20260814-152153"


async def load_profile() -> list[dict[str, object]]:
    database = Database(AppPaths.from_home(Path.home()).database)
    try:
        async with database.session() as session:
            facts = await safe_active_profile_facts(session)
        return [{"subject_id": item.subject_id, "dimension": item.dimension,
                 "value": json.loads(item.value_json), "confidence": item.confidence,
                 "origin": item.origin} for item in facts]
    finally:
        await database.dispose()


def parse_audit(raw: str, expected_mode: str) -> ReportAudit:
    audit = ReportAudit.model_validate(json.loads(raw))
    if audit.audit_mode != expected_mode:
        raise ValueError(f"unexpected audit mode: {audit.audit_mode}")
    return audit


async def call(
    client, request, *, markdown: bool = False, allow_parallel: bool = False
) -> tuple[str, float]:
    started = time.perf_counter()
    method = client.generate_markdown if markdown else client.generate
    raw = await method(
        "deepseek", system=request.instructions, user=request.user_data,
        model_id=PROVIDER_CONFIGS["deepseek"].model_id,
        scene_id=request.scene_id, max_tokens=request.max_tokens,
        timeout_seconds=request.timeout_seconds,
        segment_count=request.segment_count,
        **({} if markdown else {
            "thinking_enabled": True,
            "allow_parallel": allow_parallel,
        }),
    )
    return raw, time.perf_counter() - started


async def load_job_transcript(job_id: str) -> list[dict[str, object]]:
    database = Database(ROOT / "audio-memory.sqlite3")
    try:
        async with database.session() as session:
            rows = list((await session.execute(
                select(Transcript, JobFile).join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(JobFile.job_id == job_id, Transcript.risk_classified.is_(True),
                       Transcript.is_reliable.is_(True))
                .order_by(JobFile.position, Transcript.segment_index)
            )).all())
        if not rows:
            raise ValueError(f"no reliable transcript found for job: {job_id}")
        return [{"segment_id": f"seg_{file.position}_{row.segment_index}",
                 "file_id": file.id, "file_name": file.original_name,
                 "recording_started_at": file.recording_started_at,
                 "timezone": file.timezone or "Asia/Shanghai",
                 "start_ms": row.start_ms, "end_ms": row.end_ms,
                 "speaker_id": row.speaker_id or "unknown", "text": row.text}
                for row, file in rows]
    finally:
        await database.dispose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--label", default="2026-07-29")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-audit", action="store_true")
    parser.add_argument("--resume-raw-audit", action="store_true")
    parser.add_argument("--resume-chunks", action="store_true")
    parser.add_argument("--resume-raw-chunks", action="store_true")
    parser.add_argument("--resume-revision", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve() if args.output else (
        ROOT / "outputs/deepseek-audited-report" /
        f"{args.label}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    output.mkdir(parents=True, exist_ok=True)
    if args.job_id:
        transcript = await load_job_transcript(args.job_id)
    else:
        imported = load_merged_markdown_transcript(SOURCE)
        transcript = [{**item, "file_name": item["source_title"],
                       "recording_started_at": None, "timezone": "Asia/Shanghai",
                       "speaker_id": "unknown"} for item in imported]
    transcript_md = build_full_transcript_markdown(transcript)
    transcript_by_id = {str(item["segment_id"]): str(item["text"]) for item in transcript}
    valid_ids = set(transcript_by_id)
    profile = await load_profile()
    composer = PromptComposer()
    goal = composer.default_user_analysis_goal()
    timings: dict[str, float] = {}

    async with httpx.AsyncClient() as http:
        client = ProviderAnalysisClient(KeychainRepository(MacSecurityClient()), http)
        if args.resume and (output / "v1-report.md").exists():
            v1 = MarkdownReportResult.from_markdown(
                (output / "v1-report.md").read_text(encoding="utf-8"))
            timings["generation"] = 0.0
        else:
            raw, timings["generation"] = await call(client, composer.compose_direct_report_markdown(
                transcript_markdown=transcript_md, profile=profile,
                user_analysis_prompt=goal, segment_count=len(transcript)), markdown=True)
            v1 = MarkdownReportResult.from_markdown(raw)
            (output / "v1-report.md").write_text(v1.report_markdown, encoding="utf-8")
        v1_gate = evaluate_direct_markdown_quality(v1.report_markdown, transcript_chars=len(transcript_md))

        if args.resume_raw_audit and (output / "raw-v1-audit.json").exists():
            audit = ReportAudit.model_validate_json(
                (output / "raw-v1-audit.json").read_text(encoding="utf-8"))
            audit = canonicalize_audit_evidence(
                audit,
                transcript_by_id,
                report_markdown=v1.report_markdown,
            )
            validate_audit_evidence(
                audit,
                transcript_by_id=transcript_by_id,
                report_markdown=v1.report_markdown,
            )
            (output / "v1-audit.json").write_text(
                json.dumps(audit.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            timings["v1_audit"] = 0.0
        elif args.resume_audit and (output / "v1-audit.json").exists():
            audit = ReportAudit.model_validate_json(
                (output / "v1-audit.json").read_text(encoding="utf-8"))
            timings["v1_audit"] = 0.0
        else:
            chunks = partition_transcript_for_audit(transcript)

            async def audit_chunk(chunk):
                chunk_transcript = list(chunk.segments)
                request = composer.compose_report_audit_chunk(
                    transcript_markdown=build_full_transcript_markdown(chunk_transcript),
                    profile=profile, user_analysis_prompt=goal,
                    v1_markdown=v1.report_markdown,
                    sections=split_report_sections(v1.report_markdown),
                    gate_failures=v1_gate.failures,
                    chunk_index=chunk.index, chunk_count=chunk.total,
                    segment_count=chunk.segment_count,
                    total_segment_count=len(transcript),
                )
                raw_chunk_path = output / f"raw-v1-audit-chunk-{chunk.index:02d}.json"
                if args.resume_raw_chunks and raw_chunk_path.exists():
                    raw_chunk = raw_chunk_path.read_text(encoding="utf-8")
                    elapsed = 0.0
                else:
                    raw_chunk, elapsed = await call(
                        client, request, allow_parallel=True
                    )
                    raw_chunk_path.write_text(raw_chunk, encoding="utf-8")
                chunk_audit = parse_audit(raw_chunk, "chunk_v1_audit")
                chunk_by_id = {
                    str(item["segment_id"]): str(item["text"])
                    for item in chunk_transcript
                }
                chunk_audit = sanitize_audit_evidence(
                    chunk_audit, chunk_by_id
                )
                chunk_audit = canonicalize_audit_evidence(
                    chunk_audit, chunk_by_id,
                    report_markdown=v1.report_markdown,
                )
                validate_audit_evidence(
                    chunk_audit, transcript_by_id=chunk_by_id,
                    report_markdown=v1.report_markdown,
                )
                validate_atomic_audit_issues(chunk_audit)
                return chunk_audit, elapsed

            chunks_path = output / "v1-audit-chunks.json"
            if args.resume_chunks and chunks_path.exists():
                chunk_audits = [
                    ReportAudit.model_validate(item)
                    for item in json.loads(chunks_path.read_text(encoding="utf-8"))
                ]
                if len(chunk_audits) != len(chunks):
                    raise ValueError("saved audit chunk count does not match input")
                timings["v1_audit_chunks_wall"] = 0.0
            else:
                audit_started = time.perf_counter()
                chunk_results = await parallel_map_audit_chunks(chunks, audit_chunk)
                timings["v1_audit_chunks_wall"] = time.perf_counter() - audit_started
                chunk_audits = [item[0] for item in chunk_results]
                chunks_path.write_text(
                    json.dumps(
                        [item.model_dump(mode="json") for item in chunk_audits],
                        ensure_ascii=False, indent=2,
                    ),
                    encoding="utf-8",
                )
            raw, timings["v1_audit_merge"] = await call(
                client,
                composer.compose_merged_report_audit(
                    v1_markdown=v1.report_markdown,
                    sections=split_report_sections(v1.report_markdown),
                    gate_failures=v1_gate.failures,
                    chunk_audits=chunk_audits,
                    total_segment_count=len(transcript),
                ),
            )
            (output / "raw-v1-audit.json").write_text(raw, encoding="utf-8")
            audit = parse_audit(raw, "full_v1_audit")
            validate_atomic_audit_issues(audit)
            audit = canonicalize_audit_evidence(
                audit,
                transcript_by_id,
                report_markdown=v1.report_markdown,
            )
            validate_audit_evidence(
                audit,
                transcript_by_id=transcript_by_id,
                report_markdown=v1.report_markdown,
            )
            (output / "v1-audit.json").write_text(
                json.dumps(audit.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")

        report = v1
        final_audit = audit
        revision = None
        if audit.issues or audit.value_opportunities:
            sections = split_report_sections(v1.report_markdown)
            editable_ids = revision_target_section_ids(v1.report_markdown, audit)
            editable = [{"section_id": item.section_id, "title": item.title,
                         "markdown": item.markdown} for item in sections
                        if item.section_id in editable_ids]
            adjacent_ids: set[str] = set()
            for index, section in enumerate(sections):
                if section.section_id in editable_ids:
                    if index: adjacent_ids.add(sections[index - 1].section_id)
                    if index + 1 < len(sections): adjacent_ids.add(sections[index + 1].section_id)
            adjacent = [{"section_id": item.section_id, "title": item.title,
                         "markdown": item.markdown} for item in sections
                        if item.section_id in adjacent_ids - editable_ids]
            allowed = audit_transcript_evidence_ids(audit, valid_ids)
            revision_candidates = [
                path for path in (
                    output / "revision.json", output / "raw-revision.json"
                ) if path.exists()
            ]
            if args.resume_revision and revision_candidates:
                latest_revision = max(
                    revision_candidates, key=lambda path: path.stat().st_mtime
                )
                revision = TargetedReportRevision.model_validate_json(
                    latest_revision.read_text(encoding="utf-8"))
                timings["revision"] = 0.0
            else:
                raw, timings["revision"] = await call(client, composer.compose_targeted_report_revision(
                    v1_title=v1.title,
                    section_outline=[{"section_id": item.section_id, "title": item.title} for item in sections],
                    editable_sections=editable, adjacent_sections=adjacent,
                    audit=audit, allowed_segment_ids=allowed))
                (output / "raw-revision.json").write_text(raw, encoding="utf-8")
                revision = TargetedReportRevision.model_validate(json.loads(raw))
            v2_md = apply_audited_revision(v1.report_markdown, audit, revision, valid_ids)
            diffs = build_section_diffs(
                v1.report_markdown,
                v2_md,
                allowed_title_change_ids=audit_title_revision_section_ids(
                    v1.report_markdown, audit
                ),
            )
            report = MarkdownReportResult.from_markdown(v2_md)
            (output / "revision.json").write_text(
                json.dumps(revision.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "v2-diffs.json").write_text(json.dumps(diffs, ensure_ascii=False, indent=2), encoding="utf-8")
            final_raw_path = output / "raw-v2-final-audit.json"
            revision_raw_path = output / "raw-revision.json"
            reusable_final = (
                args.resume_revision
                and final_raw_path.exists()
                and revision_raw_path.exists()
                and final_raw_path.stat().st_mtime >= revision_raw_path.stat().st_mtime
            )
            if reusable_final:
                raw = final_raw_path.read_text(encoding="utf-8")
                timings["final_audit"] = 0.0
            else:
                raw, timings["final_audit"] = await call(client, composer.compose_revision_final_audit(
                    v2_markdown=report.report_markdown, section_diffs=diffs,
                    v1_audit=audit, revision=revision))
                final_raw_path.write_text(raw, encoding="utf-8")
            final_audit = parse_audit(raw, "revision_final_audit")
            (output / "v2-final-audit.json").write_text(
                json.dumps(final_audit.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")

        baseline_md = None
        baseline_audit = None
        if not args.skip_baseline:
            baseline_md = (BASELINE / "report.md").read_text(encoding="utf-8")
            baseline_gate = evaluate_direct_markdown_quality(baseline_md, transcript_chars=len(transcript_md))
            raw, timings["baseline_same_rubric_audit"] = await call(client, composer.compose_full_report_audit(
                transcript_markdown=transcript_md, profile=profile, user_analysis_prompt=goal,
                v1_markdown=baseline_md, sections=split_report_sections(baseline_md),
                gate_failures=baseline_gate.failures, segment_count=len(transcript)))
            baseline_audit = parse_audit(raw, "full_v1_audit")

    baseline_metrics = json.loads((BASELINE / "metrics.json").read_text(encoding="utf-8"))
    audit_chunk_count = len(partition_transcript_for_audit(transcript))
    new_call_count = 1 + audit_chunk_count + 1 + (2 if revision is not None else 0)
    published_markdown = append_report_metrics(
        report.report_markdown,
        initial_score=audit.scores.total,
        final_score=final_audit.scores.total,
        revised=revision is not None,
    )
    comparison = {
        "same_input": {"segments": len(transcript), "characters": len(transcript_md),
                       "profile_facts": len(profile), "model_id": PROVIDER_CONFIGS["deepseek"].model_id},
        "old": None if baseline_audit is None else {
                "production_call_count": baseline_metrics["model_call_count"],
                "recorded_elapsed_seconds": baseline_metrics["elapsed_seconds"],
                "report_characters": len(baseline_md or ""),
                "same_rubric_score": baseline_audit.scores.total,
                "component_scores": baseline_audit.scores.model_dump(mode="json"),
                "material_issue_count": len(baseline_audit.material_issues)},
        "new": {"production_call_count": new_call_count,
                "measured_elapsed_seconds": round(sum(value for key, value in timings.items()
                                                      if key != "baseline_same_rubric_audit"), 2),
                "per_call_seconds": {key: round(value, 2) for key, value in timings.items()
                                     if key != "baseline_same_rubric_audit"},
                "report_characters": len(published_markdown),
                "final_score": final_audit.scores.total,
                "component_scores": final_audit.scores.model_dump(mode="json"),
                "material_issue_count": len(final_audit.material_issues),
                "published_version": "v2" if revision is not None else "v1"},
        "evaluation_overhead": {"baseline_audit_seconds": round(timings.get("baseline_same_rubric_audit", 0), 2)},
        "provider_usage_all_evaluation_calls": client.usage_totals,
        "provider_diagnostics": [asdict(item) for item in client.request_diagnostics],
    }
    (output / "report.md").write_text(published_markdown, encoding="utf-8")
    if baseline_audit is not None:
        (output / "baseline-same-rubric-audit.json").write_text(
            json.dumps(baseline_audit.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "transcript-input.md").write_text(transcript_md, encoding="utf-8")
    (output / "profile-snapshot.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **comparison}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
