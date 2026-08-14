from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Protocol

from sqlalchemy import select, update

from audio_memory.analysis.full_transcript import build_full_transcript_markdown
from audio_memory.analysis.markdown_report import MarkdownReportResult
from audio_memory.analysis.direct_markdown_quality import evaluate_direct_markdown_quality
from audio_memory.analysis.direct_report_sections import (
    apply_section_revisions,
    replace_report_title,
    split_report_sections,
)
from audio_memory.analysis.direct_report_annotations import (
    parse_report_blocks,
    validate_annotations,
)
from audio_memory.analysis.direct_report_document import StructuredReportResult
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.direct_report_schema import DirectReportDocument
from audio_memory.prompts.direct_report_review_schema import DirectReportReview
from audio_memory.prompts.direct_report_annotation_schema import DirectReportAnnotations
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
        transcript, markdown, profile, goal_content = await self._inputs(version)
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
            result = MarkdownReportResult.from_markdown(raw_report)
            staged["direct_report_initial_markdown"] = result.report_markdown
            await self._save_checkpoint(
                version.id,
                staged,
                worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )
        else:
            result = MarkdownReportResult.from_markdown(raw_report)
        initial_quality = evaluate_direct_markdown_quality(
            result.report_markdown, transcript_chars=len(markdown)
        )

        review_payload = staged.get("direct_report_review")
        if isinstance(review_payload, dict):
            review = DirectReportReview.model_validate(review_payload)
        else:
            request = self.composer.compose_direct_report_review(
                transcript_markdown=markdown,
                profile=profile,
                user_analysis_prompt=goal_content,
                initial_report_markdown=result.report_markdown,
                sections=split_report_sections(result.report_markdown),
                gate_failures=initial_quality.failures,
                segment_count=len(transcript),
            )
            started = time.monotonic()
            raw_review = await self.provider.generate(
                version.provider_id,
                system=request.instructions,
                user=request.user_data,
                model_id=version.model_id,
                scene_id=request.scene_id,
                max_tokens=request.max_tokens,
                timeout_seconds=request.timeout_seconds,
                segment_count=request.segment_count,
            )
            review = DirectReportReview.model_validate(json.loads(raw_review))
            staged["direct_report_review"] = review.model_dump(mode="json")
            await self._save_checkpoint(
                version.id,
                staged,
                worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )

        material_issue_ids = {
            item.issue_id
            for item in review.issues
            if item.severity in {"critical", "major"}
        }
        resolved_issue_ids = {
            issue_id
            for revision in review.revised_sections
            for issue_id in revision.issues_resolved
        }
        unresolved = material_issue_ids - resolved_issue_ids
        if unresolved:
            raise ValueError(
                "direct report review left material issues unresolved: "
                + ", ".join(sorted(unresolved))
            )
        valid_segment_ids = {
            str(item["segment_id"])
            for item in transcript
            if isinstance(item.get("segment_id"), str)
        }
        final_markdown = apply_section_revisions(
            result.report_markdown,
            tuple(review.revised_sections),
            valid_segment_ids,
        )
        final_markdown = replace_report_title(final_markdown, review.revised_title)
        final_result = MarkdownReportResult.from_markdown(final_markdown)
        final_quality = evaluate_direct_markdown_quality(
            final_result.report_markdown, transcript_chars=len(markdown)
        )
        if not final_quality.passed:
            raise ValueError(
                "direct report quality gate failed after review: "
                + ", ".join(final_quality.failures)
            )
        staged["direct_report_section_revisions"] = [
            item.model_dump(mode="json") for item in review.revised_sections
        ]
        staged["direct_report_final_markdown"] = final_result.report_markdown
        staged["direct_report_markdown"] = final_result.report_markdown
        staged["direct_report_quality"] = {
            "passed": True,
            "failures": [],
            "report_chars": final_quality.report_chars,
            "minimum_report_chars": final_quality.minimum_report_chars,
            "reviewed": True,
            "revised_section_count": len(review.revised_sections),
        }
        await self._save_checkpoint(
            version.id, staged, worker_owner_id, duration_ms=0
        )

        blocks = parse_report_blocks(final_result.report_markdown)
        annotations = None
        annotation_payload = staged.get("direct_report_annotations")
        if isinstance(annotation_payload, dict):
            parsed = DirectReportAnnotations.model_validate(annotation_payload)
            annotations = validate_annotations(blocks, parsed)
        elif not staged.get("direct_report_annotation_degraded_reason"):
            request = self.composer.compose_direct_report_annotations(blocks=blocks)
            started = time.monotonic()
            try:
                raw_annotations = await self.provider.generate(
                    version.provider_id,
                    system=request.instructions,
                    user=request.user_data,
                    model_id=version.model_id,
                    scene_id=request.scene_id,
                    max_tokens=request.max_tokens,
                    timeout_seconds=request.timeout_seconds,
                    segment_count=request.segment_count,
                )
                parsed = DirectReportAnnotations.model_validate(
                    json.loads(raw_annotations)
                )
                annotations = validate_annotations(blocks, parsed)
                staged["direct_report_annotations"] = parsed.model_dump(mode="json")
            except Exception as exc:
                staged["direct_report_annotation_degraded_reason"] = (
                    f"{type(exc).__name__}: {exc}"
                )[:1_000]
            await self._save_checkpoint(
                version.id,
                staged,
                worker_owner_id,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )
        if annotations is not None:
            final_result = replace(
                final_result,
                report_annotations=tuple(
                    item.model_dump(mode="json") for item in annotations
                ),
            )
        return await self.publisher.publish(
            version.id, final_result, [], worker_owner_id=worker_owner_id
        )

    async def _run_structured(self, version, staged, *, worker_owner_id):
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
