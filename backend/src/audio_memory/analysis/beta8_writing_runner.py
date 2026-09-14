from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re

from sqlalchemy import select, update

from audio_memory.analysis.beta8_writing_pipeline import WritingPipeline
from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStopped, WritingStore, digest
from audio_memory.analysis.pipeline_identity import BETA8_WRITING_PIPELINE_KIND, validate_version_pipeline_identity, is_current_pipeline_compatible
from audio_memory.analysis.runner import LeaseLostError
from audio_memory.models import AnalysisVersion, JobFile, Transcript


class Beta8WritingRunner:
    """Opt-in draft generation; deliberately has no publisher or auditor dependency."""

    def __init__(self, *, database, transport, generation_source, output_root: Path, write_boundary=None, enabled=True):
        self.database = database
        self.transport = transport
        self.generation_source = generation_source
        self.output_root = Path(output_root)
        self.write_boundary = write_boundary
        self.enabled = enabled
        self._lock = asyncio.Lock()

    async def run(self, version_id: str, worker_owner_id: str | None = None):
        async with self._lock:
            return await self._run(version_id, worker_owner_id)

    async def _version(self, version_id, owner):
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        if version is None or version.status != "running" or (owner is not None and version.worker_owner_id != owner):
            raise LeaseLostError("Writing version lease was lost")
        return version

    async def _run(self, version_id, owner):
        if not self.enabled:
            raise WritingStopped("Writing V1 is enabled only in development")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", version_id):
            raise ValueError("Invalid version id")
        version = await self._version(version_id, owner)
        identity = validate_version_pipeline_identity(version)
        if identity.pipeline_kind != BETA8_WRITING_PIPELINE_KIND:
            raise ValueError("Writing runner requires its own pipeline identity")
        if not is_current_pipeline_compatible(identity):
            raise WritingStopped("Writing prompt rules changed; prior authorization is invalid")
        async with self.database.session() as session:
            rows = (await session.execute(select(Transcript, JobFile).join(JobFile, JobFile.id == Transcript.job_file_id).where(JobFile.job_id == version.source_job_id, Transcript.risk_classified.is_(True), Transcript.is_reliable.is_(True)).order_by(JobFile.position, Transcript.segment_index))).all()
        segments = [{"segment_id": f"seg_{file.position}_{row.segment_index}", "source_file": file.id,
                     "file_id": file.id, "file_name": file.original_name,
                     "recording_started_at": file.recording_started_at, "timezone": file.timezone,
                     "start_ms": row.start_ms, "end_ms": row.end_ms, "speaker_id": row.speaker_id or "unknown", "text": row.text}
                    for row, file in rows]
        binding = {"version_id": version_id, "input_sha256": digest(segments), "pipeline_parameters_fingerprint": version.pipeline_parameters_fingerprint}
        root = self.output_root / version_id
        control = WritingStore(root, binding, write_boundary=self.write_boundary)
        request = dict(binding, limits=asdict(WritingLimits()), user_corrections=[], report_period="", search_credential_generation=None)
        control.write("execution-request.json", request)
        if not control.exists("authorization.json"):
            result = {"status": "waiting_for_authorization", "cards": [], "metrics": {"model_request_count": 0}, "published": False, "audit_performed": False}
        else:
            grant = control.read("authorization.json")
            if any(grant.get(key) != value for key, value in binding.items()):
                raise WritingStopped("authorization does not match version/input/model binding")
            limits = WritingLimits(**grant["limits"])
            if not limits.allow_paid or limits.max_requests < 1:
                raise WritingStopped("authorization budget is disabled")
            corrections = grant.get("user_corrections", [])
            if not isinstance(corrections, list) or not isinstance(grant.get("report_period"), str):
                raise ValueError("Invalid correction/report period input")
            async def fence():
                await self._version(version_id, owner)
                if not is_current_pipeline_compatible(identity):
                    raise WritingStopped("Writing prompt rules changed during execution")
                if await self.generation_source.credential_generation(version.provider_id) != version.credential_generation:
                    raise WritingStopped("Report credential generation changed")
                if identity.search_provider_id:
                    actual = await self.generation_source.credential_generation(identity.search_provider_id)
                    if actual != grant.get("search_credential_generation"):
                        raise WritingStopped("Search credential generation missing or changed")
                if self.write_boundary:
                    self.write_boundary.verify()
            await fence()
            pipeline = WritingPipeline(output_dir=root / "artifacts", transport=self.transport, limits=limits,
                provider_id=version.provider_id, model_id=version.model_id,
                search_provider_id=identity.search_provider_id, search_model_id=identity.search_model_id,
                credential_generation=version.credential_generation,
                search_credential_generation=grant.get("search_credential_generation"), before_dispatch=fence,
                write_boundary=self.write_boundary)
            result = await pipeline.run(segments, user_corrections=corrections, report_period=grant["report_period"])
        async with self.database.session() as session:
            staged = json.loads(version.staged_results_json or "{}")
            staged["beta8_writing_v1"] = dict(result, artifact_directory=str(root / "artifacts"))
            changed = await session.execute(update(AnalysisVersion).where(AnalysisVersion.id == version_id,
                AnalysisVersion.status == "running", AnalysisVersion.worker_owner_id == version.worker_owner_id).values(
                status="paused", error_code=None, worker_owner_id=None, lease_expires_at=None,
                staged_results_json=json.dumps(staged, ensure_ascii=False),
                pipeline_checkpoints_json=json.dumps({"report_phase": result["status"], "writing_only": True}),
                pipeline_metrics_json=json.dumps({"model_call_count": result["metrics"].get("model_request_count", 0),
                    "input_tokens": result["metrics"].get("input_tokens"), "output_tokens": result["metrics"].get("output_tokens"),
                    "final_audit_score": None})))
            if changed.rowcount != 1:
                raise LeaseLostError("Writing completion lease was lost")
            await session.commit()
        return result
