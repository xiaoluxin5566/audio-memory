from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    ReanalysisBatch,
    ReanalysisItem,
)
from audio_memory.prompts.composer import PromptComposer


NEW_UPLOAD_PRIORITY = 0
HISTORY_REANALYSIS_PRIORITY = 10
_PAUSED_HISTORY_STATES = {
    "stopped",
    "cancelled",
    "paused",
    "paused_credential_changed",
    "credential_changed",
}


class AlreadyRunningError(RuntimeError):
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


class VersionRunner(Protocol):
    async def run(self, version_id: str): ...


class AnalysisTaskCoordinator:
    """SQLite-backed authority for the single global remote-model worker."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._condition = asyncio.Condition()
        self._initialized = False
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    async def initialize(self) -> None:
        async with self._condition:
            if self._initialized:
                return
            async with self.database.session() as session:
                await session.execute(
                    update(AnalysisVersion)
                    .where(AnalysisVersion.status == "running")
                    .values(status="pending", error_code=None)
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
        if request.priority != HISTORY_REANALYSIS_PRIORITY:
            raise ValueError("History reanalysis must use priority 10")
        return await self._submit(request)

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
        version_id = str(uuid4())
        async with self._condition:
            try:
                async with self.database.session() as session:
                    existing = await session.scalar(
                        select(AnalysisVersion.id).where(
                            AnalysisVersion.source_job_id == request.source_job_id,
                            AnalysisVersion.status.in_(("pending", "running")),
                        )
                    )
                    if existing is not None:
                        raise AlreadyRunningError(
                            f"Analysis is already pending or running for {request.source_job_id}"
                        )
                    reanalysis_item = None
                    if request.source_batch_id is not None:
                        reanalysis_item = await session.scalar(
                            select(ReanalysisItem)
                            .where(
                                ReanalysisItem.source_batch_id
                                == request.source_batch_id,
                                ReanalysisItem.status.in_(("pending", "running")),
                            )
                            .order_by(ReanalysisItem.created_at.desc())
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
                        fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                        staged_results_json="{}",
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
                        job = await session.get(AnalysisJob, request.source_job_id)
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
                    f"Analysis is already pending or running for {request.source_job_id}"
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
                async with self.database.session() as session:
                    row = await session.scalar(
                        select(AnalysisVersion)
                        .outerjoin(
                            ReanalysisBatch,
                            ReanalysisBatch.id == AnalysisVersion.reanalysis_batch_id,
                        )
                        .where(
                            AnalysisVersion.status == "pending",
                            or_(
                                AnalysisVersion.reanalysis_batch_id.is_(None),
                                ReanalysisBatch.status.not_in(_PAUSED_HISTORY_STATES),
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
                            .values(status="running")
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

    async def close(self) -> None:
        self._closed = True
        async with self._condition:
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def _work(self, runner: VersionRunner) -> None:
        while not self._closed:
            version_id, _request = await self._claim_next()
            try:
                await runner.run(version_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                async with self.database.session() as session:
                    version = await session.get(AnalysisVersion, version_id)
                    if version is not None and version.status == "running":
                        version.status = "failed"
                        version.error_code = "model_analysis_failed"
                        await session.commit()

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
        )
