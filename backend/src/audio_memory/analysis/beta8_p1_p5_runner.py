from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re

from sqlalchemy import select

from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
from audio_memory.analysis.beta8_p1_p5_publication import build_publication
from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStopped
from audio_memory.analysis.pipeline_identity import (
    BETA8_P1_P5_PIPELINE_KIND,
    is_current_pipeline_compatible,
    validate_version_pipeline_identity,
)
from audio_memory.analysis.pipeline_state import ModelCallMetric, PipelineMetrics
from audio_memory.analysis.runner import LeaseLostError
from audio_memory.models import AnalysisVersion, JobFile, Transcript


class Beta8P1P5Runner:
    """Run the first-result P1-P5 pipeline and publish only complete scored cards."""

    def __init__(
        self,
        *,
        database,
        transport,
        publisher,
        generation_source,
        output_root: Path,
        write_boundary=None,
        pipeline_factory=P1P5Pipeline,
    ) -> None:
        self.database = database
        self.transport = transport
        self.publisher = publisher
        self.generation_source = generation_source
        self.output_root = Path(output_root)
        self.write_boundary = write_boundary
        self.pipeline_factory = pipeline_factory
        self._lock = asyncio.Lock()

    async def run(self, version_id: str, worker_owner_id: str | None = None):
        async with self._lock:
            return await self._run(version_id, worker_owner_id)

    async def _version(self, version_id: str, owner: str | None) -> AnalysisVersion:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        if (
            version is None
            or version.status != "running"
            or owner is not None
            and version.worker_owner_id != owner
        ):
            raise LeaseLostError("P1-P5 analysis version lease was lost")
        return version

    @staticmethod
    def _report_period(rows: list[tuple[Transcript, JobFile]]) -> str:
        dates = []
        for _, file in rows:
            value = file.recording_started_at
            if not value:
                continue
            try:
                date = datetime.fromisoformat(value).date().isoformat()
            except ValueError:
                continue
            if date not in dates:
                dates.append(date)
        dates.sort()
        if not dates:
            return ""
        return dates[0] if len(dates) == 1 else f"{dates[0]} 至 {dates[-1]}"

    @staticmethod
    def _metrics(
        result: dict,
        *,
        search_degraded_reason: str | None,
        request_rows: list[dict] | None = None,
        run_id: str | None = None,
        provider_id: str = "deepseek",
    ) -> PipelineMetrics:
        source = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        input_tokens = source.get("input_tokens")
        output_tokens = source.get("output_tokens")
        search_calls = int(source.get("search_model_request_count") or 0)
        search_tool_calls = int(source.get("search_tool_call_count") or 0)
        search_input_tokens = source.get("search_input_tokens")
        search_output_tokens = source.get("search_output_tokens")
        rows = request_rows or []
        new_count = min(len(rows), int(result.get("new_request_count") or 0))
        historical_count = len(rows) - new_count
        current_rows = rows[historical_count:]

        def parsed(value: object) -> datetime | None:
            if not isinstance(value, str):
                return None
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None

        def endpoint(row: dict) -> datetime | None:
            return parsed(
                row.get("finished_at") or row.get("failed_at") or row.get("progress_at")
            )

        def duration(row: dict) -> int:
            start = parsed(row.get("started_at"))
            end = endpoint(row)
            if start is None or end is None:
                return 0
            return max(0, round((end - start).total_seconds() * 1000))

        def usage(row: dict, *names: str) -> int | None:
            response = row.get("response")
            values = response.get("usage") if isinstance(response, dict) else None
            if not isinstance(values, dict):
                return None
            return next((
                values[name] for name in names
                if isinstance(values.get(name), int)
                and not isinstance(values[name], bool) and values[name] >= 0
            ), None)

        def summed_usage(selected: list[dict], *names: str) -> int | None:
            values = [usage(row, *names) for row in selected]
            return sum(values) if all(value is not None for value in values) else None

        calls = []
        for index, row in enumerate(rows):
            is_current = index >= historical_count
            stage = str(row.get("stage") or "unknown")
            row_run_id = run_id if is_current and run_id else "historical"
            calls.append(ModelCallMetric(
                run_id=row_run_id,
                invocation_id=f"{row_run_id}-{index + 1}",
                attempt_index=1 if is_current and historical_count else 0,
                stage=stage,
                attempt_kind="resume" if is_current and historical_count else "normal",
                provider="kimi" if stage == "P3" else provider_id,
                model=str((row.get("payload") or {}).get("model") or "unknown"),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at") or row.get("failed_at"),
                input_tokens=usage(row, "prompt_tokens", "input_tokens"),
                output_tokens=usage(row, "completion_tokens", "output_tokens"),
                token_usage_unavailable_reason=(
                    None if usage(row, "prompt_tokens", "input_tokens") is not None
                    and usage(row, "completion_tokens", "output_tokens") is not None
                    else "Provider response did not expose complete token usage"
                ),
                duration_ms=duration(row),
                checkpoint_reused=False,
            ))

        stage_durations: dict[str, int] = {}
        for row in current_rows:
            stage = str(row.get("stage") or "unknown")
            stage_durations[stage] = stage_durations.get(stage, 0) + duration(row)
        if current_rows:
            stage_durations["all_scenes_v1"] = sum(
                duration(row) for row in current_rows if row.get("stage") != "P3"
            )
        current_starts = [parsed(row.get("started_at")) for row in current_rows]
        current_ends = [endpoint(row) for row in current_rows]
        current_starts = [value for value in current_starts if value is not None]
        current_ends = [value for value in current_ends if value is not None]
        run_started = min(current_starts) if current_starts else None
        run_finished = max(current_ends) if current_ends else None
        run_duration = (
            max(0, round((run_finished - run_started).total_seconds() * 1000))
            if run_started is not None and run_finished is not None else 0
        )
        run_model_duration = sum(duration(row) for row in current_rows)
        model_duration = sum(duration(row) for row in rows)
        reused_stages = tuple(dict.fromkeys(
            str(row["stage"]) for row in rows[:historical_count]
            if row.get("status") == "response_received" and row.get("stage")
        ))
        run_search_rows = [row for row in current_rows if row.get("stage") == "P3"]
        effective_count = len(rows) if rows else int(source.get("model_request_count") or 0)
        return PipelineMetrics(
            local_duration_ms=0,
            model_duration_ms=model_duration,
            total_duration_ms=model_duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_call_count=effective_count,
            token_usage_unavailable_reason=(
                "Provider response did not expose complete token usage"
                if input_tokens is None or output_tokens is None else None
            ),
            search_input_tokens=search_input_tokens if search_calls else 0,
            search_output_tokens=search_output_tokens if search_calls else 0,
            search_token_usage_unavailable_reason=(
                "Search provider response did not expose complete token usage"
                if search_calls and (
                    search_input_tokens is None or search_output_tokens is None
                ) else None
            ),
            search_model_response_count=search_calls,
            web_search_tool_call_count=search_tool_calls,
            web_search_performed=search_calls > 0,
            web_search_degraded_reason=search_degraded_reason,
            run_id=run_id if rows else None,
            run_started_at=run_started.isoformat() if run_started else None,
            run_finished_at=run_finished.isoformat() if run_finished else None,
            run_duration_ms=run_duration,
            run_model_duration_ms=run_model_duration,
            run_input_tokens=summed_usage(current_rows, "prompt_tokens", "input_tokens"),
            run_output_tokens=summed_usage(current_rows, "completion_tokens", "output_tokens"),
            new_model_call_count=new_count if rows else int(result.get("new_request_count") or 0),
            historical_model_call_count=historical_count,
            run_search_input_tokens=summed_usage(run_search_rows, "prompt_tokens", "input_tokens") if run_search_rows else 0,
            run_search_output_tokens=summed_usage(run_search_rows, "completion_tokens", "output_tokens") if run_search_rows else 0,
            run_search_model_response_count=len(run_search_rows),
            run_web_search_tool_call_count=0,
            stage_durations_ms=stage_durations,
            checkpoint_reused_stages=reused_stages,
            cost_unavailable_reason="Provider pricing was not recorded for this run",
            model_calls=tuple(calls),
        )

    async def _run(self, version_id: str, owner: str | None):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", version_id):
            raise ValueError("Invalid version id")
        version = await self._version(version_id, owner)
        identity = validate_version_pipeline_identity(version)
        if identity.pipeline_kind != BETA8_P1_P5_PIPELINE_KIND:
            raise ValueError("P1-P5 runner requires its own pipeline identity")
        if not is_current_pipeline_compatible(identity):
            raise WritingStopped("P1-P5 prompt rules changed before execution")

        async with self.database.session() as session:
            rows = list((await session.execute(
                select(Transcript, JobFile)
                .join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(
                    JobFile.job_id == version.source_job_id,
                    Transcript.risk_classified.is_(True),
                    Transcript.is_reliable.is_(True),
                )
                .order_by(JobFile.position, Transcript.segment_index)
            )).all())
        segments = [{
            "segment_id": f"seg_{file.position}_{row.segment_index}",
            "source_file": file.id,
            "file_id": file.id,
            "file_name": file.original_name,
            "recording_started_at": file.recording_started_at,
            "timezone": file.timezone,
            "start_ms": row.start_ms,
            "end_ms": row.end_ms,
            "speaker_id": row.speaker_id or "unknown",
            "text": row.text,
            "is_reliable": row.is_reliable,
        } for row, file in rows]
        if not segments:
            raise WritingStopped("P1-P5 requires at least one reliable transcript segment")

        search_generation = None
        if identity.search_provider_id:
            search_generation = await self.generation_source.credential_generation(
                identity.search_provider_id
            )

        async def fence() -> None:
            await self._version(version_id, owner)
            if not is_current_pipeline_compatible(identity):
                raise WritingStopped("P1-P5 prompt rules changed during execution")
            if await self.generation_source.credential_generation(
                version.provider_id
            ) != version.credential_generation:
                raise WritingStopped("Report credential generation changed")
            if identity.search_provider_id:
                actual = await self.generation_source.credential_generation(
                    identity.search_provider_id
                )
                if actual != search_generation:
                    raise WritingStopped("Search credential generation changed")
            if self.write_boundary:
                self.write_boundary.verify()

        await fence()
        artifact_dir = self.output_root / version_id / "artifacts"
        limits = WritingLimits(
            allow_paid=True,
            max_requests=96,
            max_input_tokens=160_000,
            max_total_input_tokens=12_000_000,
            max_output_tokens=96_000,
            max_search_output_tokens=24_000,
            max_total_output_tokens=2_000_000,
            core_budget_bytes=80_000,
            max_research_tasks=6,
            source_limit=5,
        )
        pipeline = self.pipeline_factory(
            output_dir=artifact_dir,
            transport=self.transport,
            limits=limits,
            provider_id=version.provider_id,
            model_id=version.model_id,
            search_provider_id=identity.search_provider_id,
            search_model_id=identity.search_model_id,
            credential_generation=version.credential_generation,
            search_credential_generation=search_generation,
            before_dispatch=fence,
            write_boundary=self.write_boundary,
            explicit_retry_generation=(
                json.loads(version.pipeline_checkpoints_json or "{}").get(
                    "explicit_retry_generation", 0
                )
            ),
        )
        result = await pipeline.run(
            segments,
            user_corrections=[],
            report_period=self._report_period(rows),
        )
        if result.get("status") != "scored_v1":
            raise WritingStopped(
                "P1-P5 result is not scored_v1; incomplete reports are not published"
            )
        publication = build_publication(result, artifact_dir=artifact_dir)
        retry_generation = int(
            json.loads(version.pipeline_checkpoints_json or "{}").get(
                "explicit_retry_generation", 0
            )
        )
        store = getattr(pipeline, "store", None)
        request_rows = store.requests() if store is not None else None
        metrics = self._metrics(
            result,
            search_degraded_reason=publication.bundle.search_degraded_reason,
            request_rows=request_rows,
            run_id=(
                f"{version_id}-retry-{retry_generation}"
                if retry_generation else version_id
            ),
            provider_id=version.provider_id,
        )
        async with self.database.session() as session:
            stored = await session.get(AnalysisVersion, version_id)
            if (
                stored is None
                or stored.status != "running"
                or owner is not None
                and stored.worker_owner_id != owner
            ):
                raise LeaseLostError("P1-P5 analysis version lease was lost")
            staged = json.loads(stored.staged_results_json or "{}")
            staged[BETA8_P1_P5_PIPELINE_KIND] = {
                "status": result["status"],
                "rubric_version": result.get("rubric_version"),
                "card_count": len(result["cards"]),
                "artifact_directory": str(artifact_dir),
            }
            stored.staged_results_json = json.dumps(staged, ensure_ascii=False)
            stored.pipeline_metrics_json = metrics.model_dump_json()
            await session.commit()
        await fence()
        return await self.publisher.publish_beta8(
            version_id,
            publication.bundle,
            worker_owner_id=owner,
            card_assessments=publication.card_assessments,
        )
