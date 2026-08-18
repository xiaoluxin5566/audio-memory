from __future__ import annotations

import asyncio
import json
import logging
from hashlib import sha256
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    ReanalysisBatch,
    ReanalysisItem,
)
from audio_memory.prompts.composer import PromptComposer
from audio_memory.analysis.pipeline_state import PipelineMetrics
from audio_memory.reanalysis.preview import (
    current_fixed_rule_hashes,
    transcript_fingerprint_from_session,
)


NEW_UPLOAD_PRIORITY = 0
HISTORY_REANALYSIS_PRIORITY = 10
logger = logging.getLogger("uvicorn.error")
_PAUSED_HISTORY_STATES = {
    "stopped",
    "stopping",
    "cancelled",
    "paused",
    "paused_credential_changed",
    "paused_error",
    "paused_rules_changed",
    "credential_changed",
}
_LEASE_SECONDS = 30
_HEARTBEAT_SECONDS = 10


class AlreadyRunningError(RuntimeError):
    pass


class ReanalysisSnapshotChangedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    source_job_id: str
    source_batch_id: str | None
    provider_id: str
    model_id: str
    credential_generation: int
    prompt_snapshot: dict[str, object]
    profile_snapshot: list[dict[str, object]]
    priority: int
    event_map_json: str | None = None
    event_map_hash: str | None = None


class VersionRunner(Protocol):
    async def run(self, version_id: str, worker_owner_id: str): ...


class AnalysisTaskCoordinator:
    """SQLite-backed authority for the single global remote-model worker."""

    def __init__(
        self,
        database: Database,
        *,
        reclaim_foreign_on_initialize: bool = False,
        on_upload_started: Callable[[str], Awaitable[None]] | None = None,
        on_upload_finished: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.database = database
        self.owner_id = str(uuid4())
        self._condition = asyncio.Condition()
        self._maintenance_lock = asyncio.Lock()
        self._profile_retry_generation = 0
        self._profile_retries_active = 0
        self._initialized = False
        self._worker: asyncio.Task[None] | None = None
        self._current_upload_job_id: str | None = None
        self._closed = False
        self._reclaim_foreign_on_initialize = reclaim_foreign_on_initialize
        self._on_upload_started = on_upload_started
        self._on_upload_finished = on_upload_finished

    async def initialize(self) -> None:
        async with self._condition:
            if self._initialized:
                return
            async with self.database.session() as session:
                now = datetime.now(UTC).isoformat()
                reclaimable = AnalysisVersion.status == "running"
                if not self._reclaim_foreign_on_initialize:
                    reclaimable = and_(
                        reclaimable,
                        or_(
                            AnalysisVersion.lease_expires_at.is_(None),
                            AnalysisVersion.lease_expires_at < now,
                        ),
                    )
                expired_versions = select(AnalysisVersion.id).where(
                    reclaimable
                )
                await session.execute(
                    update(ReanalysisItem)
                    .where(
                        ReanalysisItem.analysis_version_id.in_(expired_versions),
                        ReanalysisItem.status == "running",
                    )
                    .values(status="pending")
                )
                await session.execute(
                    update(AnalysisVersion)
                    .where(reclaimable)
                    .values(
                        status="pending",
                        error_code=None,
                        worker_owner_id=None,
                        lease_expires_at=None,
                    )
                )
                await session.commit()
            self._initialized = True
            self._condition.notify_all()

    async def submit_new_upload(self, request: AnalysisRequest) -> str:
        if request.source_batch_id is not None:
            raise ValueError("A not-yet-published upload cannot have source_batch_id")
        if request.priority != NEW_UPLOAD_PRIORITY:
            raise ValueError("New uploads must use priority 0")
        return await self._submit(request)

    async def submit_reanalysis(self, request: AnalysisRequest) -> str:
        if request.source_batch_id is None:
            raise ValueError("History reanalysis requires source_batch_id")
        if request.priority != HISTORY_REANALYSIS_PRIORITY:
            raise ValueError("History reanalysis must use priority 10")
        return await self._submit(request)

    async def retry_failed_upload_in_place(
        self,
        *,
        source_job_id: str,
        provider_id: str,
        model_id: str,
        credential_generation: int,
    ) -> str | None:
        """Requeue compatible failed or legacy unaudited work in place."""
        await self.initialize()
        async with self._condition:
            async with self.maintenance_guard():
                async with self.database.session() as session:
                    await session.execute(text("BEGIN IMMEDIATE"))
                    version = await session.scalar(
                        select(AnalysisVersion)
                        .where(
                            AnalysisVersion.source_job_id == source_job_id,
                            AnalysisVersion.reanalysis_batch_id.is_(None),
                            AnalysisVersion.status.in_(("failed", "completed")),
                        )
                        .order_by(AnalysisVersion.created_at.desc())
                        .limit(1)
                    )
                    if version is None:
                        await session.rollback()
                        return None
                    if version.status == "completed":
                        try:
                            staged = json.loads(version.staged_results_json or "{}")
                        except (TypeError, json.JSONDecodeError):
                            staged = {}
                        metadata = staged.get("direct_report_publication_metadata")
                        if not (
                            isinstance(metadata, dict)
                            and metadata.get("audit_status")
                            == "completed_unaudited"
                            and isinstance(
                                staged.get("direct_report_v1_markdown"), str
                            )
                        ):
                            await session.rollback()
                            return None
                    if (
                        version.provider_id != provider_id
                        or version.model_id != model_id
                        or version.credential_generation != credential_generation
                        or version.fixed_rules_hash != PromptComposer.fixed_rules_hash()
                    ):
                        await session.rollback()
                        return None
                    version.status = "pending"
                    version.error_code = None
                    version.worker_owner_id = None
                    version.lease_expires_at = None
                    version.completed_at = None
                    job = await session.get(AnalysisJob, source_job_id)
                    if job is None:
                        await session.rollback()
                        return None
                    job.stage = "analyzing"
                    job.error_code = None
                    await session.commit()
            self._condition.notify_all()
        return version.id

    @asynccontextmanager
    async def maintenance_guard(self):
        """Fence queue mutations while history storage is maintained."""
        async with self._maintenance_lock:
            yield

    @asynccontextmanager
    async def profile_retry_guard(self):
        """Mark and fence a profile-only rebuild from history deletion."""
        async with self._maintenance_lock:
            self._profile_retry_generation += 1
            self._profile_retries_active += 1
            try:
                yield
            finally:
                self._profile_retries_active -= 1

    @asynccontextmanager
    async def history_cleanup_guard(self):
        """Fence cleanup and report a profile rebuild that raced it."""
        observed_generation = self._profile_retry_generation
        observed_active = self._profile_retries_active > 0
        async with self._maintenance_lock:
            profile_retry_raced = (
                observed_active
                or observed_generation != self._profile_retry_generation
            )
            yield profile_retry_raced

    async def _submit(self, request: AnalysisRequest) -> str:
        await self.initialize()
        if not request.source_job_id.strip():
            raise ValueError("source_job_id must not be empty")
        prompt_json = json.dumps(
            request.prompt_snapshot, ensure_ascii=False, sort_keys=True
        )
        profile_json = json.dumps(
            request.profile_snapshot, ensure_ascii=False, sort_keys=True
        )
        fixed_rules_hash = PromptComposer.fixed_rules_hash()
        prompt_manifest = [
            {
                "role": item["role"],
                "files": item["files"],
                "sha256": item["sha256"],
            }
            for item in PromptComposer.final_report_prompt_manifest()
        ]
        pipeline_parameters = {
            "provider_id": request.provider_id,
            "model_id": request.model_id,
            "credential_generation": request.credential_generation,
            "fixed_rules_hash": fixed_rules_hash,
            "prompt_manifest": prompt_manifest,
        }
        pipeline_parameters_json = json.dumps(
            pipeline_parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        pipeline_parameters_fingerprint = sha256(
            pipeline_parameters_json.encode("utf-8")
        ).hexdigest()
        version_id = str(uuid4())
        async with self._condition:
            async with self.maintenance_guard():
                try:
                    async with self.database.session() as session:
                        await session.execute(text("BEGIN IMMEDIATE"))
                        existing = await session.scalar(
                            select(AnalysisVersion.id).where(
                                AnalysisVersion.source_job_id
                                == request.source_job_id,
                                AnalysisVersion.status.in_(("pending", "running")),
                            )
                        )
                        if existing is not None:
                            raise AlreadyRunningError(
                                "Analysis is already pending or running for "
                                f"{request.source_job_id}"
                            )
                        reanalysis_item = None
                        if request.source_batch_id is not None:
                            ownership = (
                                await session.execute(
                                    select(ReanalysisItem, ReanalysisBatch, Batch)
                                    .join(
                                        ReanalysisBatch,
                                        ReanalysisBatch.id
                                        == ReanalysisItem.reanalysis_batch_id,
                                    )
                                    .join(
                                        Batch,
                                        Batch.id == ReanalysisItem.source_batch_id,
                                    )
                                    .where(
                                        ReanalysisItem.source_batch_id
                                        == request.source_batch_id,
                                        ReanalysisItem.status == "pending",
                                        ReanalysisBatch.status.in_(
                                            ("pending", "running")
                                        ),
                                    )
                                    .order_by(ReanalysisItem.created_at.desc())
                                    .limit(1)
                                )
                            ).first()
                            if ownership is None:
                                raise ValueError(
                                    "History reanalysis requires an active owning "
                                    "item and batch"
                                )
                            reanalysis_item, owning_run, source_batch = ownership
                            if source_batch.job_id != request.source_job_id:
                                raise ValueError(
                                    "History source batch does not belong to source job"
                                )
                            if (
                                owning_run.provider_id != request.provider_id
                                or owning_run.model_id != request.model_id
                                or owning_run.credential_generation
                                != request.credential_generation
                            ):
                                raise ValueError(
                                    "History request does not match owning batch snapshot"
                                )
                            owning_prompts = json.loads(
                                owning_run.prompt_snapshot_json
                            )
                            metadata = owning_prompts.get("_reanalysis", {})
                            if (
                                isinstance(metadata, dict)
                                and "fixed_rule_hashes" in metadata
                                and "transcript_fingerprints" in metadata
                            ):
                                expected_transcript = metadata.get(
                                    "transcript_fingerprints", {}
                                ).get(source_batch.id)
                                current_transcript = (
                                    await transcript_fingerprint_from_session(
                                        session, source_batch.job_id
                                    )
                                )
                                compatibility_error = None
                                if metadata.get(
                                    "fixed_rule_hashes"
                                ) != current_fixed_rule_hashes():
                                    compatibility_error = "analysis_schema_changed"
                                elif expected_transcript != current_transcript:
                                    compatibility_error = "transcript_changed"
                                if compatibility_error is not None:
                                    owning_run.status = "paused"
                                    reanalysis_item.error_code = compatibility_error
                                    await session.commit()
                                    raise ReanalysisSnapshotChangedError(
                                        "History snapshot changed before queue insertion"
                                    )
                            if (
                                owning_prompts != request.prompt_snapshot
                                or json.loads(owning_run.profile_snapshot_json)
                                != request.profile_snapshot
                                or owning_run.fixed_rules_hash != fixed_rules_hash
                            ):
                                raise ValueError(
                                    "History request does not match owning batch snapshot"
                                )
                        version = AnalysisVersion(
                            id=version_id,
                            source_job_id=request.source_job_id,
                            batch_id=request.source_batch_id,
                            provider_id=request.provider_id,
                            model_id=request.model_id,
                            credential_generation=request.credential_generation,
                            prompt_snapshot_json=prompt_json,
                            profile_snapshot_json=profile_json,
                            fixed_rules_hash=fixed_rules_hash,
                            event_map_json=request.event_map_json,
                            event_map_hash=request.event_map_hash,
                            staged_results_json="{}",
                            pipeline_parameters_json=pipeline_parameters_json,
                            pipeline_parameters_fingerprint=(
                                pipeline_parameters_fingerprint
                            ),
                            pipeline_checkpoints_json="{}",
                            pipeline_metrics_json=PipelineMetrics().model_dump_json(),
                            priority=request.priority,
                            status="pending",
                            reanalysis_batch_id=(
                                reanalysis_item.reanalysis_batch_id
                                if reanalysis_item is not None
                                else None
                            ),
                        )
                        session.add(version)
                        if request.source_batch_id is None:
                            job = await session.get(
                                AnalysisJob, request.source_job_id
                            )
                            if job is None:
                                raise LookupError(
                                    f"Unknown analysis job: {request.source_job_id}"
                                )
                            job.stage = "analyzing"
                            job.error_code = None
                        if reanalysis_item is not None:
                            reanalysis_item.analysis_version_id = version_id
                        await session.commit()
                except IntegrityError as exc:
                    raise AlreadyRunningError(
                        "Analysis is already pending or running for "
                        f"{request.source_job_id}"
                    ) from exc
            self._condition.notify_all()
        return version_id

    async def next_request(self) -> AnalysisRequest:
        _version_id, request = await self._claim_next()
        return request

    async def _claim_next(self) -> tuple[str, AnalysisRequest]:
        await self.initialize()
        while True:
            async with self._condition:
                async with self.maintenance_guard():
                    async with self.database.session() as session:
                        row = await session.scalar(
                            select(AnalysisVersion)
                            .outerjoin(
                                ReanalysisBatch,
                                ReanalysisBatch.id
                                == AnalysisVersion.reanalysis_batch_id,
                            )
                            .where(
                                AnalysisVersion.status == "pending",
                                or_(
                                    AnalysisVersion.reanalysis_batch_id.is_(None),
                                    ReanalysisBatch.status.not_in(
                                        _PAUSED_HISTORY_STATES
                                    ),
                                ),
                            )
                            .order_by(
                                AnalysisVersion.priority,
                                AnalysisVersion.created_at,
                                AnalysisVersion.id,
                            )
                            .limit(1)
                        )
                        if row is not None:
                            claimed = await session.execute(
                                update(AnalysisVersion)
                                .where(
                                    AnalysisVersion.id == row.id,
                                    AnalysisVersion.status == "pending",
                                )
                                .values(
                                    status="running",
                                    worker_owner_id=self.owner_id,
                                    lease_expires_at=self._lease_deadline(),
                                )
                            )
                            if int(claimed.rowcount) != 1:
                                await session.rollback()
                                continue
                            if row.reanalysis_batch_id is not None:
                                item = await session.scalar(
                                    select(ReanalysisItem).where(
                                        ReanalysisItem.analysis_version_id == row.id
                                    )
                                )
                                if item is not None:
                                    item.status = "running"
                            await session.commit()
                            request = self._request_from_version(row)
                            return row.id, request
                await self._condition.wait()

    async def start(self, runner: VersionRunner) -> None:
        await self.initialize()
        if self._worker is None or self._worker.done():
            self._closed = False
            self._worker = asyncio.create_task(self._work(runner))

    async def cancel_new_upload(self, source_job_id: str) -> bool:
        """Discard unpublished versions when no report worker owns the job."""
        await self.initialize()
        async with self._condition:
            if self._current_upload_job_id == source_job_id:
                raise AlreadyRunningError("Analysis publication is still running")
            async with self.maintenance_guard():
                async with self.database.session() as session:
                    removed = await session.execute(
                        delete(AnalysisVersion).where(
                            AnalysisVersion.source_job_id == source_job_id,
                            AnalysisVersion.batch_id.is_(None),
                        )
                    )
                    await session.commit()
            self._condition.notify_all()
        return int(removed.rowcount) > 0

    async def close(self) -> None:
        self._closed = True
        async with self._condition:
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        async with self.database.session() as session:
            owned_versions = select(AnalysisVersion.id).where(
                AnalysisVersion.status == "running",
                AnalysisVersion.worker_owner_id == self.owner_id,
            )
            await session.execute(
                update(ReanalysisItem)
                .where(
                    ReanalysisItem.analysis_version_id.in_(owned_versions),
                    ReanalysisItem.status == "running",
                )
                .values(status="pending")
            )
            await session.execute(
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.status == "running",
                    AnalysisVersion.worker_owner_id == self.owner_id,
                )
                .values(
                    status="pending",
                    worker_owner_id=None,
                    lease_expires_at=None,
                )
            )
            await session.commit()

    async def _work(self, runner: VersionRunner) -> None:
        while not self._closed:
            version_id, request = await self._claim_next()
            self._current_upload_job_id = (
                request.source_job_id if request.source_batch_id is None else None
            )
            if request.source_batch_id is None and self._on_upload_started is not None:
                try:
                    await self._on_upload_started(request.source_job_id)
                except Exception:
                    logger.exception(
                        "Upload runtime setup failed job_id=%s",
                        request.source_job_id,
                    )
            heartbeat = asyncio.create_task(self._heartbeat(version_id))
            try:
                await runner.run(version_id, self.owner_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Analysis task failed version_id=%s without a typed provider error",
                    version_id,
                )
                # AnalysisRunner normally persists its typed failure first. This
                # fenced fallback only handles exceptions that leave the version
                # running, so it cannot overwrite a concrete runner error code.
                async with self.database.session() as session:
                    await session.execute(
                        update(AnalysisVersion)
                        .where(
                            AnalysisVersion.id == version_id,
                            AnalysisVersion.status == "running",
                            AnalysisVersion.worker_owner_id == self.owner_id,
                        )
                        .values(
                            status="failed",
                            error_code="model_analysis_failed",
                        )
                    )
                    await session.commit()
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                async with self.database.session() as session:
                    await session.execute(
                        update(AnalysisVersion)
                        .where(
                            AnalysisVersion.id == version_id,
                            AnalysisVersion.worker_owner_id == self.owner_id,
                            AnalysisVersion.status != "running",
                        )
                        .values(worker_owner_id=None, lease_expires_at=None)
                    )
                    await session.commit()
                if (
                    request.source_batch_id is None
                    and self._on_upload_finished is not None
                ):
                    try:
                        await self._on_upload_finished(request.source_job_id)
                    except Exception:
                        logger.exception(
                            "Upload runtime cleanup failed job_id=%s",
                            request.source_job_id,
                        )
                self._current_upload_job_id = None

    async def _heartbeat(self, version_id: str) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            async with self.database.session() as session:
                renewed = await session.execute(
                    update(AnalysisVersion)
                    .where(
                        AnalysisVersion.id == version_id,
                        AnalysisVersion.status == "running",
                        AnalysisVersion.worker_owner_id == self.owner_id,
                    )
                    .values(lease_expires_at=self._lease_deadline())
                )
                await session.commit()
                if int(renewed.rowcount) != 1:
                    return

    @staticmethod
    def _lease_deadline() -> str:
        return (datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS)).isoformat()

    @staticmethod
    def _request_from_version(version: AnalysisVersion) -> AnalysisRequest:
        return AnalysisRequest(
            source_job_id=version.source_job_id,
            source_batch_id=version.batch_id,
            provider_id=version.provider_id,
            model_id=version.model_id,
            credential_generation=version.credential_generation,
            prompt_snapshot=json.loads(version.prompt_snapshot_json),
            profile_snapshot=json.loads(version.profile_snapshot_json),
            priority=version.priority,
            event_map_json=version.event_map_json,
            event_map_hash=version.event_map_hash,
        )
