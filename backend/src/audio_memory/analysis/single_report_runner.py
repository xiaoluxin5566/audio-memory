from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import logging
import time
from typing import Protocol

from sqlalchemy import select, update

from audio_memory.analysis.full_transcript import build_full_transcript_markdown
from audio_memory.analysis.audit_model_policy import resolve_audit_model_policy
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.analysis.markdown_report import (
    MarkdownReportResult,
    append_report_metrics,
)
from audio_memory.analysis.direct_markdown_quality import evaluate_direct_markdown_quality
from audio_memory.analysis.direct_report_sections import (
    normalize_report_headings,
    split_report_sections,
)
from audio_memory.analysis.direct_report_pipeline import (
    ReportQualityMetadata,
    apply_audited_revision,
    audit_title_revision_section_ids,
    audit_transcript_evidence_ids,
    build_section_diffs,
    canonicalize_audit_evidence,
    metadata_from_audit,
    revision_target_section_ids,
    sanitize_audit_evidence,
    validate_audit_evidence,
)
from audio_memory.analysis.direct_report_document import StructuredReportResult
from audio_memory.analysis.segmented_report_audit import (
    audit_chunk_id,
    partition_transcript_for_audit,
    split_audit_chunk,
    validate_atomic_audit_issues,
)
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion, JobFile, Transcript
from audio_memory.observability import emit_analysis_event
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.direct_report_schema import DirectReportDocument
from audio_memory.prompts.direct_report_audit_schema import ReportAudit
from audio_memory.prompts.direct_report_revision_schema import TargetedReportRevision
from audio_memory.analysis.runner import (
    CredentialChangedError,
    FixedRulesChangedError,
    LeaseLostError,
)


class MarkdownProvider(Protocol):
    async def generate_markdown(self, provider_id: str, **kwargs: object) -> str: ...


class SingleReportRunner:
    def __init__(
        self,
        *,
        database: Database,
        provider,
        publisher,
        generation_source,
        output_mode: str = "markdown",
    ) -> None:
        if output_mode not in {"markdown", "structured"}:
            raise ValueError(f"Unsupported direct report output mode: {output_mode}")
        self.database = database
        self.provider = provider
        self.publisher = publisher
        self.generation_source = generation_source
        self.output_mode = output_mode
        self.composer = PromptComposer()

    async def run(self, version_id: str, worker_owner_id: str | None = None):
        version = await self._version(version_id, worker_owner_id)
        if version.fixed_rules_hash != PromptComposer.fixed_rules_hash():
            raise FixedRulesChangedError("Fixed analysis rules changed")
        generation = await self.generation_source.credential_generation(version.provider_id)
        if generation != version.credential_generation:
            raise CredentialChangedError("Credential generation changed")
        staged = json.loads(version.staged_results_json or "{}")
        if self.output_mode == "structured":
            return await self._run_structured(
                version, staged, worker_owner_id=worker_owner_id
            )
        return await self._run_markdown(version, staged, worker_owner_id=worker_owner_id)

    async def _run_markdown(self, version, staged, *, worker_owner_id):
        await self._set_report_phase(version.id, "generating", worker_owner_id)
        transcript, markdown, profile, goal_content = await self._inputs(version)
        raw_report = staged.get("direct_report_v1_markdown")
        if not isinstance(raw_report, str) or not raw_report.strip():
            raw_report = staged.get("direct_report_initial_markdown")
        if not isinstance(raw_report, str) or not raw_report.strip():
            legacy_report = staged.get("direct_report_markdown")
            if isinstance(legacy_report, str) and legacy_report.strip():
                raw_report = legacy_report
        if not isinstance(raw_report, str) or not raw_report.strip():
            request = self.composer.compose_direct_report_markdown(
                transcript_markdown=markdown,
                profile=profile,
                user_analysis_prompt=goal_content,
                segment_count=len(transcript),
            )
            started = time.monotonic()
            raw_report = await self.provider.generate_markdown(
                version.provider_id,
                system=request.instructions,
                user=request.user_data,
                model_id=version.model_id,
                scene_id=request.scene_id,
                max_tokens=request.max_tokens,
                timeout_seconds=request.timeout_seconds,
                segment_count=request.segment_count,
            )
            raw_report = normalize_report_headings(raw_report)
            result = MarkdownReportResult.from_markdown(raw_report)
            staged["direct_report_v1_markdown"] = result.report_markdown
            staged["direct_report_initial_markdown"] = result.report_markdown
            await self._save_checkpoint(
                version.id,
                staged,
                worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )
        else:
            result = MarkdownReportResult.from_markdown(
                normalize_report_headings(raw_report)
            )
        v1_quality = evaluate_direct_markdown_quality(
            result.report_markdown, transcript_chars=len(markdown)
        )

        audit_payload = staged.get("direct_report_v1_audit")
        audit = None
        if isinstance(audit_payload, dict):
            try:
                candidate = ReportAudit.model_validate(audit_payload)
                if (
                    candidate.coverage.reviewed_segment_count != len(transcript)
                    or candidate.coverage.total_segment_count != len(transcript)
                ):
                    raise ValueError("saved V1 audit returned wrong coverage")
                audit = candidate
            except ValueError:
                staged.pop("direct_report_v1_audit", None)
        if audit is None:
            await self._set_report_phase(version.id, "auditing", worker_owner_id)
            started = time.monotonic()
            try:
                transcript_by_id = {
                    str(item["segment_id"]): str(item["text"])
                    for item in transcript
                }
                policy = resolve_audit_model_policy(
                    version.provider_id, version.model_id
                )
                fixed_prompt_chars = (
                    len(result.report_markdown)
                    + len(goal_content)
                    + len(json.dumps(profile, ensure_ascii=False))
                    + 20_000
                )
                chunks = partition_transcript_for_audit(
                    transcript,
                    model_policy=policy,
                    fixed_prompt_chars=fixed_prompt_chars,
                )
                report_fingerprint = sha256(
                    result.report_markdown.encode("utf-8")
                ).hexdigest()
                prompt_fingerprint = sha256(
                    json.dumps(
                        {
                            "goal": goal_content,
                            "profile": profile,
                            "fixed_rules": version.fixed_rules_hash,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                saved_chunk_results = staged.get(
                    "direct_report_v1_audit_chunk_results"
                )
                if not isinstance(saved_chunk_results, dict):
                    saved_chunk_results = {}
                    staged["direct_report_v1_audit_chunk_results"] = (
                        saved_chunk_results
                    )
                saved_chunk_splits = staged.get(
                    "direct_report_v1_audit_chunk_splits"
                )
                if not isinstance(saved_chunk_splits, dict):
                    saved_chunk_splits = {}
                    staged["direct_report_v1_audit_chunk_splits"] = (
                        saved_chunk_splits
                    )
                checkpoint_lock = asyncio.Lock()
                provider_limit = asyncio.Semaphore(policy.max_parallel_chunks)

                async def audit_chunk(
                    chunk, *, split_depth: int = 0, validation_attempt: int = 0
                ):
                    chunk_id = audit_chunk_id(
                        chunk,
                        report_fingerprint=report_fingerprint,
                        prompt_fingerprint=prompt_fingerprint,
                        policy_version=policy.policy_name,
                    )
                    saved_payload = saved_chunk_results.get(chunk_id)
                    if isinstance(saved_payload, dict):
                        try:
                            candidate = ReportAudit.model_validate(saved_payload)
                            if (
                                candidate.audit_mode != "chunk_v1_audit"
                                or candidate.coverage.reviewed_segment_count
                                != chunk.segment_count
                                or candidate.coverage.total_segment_count
                                != chunk.segment_count
                            ):
                                raise ValueError(
                                    "saved chunk audit returned wrong coverage"
                                )
                            return [candidate]
                        except ValueError:
                            saved_chunk_results.pop(chunk_id, None)
                    split_children = None
                    split_child_ids = None
                    if chunk.segment_count > policy.minimum_segment_count:
                        split_children = split_audit_chunk(chunk)
                        split_child_ids = [
                            audit_chunk_id(
                                child,
                                report_fingerprint=report_fingerprint,
                                prompt_fingerprint=prompt_fingerprint,
                                policy_version=policy.policy_name,
                            )
                            for child in split_children
                        ]
                    saved_split = saved_chunk_splits.get(chunk_id)
                    if (
                        split_children is not None
                        and saved_split == split_child_ids
                    ):
                        children = await asyncio.gather(
                            *(
                                audit_chunk(
                                    child, split_depth=split_depth + 1
                                )
                                for child in split_children
                            )
                        )
                        return [item for group in children for item in group]
                    if saved_split is not None:
                        saved_chunk_splits.pop(chunk_id, None)
                    chunk_markdown = build_full_transcript_markdown(
                        list(chunk.segments)
                    )
                    request = self.composer.compose_report_audit_chunk(
                        transcript_markdown=chunk_markdown,
                        profile=profile,
                        user_analysis_prompt=goal_content,
                        v1_markdown=result.report_markdown,
                        sections=split_report_sections(result.report_markdown),
                        gate_failures=v1_quality.failures,
                        chunk_index=chunk.index,
                        chunk_count=chunk.total,
                        segment_count=chunk.segment_count,
                        total_segment_count=len(transcript),
                    )
                    try:
                        async with provider_limit:
                            raw = await self.provider.generate(
                                version.provider_id,
                                system=request.instructions,
                                user=request.user_data,
                                model_id=version.model_id,
                                scene_id=request.scene_id,
                                max_tokens=request.max_tokens,
                                timeout_seconds=request.timeout_seconds,
                                segment_count=request.segment_count,
                                allow_parallel=True,
                            )
                    except ProviderAnalysisError as exc:
                        if (
                            exc.code == "model_output_truncated"
                            and chunk.segment_count > policy.minimum_segment_count
                        ):
                            assert split_children is not None
                            assert split_child_ids is not None
                            emit_analysis_event(
                                logging.getLogger("uvicorn.error"),
                                "analysis.report.audit_chunk_split",
                                analysis_version_id=version.id,
                                provider_id=version.provider_id,
                                model_id=version.model_id,
                                status="retrying",
                                reason=exc.code,
                                split_depth=split_depth,
                                segment_count=chunk.segment_count,
                                child_count=len(split_children),
                                chunk_index=chunk.index,
                                chunk_count=chunk.total,
                            )
                            saved_chunk_splits[chunk_id] = split_child_ids
                            async with checkpoint_lock:
                                await self._save_checkpoint(
                                    version.id,
                                    staged,
                                    worker_owner_id,
                                    duration_ms=int(
                                        (time.monotonic() - started) * 1_000
                                    ),
                                )
                            children = await asyncio.gather(
                                *(
                                    audit_chunk(
                                        child, split_depth=split_depth + 1
                                    )
                                    for child in split_children
                                )
                            )
                            return [item for group in children for item in group]
                        raise
                    try:
                        chunk_audit = ReportAudit.model_validate(json.loads(raw))
                        if chunk_audit.audit_mode != "chunk_v1_audit":
                            raise ValueError(
                                "chunk audit returned the wrong audit mode"
                            )
                        if (
                            chunk_audit.coverage.total_segment_count
                            != chunk.segment_count
                        ):
                            raise ValueError("chunk audit returned wrong coverage")
                        chunk_by_id = {
                            str(item["segment_id"]): str(item["text"])
                            for item in chunk.segments
                        }
                        chunk_audit = sanitize_audit_evidence(
                            chunk_audit, chunk_by_id
                        )
                        chunk_audit = canonicalize_audit_evidence(
                            chunk_audit,
                            chunk_by_id,
                            report_markdown=result.report_markdown,
                        )
                        validate_audit_evidence(
                            chunk_audit,
                            transcript_by_id=chunk_by_id,
                            report_markdown=result.report_markdown,
                        )
                        validate_atomic_audit_issues(chunk_audit)
                    except ValueError:
                        if validation_attempt < 1:
                            return await audit_chunk(
                                chunk,
                                split_depth=split_depth,
                                validation_attempt=validation_attempt + 1,
                            )
                        raise
                    saved_chunk_results[chunk_id] = chunk_audit.model_dump(
                        mode="json"
                    )
                    async with checkpoint_lock:
                        await self._save_checkpoint(
                            version.id,
                            staged,
                            worker_owner_id,
                            duration_ms=int(
                                (time.monotonic() - started) * 1_000
                            ),
                        )
                    return [chunk_audit]

                grouped_audits = await asyncio.gather(
                    *(audit_chunk(chunk) for chunk in chunks)
                )
                chunk_audits = tuple(
                    item for group in grouped_audits for item in group
                )
                staged["direct_report_v1_audit_chunks"] = [
                    item.model_dump(mode="json") for item in chunk_audits
                ]
                merge_request = self.composer.compose_merged_report_audit(
                    v1_markdown=result.report_markdown,
                    sections=split_report_sections(result.report_markdown),
                    gate_failures=v1_quality.failures,
                    chunk_audits=list(chunk_audits),
                    total_segment_count=len(transcript),
                )
                for merge_attempt in range(2):
                    raw_audit = await self.provider.generate(
                        version.provider_id,
                        system=merge_request.instructions,
                        user=merge_request.user_data,
                        model_id=version.model_id,
                        scene_id=merge_request.scene_id,
                        max_tokens=merge_request.max_tokens,
                        timeout_seconds=merge_request.timeout_seconds,
                        segment_count=merge_request.segment_count,
                        repair_attempted=merge_attempt > 0,
                    )
                    try:
                        audit = ReportAudit.model_validate(json.loads(raw_audit))
                        if audit.audit_mode != "full_v1_audit":
                            raise ValueError(
                                "merged V1 audit returned the wrong audit mode"
                            )
                        if (
                            audit.coverage.reviewed_segment_count != len(transcript)
                            or audit.coverage.total_segment_count != len(transcript)
                        ):
                            raise ValueError("merged V1 audit returned wrong coverage")
                        validate_atomic_audit_issues(audit)
                        audit = canonicalize_audit_evidence(
                            audit,
                            transcript_by_id,
                            report_markdown=result.report_markdown,
                        )
                        validate_audit_evidence(
                            audit,
                            transcript_by_id=transcript_by_id,
                            report_markdown=result.report_markdown,
                        )
                        break
                    except ValueError:
                        if merge_attempt:
                            raise
            except Exception as exc:
                emit_analysis_event(
                    logging.getLogger("uvicorn.error"),
                    "analysis.report.audit_recovery_failed",
                    analysis_version_id=version.id,
                    provider_id=version.provider_id,
                    model_id=version.model_id,
                    status="failed",
                    error=exc,
                )
                staged["direct_report_v1_audit_error"] = self._error_text(exc)
                await self._save_checkpoint(
                    version.id,
                    staged,
                    worker_owner_id,
                    duration_ms=int((time.monotonic() - started) * 1_000),
                )
                raise ProviderAnalysisError(
                    f"Report generated; audit pending retry: {exc}",
                    code="report_audit_pending",
                    retriable=True,
                ) from exc
            staged["direct_report_v1_audit"] = audit.model_dump(mode="json")
            emit_analysis_event(
                logging.getLogger("uvicorn.error"),
                "analysis.report.audit_recovery_completed",
                analysis_version_id=version.id,
                provider_id=version.provider_id,
                model_id=version.model_id,
                status="completed",
                audit_chunk_count=len(chunk_audits),
            )
            await self._save_checkpoint(
                version.id,
                staged,
                worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )

        if not audit.issues and not audit.value_opportunities:
            return await self._publish_report(
                version, staged, result,
                metadata_from_audit(
                    report_version="v1", audit=audit,
                    score_scope="v1_full_audit",
                ),
                v1_quality, worker_owner_id, 0,
            )

        sections = split_report_sections(result.report_markdown)
        section_map = {item.section_id: item for item in sections}
        editable_ids = revision_target_section_ids(result.report_markdown, audit)
        editable = [
            {"section_id": item.section_id, "title": item.title,
             "markdown": item.markdown}
            for item in sections if item.section_id in editable_ids
        ]
        adjacent_ids: set[str] = set()
        for index, item in enumerate(sections):
            if item.section_id in editable_ids:
                if index: adjacent_ids.add(sections[index - 1].section_id)
                if index + 1 < len(sections): adjacent_ids.add(sections[index + 1].section_id)
        adjacent_ids -= editable_ids
        adjacent = [
            {"section_id": item.section_id, "title": item.title,
             "markdown": item.markdown}
            for item in sections if item.section_id in adjacent_ids
        ]
        valid_segment_ids = {str(item["segment_id"]) for item in transcript}
        allowed_ids = audit_transcript_evidence_ids(audit, valid_segment_ids)
        await self._set_report_phase(version.id, "revising", worker_owner_id)
        revision_payload = staged.get("direct_report_v2_revisions")
        if isinstance(revision_payload, dict):
            revision = TargetedReportRevision.model_validate(revision_payload)
        else:
            request = self.composer.compose_targeted_report_revision(
                v1_title=result.title,
                section_outline=[{"section_id": item.section_id, "title": item.title} for item in sections],
                editable_sections=editable,
                adjacent_sections=adjacent,
                audit=audit,
                allowed_segment_ids=allowed_ids,
            )
            started = time.monotonic()
            try:
                raw_revision = await self.provider.generate(
                    version.provider_id, system=request.instructions,
                    user=request.user_data, model_id=version.model_id,
                    scene_id=request.scene_id, max_tokens=request.max_tokens,
                    timeout_seconds=request.timeout_seconds,
                    segment_count=request.segment_count,
                )
                revision = TargetedReportRevision.model_validate(json.loads(raw_revision))
                unknown_sections = {item.section_id for item in revision.revisions} - editable_ids
                if unknown_sections:
                    raise ValueError(f"revision changed non-editable sections: {sorted(unknown_sections)}")
            except Exception as exc:
                staged["direct_report_v2_revision_error"] = self._error_text(exc)
                metadata = metadata_from_audit(
                    report_version="v1", audit=audit,
                    score_scope="v1_full_audit",
                    audit_status="completed_v1_revision_failed",
                    degraded_reason=str(exc)[:1_000],
                )
                return await self._publish_report(
                    version, staged, result, metadata, v1_quality,
                    worker_owner_id, int((time.monotonic() - started) * 1_000),
                )
            staged["direct_report_v2_revisions"] = revision.model_dump(mode="json")
            await self._save_checkpoint(
                version.id, staged, worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )

        try:
            v2_markdown = apply_audited_revision(
                result.report_markdown, audit, revision, valid_segment_ids
            )
            diffs = build_section_diffs(
                result.report_markdown,
                v2_markdown,
                allowed_title_change_ids=audit_title_revision_section_ids(
                    result.report_markdown, audit
                ),
            )
            v2_result = MarkdownReportResult.from_markdown(v2_markdown)
            v2_quality = evaluate_direct_markdown_quality(
                v2_result.report_markdown, transcript_chars=len(markdown)
            )
        except Exception as exc:
            staged["direct_report_v2_revision_error"] = self._error_text(exc)
            metadata = metadata_from_audit(
                report_version="v1", audit=audit,
                score_scope="v1_full_audit",
                audit_status="completed_v1_revision_failed",
                degraded_reason=str(exc)[:1_000],
            )
            return await self._publish_report(
                version, staged, result, metadata, v1_quality,
                worker_owner_id, 0,
            )
        staged["direct_report_v2_markdown"] = v2_result.report_markdown
        staged["direct_report_v2_diffs"] = diffs

        final_payload = staged.get("direct_report_v2_final_audit")
        if isinstance(final_payload, dict):
            final_audit = ReportAudit.model_validate(final_payload)
        else:
            await self._set_report_phase(version.id, "auditing", worker_owner_id)
            request = self.composer.compose_revision_final_audit(
                v2_markdown=v2_result.report_markdown, section_diffs=diffs,
                v1_audit=audit, revision=revision,
            )
            started = time.monotonic()
            try:
                raw_final = await self.provider.generate(
                    version.provider_id, system=request.instructions,
                    user=request.user_data, model_id=version.model_id,
                    scene_id=request.scene_id, max_tokens=request.max_tokens,
                    timeout_seconds=request.timeout_seconds,
                    segment_count=request.segment_count,
                )
                final_audit = ReportAudit.model_validate(json.loads(raw_final))
                if final_audit.audit_mode != "revision_final_audit":
                    raise ValueError("final audit returned the wrong audit mode")
            except Exception as exc:
                staged["direct_report_v2_final_audit_error"] = self._error_text(exc)
                metadata = metadata_from_audit(
                    report_version="v2", audit=audit,
                    score_scope="v1_pre_revision",
                    audit_status="completed_v2_final_audit_degraded",
                    degraded_reason=str(exc)[:1_000],
                )
                return await self._publish_report(
                    version, staged, v2_result, metadata, v2_quality,
                    worker_owner_id, int((time.monotonic() - started) * 1_000),
                )
            staged["direct_report_v2_final_audit"] = final_audit.model_dump(mode="json")
            await self._save_checkpoint(
                version.id, staged, worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )

        return await self._publish_report(
            version, staged, v2_result,
            metadata_from_audit(
                report_version="v2", audit=final_audit,
                score_scope="v2_final_audit",
            ),
            v2_quality, worker_owner_id, 0,
        )

    @staticmethod
    def _error_text(exc: Exception) -> dict[str, str]:
        return {"type": type(exc).__name__, "message": str(exc)[:1_000]}

    async def _publish_report(
        self, version, staged, result: MarkdownReportResult,
        metadata: ReportQualityMetadata, deterministic_quality,
        worker_owner_id: str | None, duration_ms: int,
    ):
        await self._set_report_phase(version.id, "publishing", worker_owner_id)
        initial_audit_payload = staged.get("direct_report_v1_audit")
        initial_score = None
        if isinstance(initial_audit_payload, dict):
            scores = initial_audit_payload.get("scores")
            if isinstance(scores, dict) and isinstance(scores.get("total"), int):
                initial_score = scores["total"]
        final_markdown = append_report_metrics(
            result.report_markdown,
            initial_score=initial_score,
            final_score=metadata.quality_score,
            revised=metadata.report_version == "v2",
        )
        staged["direct_report_final_markdown"] = final_markdown
        staged["direct_report_markdown"] = final_markdown
        staged["direct_report_publication_metadata"] = metadata.as_dict()
        staged["direct_report_quality"] = {
            "passed": deterministic_quality.passed,
            "failures": list(deterministic_quality.failures),
            "report_chars": deterministic_quality.report_chars,
            "minimum_report_chars": deterministic_quality.minimum_report_chars,
            **metadata.as_dict(),
        }
        await self._save_checkpoint(
            version.id, staged, worker_owner_id, duration_ms=duration_ms
        )
        result = MarkdownReportResult(
            title=result.title, summary=result.summary,
            report_markdown=final_markdown,
            report_annotations=result.report_annotations,
            quality_metadata=metadata,
        )
        return await self.publisher.publish(
            version.id, result, [], worker_owner_id=worker_owner_id
        )

    async def _run_structured(self, version, staged, *, worker_owner_id):
        await self._set_report_phase(version.id, "generating", worker_owner_id)
        raw_document = staged.get("direct_report_document")
        checkpoint_mode = staged.get("direct_report_output_mode")
        if isinstance(raw_document, dict) and checkpoint_mode == "structured":
            document = DirectReportDocument.model_validate(raw_document)
        else:
            transcript, markdown, profile, goal_content = await self._inputs(version)
            request = self.composer.compose_direct_report(
                transcript_markdown=markdown,
                profile=profile,
                user_analysis_prompt=goal_content,
                segment_count=len(transcript),
            )
            started = time.monotonic()
            raw_response = await self.provider.generate(
                version.provider_id,
                system=request.instructions,
                user=request.user_data,
                model_id=version.model_id,
                scene_id=request.scene_id,
                max_tokens=request.max_tokens,
                timeout_seconds=request.timeout_seconds,
                segment_count=request.segment_count,
            )
            document = DirectReportDocument.model_validate(json.loads(raw_response))
            staged["direct_report_output_mode"] = "structured"
            staged["direct_report_document"] = document.model_dump(mode="json")
            await self._save_checkpoint(
                version.id,
                staged,
                worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )
        result = StructuredReportResult.from_document(document)
        await self._set_report_phase(version.id, "publishing", worker_owner_id)
        return await self.publisher.publish(
            version.id, result, [], worker_owner_id=worker_owner_id
        )

    async def _inputs(self, version):
        transcript = await self._transcript(version.source_job_id)
        markdown = build_full_transcript_markdown(transcript)
        snapshot = json.loads(version.prompt_snapshot_json or "{}")
        goal = snapshot.get("user-analysis-goal", {})
        goal_content = goal.get("content") if isinstance(goal, dict) else None
        if not isinstance(goal_content, str) or not goal_content.strip():
            goal_content = self.composer.default_user_analysis_goal()
        profile = json.loads(version.profile_snapshot_json or "[]")
        if not isinstance(profile, list):
            profile = []
        return transcript, markdown, profile, goal_content

    async def _version(self, version_id: str, worker_owner_id: str | None):
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        if version is None:
            raise LookupError(f"Unknown analysis version: {version_id}")
        if version.status != "running":
            raise ValueError(f"Analysis version is not running: {version.status}")
        if worker_owner_id is not None and version.worker_owner_id != worker_owner_id:
            raise LeaseLostError("Analysis worker lease was lost")
        return version

    async def _transcript(self, job_id: str) -> list[dict[str, object]]:
        async with self.database.session() as session:
            rows = list((await session.execute(
                select(Transcript, JobFile)
                .join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(
                    JobFile.job_id == job_id,
                    Transcript.risk_classified.is_(True),
                    Transcript.is_reliable.is_(True),
                )
                .order_by(JobFile.position, Transcript.segment_index)
            )).all())
        return [
            {
                "segment_id": f"seg_{file.position}_{row.segment_index}",
                "file_id": file.id,
                "file_name": file.original_name,
                "recording_started_at": file.recording_started_at,
                "timezone": file.timezone,
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "speaker_id": row.speaker_id or "unknown",
                "text": row.text,
            }
            for row, file in rows
        ]

    async def _save_checkpoint(
        self,
        version_id: str,
        staged: dict[str, object],
        worker_owner_id: str | None,
        *,
        duration_ms: int,
    ) -> None:
        diagnostics = getattr(self.provider, "request_diagnostics", [])
        latest = diagnostics[-1] if diagnostics else None
        metrics = {
            "model_duration_ms": duration_ms,
            "total_duration_ms": duration_ms,
            "input_tokens": sum(int(getattr(item, "input_tokens", 0)) for item in diagnostics),
            "output_tokens": sum(int(getattr(item, "output_tokens", 0)) for item in diagnostics),
            "model_call_count": max(1, len(diagnostics)),
            "web_search_performed": False,
            "web_search_degraded_reason": "单次报告主链路未启用联网核验。",
        }
        async with self.database.session() as session:
            statement = update(AnalysisVersion).where(
                AnalysisVersion.id == version_id,
                AnalysisVersion.status == "running",
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            saved = await session.execute(statement.values(
                staged_results_json=json.dumps(staged, ensure_ascii=False),
                pipeline_metrics_json=json.dumps(metrics, ensure_ascii=False),
            ))
            if int(saved.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            await session.commit()

    async def _set_report_phase(
        self, version_id: str, phase: str, worker_owner_id: str | None
    ) -> None:
        if phase not in {"generating", "auditing", "revising", "publishing"}:
            raise ValueError(f"Unsupported report phase: {phase}")
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is None or version.status != "running":
                raise LeaseLostError("Analysis worker lease was lost")
            if worker_owner_id is not None and version.worker_owner_id != worker_owner_id:
                raise LeaseLostError("Analysis worker lease was lost")
            try:
                checkpoints = json.loads(version.pipeline_checkpoints_json or "{}")
            except (TypeError, json.JSONDecodeError):
                checkpoints = {}
            checkpoints["report_phase"] = phase
            version.pipeline_checkpoints_json = json.dumps(
                checkpoints, ensure_ascii=False
            )
            await session.commit()
        emit_analysis_event(
            logging.getLogger("uvicorn.error"),
            "analysis.report.phase_changed",
            analysis_version_id=version_id,
            status=phase,
        )
