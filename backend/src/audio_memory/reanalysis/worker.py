from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager

from pydantic import ValidationError
from sqlalchemy import select, update

from audio_memory.analysis.task_coordinator import (
    AlreadyRunningError,
    AnalysisRequest,
    HISTORY_REANALYSIS_PRIORITY,
    ReanalysisSnapshotChangedError,
)
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisVersion,
    Batch,
    JobFile,
    ReanalysisBatch,
    ReanalysisItem,
    Transcript,
)
from audio_memory.prompts.event_schema import EventMap
from audio_memory.reanalysis.preview import (
    current_fixed_rule_hashes,
    transcript_fingerprint,
)


logger = logging.getLogger(__name__)


class ReanalysisWorker:
    """Durable one-item-at-a-time feeder for the global model coordinator."""

    def __init__(
        self,
        *,
        database: Database,
        task_coordinator,
        publisher,
        provider_coordinator=None,
        poll_interval: float = 0.1,
    ) -> None:
        self.database = database
        self.task_coordinator = task_coordinator
        self.publisher = publisher
        self.provider_coordinator = provider_coordinator
        self.poll_interval = poll_interval
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._tick_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.recover()
        if self._task is None or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def notify(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.tick()
            except Exception:
                logger.exception("History reanalysis worker tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass

    async def recover(self) -> None:
        """The instance lock proves prior in-process worker owners are dead."""
        async with self.database.session() as session:
            running_versions = select(AnalysisVersion.id).where(
                AnalysisVersion.reanalysis_batch_id.is_not(None),
                AnalysisVersion.status == "running",
            )
            await session.execute(
                update(ReanalysisItem)
                .where(
                    ReanalysisItem.analysis_version_id.in_(running_versions),
                    ReanalysisItem.status == "running",
                )
                .values(status="pending")
            )
            await session.execute(
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.reanalysis_batch_id.is_not(None),
                    AnalysisVersion.status == "running",
                )
                .values(
                    status="pending",
                    worker_owner_id=None,
                    lease_expires_at=None,
                )
            )
            stale_items = list(
                await session.scalars(
                    select(ReanalysisItem).where(ReanalysisItem.status == "running")
                )
            )
            for item in stale_items:
                version = (
                    await session.get(AnalysisVersion, item.analysis_version_id)
                    if item.analysis_version_id is not None
                    else None
                )
                if version is None or version.status in {
                    "pending",
                    "credential_changed",
                    "fixed_rules_changed",
                    "provider_paused",
                }:
                    item.status = "pending"
                elif version.status == "completed":
                    item.status = "succeeded"
                    item.error_code = None
                elif version.status in {"failed", "stopped"}:
                    item.status = version.status
                    item.error_code = version.error_code
            await session.commit()

    async def tick(self) -> None:
        async with self._tick_lock:
            async with self.database.session() as session:
                batch = await session.scalar(
                    select(ReanalysisBatch)
                    .where(
                        ReanalysisBatch.status.in_(
                            ("pending", "running", "stopping")
                        )
                    )
                    .order_by(ReanalysisBatch.created_at, ReanalysisBatch.id)
                    .limit(1)
                )
                if batch is None:
                    return
                batch_id = batch.id
                status = batch.status

            if status == "stopping":
                await self._finish_stop(batch_id)
                return
            if self.provider_coordinator is not None:
                provider_state = self.provider_coordinator.state(batch.provider_id)
                if getattr(provider_state.state, "value", provider_state.state) != "available":
                    async with self.database.session() as session:
                        await session.execute(
                            update(ReanalysisBatch)
                            .where(ReanalysisBatch.id == batch_id)
                            .values(status="paused")
                        )
                        await session.commit()
                    return
            if await self._has_active_version(batch_id):
                await self._set_running(batch_id)
                return

            item = await self._next_pending_item(batch_id)
            if item is None:
                await self._finish_content(batch_id)
                return
            prompt_snapshot = json.loads(batch.prompt_snapshot_json)
            metadata = prompt_snapshot.get("_reanalysis", {})
            fixed_rule_hashes = metadata.get("fixed_rule_hashes")
            if fixed_rule_hashes != current_fixed_rule_hashes():
                await self._pause_snapshot_changed(
                    batch_id, item.id, "analysis_schema_changed"
                )
                return
            transcript_fingerprints = metadata.get("transcript_fingerprints", {})
            expected_transcript_hash = transcript_fingerprints.get(
                item.source_batch_id
            )
            async with self.database.session() as session:
                fingerprint_source = await session.get(Batch, item.source_batch_id)
            if fingerprint_source is None:
                await self._pause_snapshot_changed(
                    batch_id, item.id, "transcript_changed"
                )
                return
            current_transcript_hash = await transcript_fingerprint(
                self.database, fingerprint_source.job_id
            )
            if expected_transcript_hash != current_transcript_hash:
                await self._pause_snapshot_changed(
                    batch_id, item.id, "transcript_changed"
                )
                return
            event_json, event_hash = await self._compatible_event_map(
                item.source_batch_id,
                batch.fixed_rules_hash,
                fixed_rule_hashes,
                expected_transcript_hash,
            )
            async with self.database.session() as session:
                source = await session.get(Batch, item.source_batch_id)
                owner = await session.get(ReanalysisBatch, batch_id)
                if source is None or owner is None or owner.status not in {
                    "pending",
                    "running",
                }:
                    return
                request = AnalysisRequest(
                    source_job_id=source.job_id,
                    source_batch_id=source.id,
                    provider_id=owner.provider_id,
                    model_id=owner.model_id,
                    credential_generation=owner.credential_generation,
                    prompt_snapshot=json.loads(owner.prompt_snapshot_json),
                    profile_snapshot=json.loads(owner.profile_snapshot_json),
                    priority=HISTORY_REANALYSIS_PRIORITY,
                    event_map_json=event_json,
                    event_map_hash=event_hash,
                )
            try:
                await self.task_coordinator.submit_reanalysis(request)
            except AlreadyRunningError:
                # A normal retry/new-upload path owns the source. Priority zero
                # remains authoritative; retry this history item later.
                return
            except ReanalysisSnapshotChangedError:
                # The coordinator persisted the pause and item diagnostic in
                # the same transaction that refused queue insertion.
                return
            await self._set_running(batch_id)

    async def _finish_stop(self, batch_id: str) -> None:
        async with self.database.session() as session:
            running = await session.scalar(
                select(AnalysisVersion.id)
                .where(
                    AnalysisVersion.reanalysis_batch_id == batch_id,
                    AnalysisVersion.status == "running",
                )
                .limit(1)
            )
            if running is not None:
                return
            await session.execute(
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.reanalysis_batch_id == batch_id,
                    AnalysisVersion.status == "pending",
                )
                .values(status="stopped", error_code="stopped")
            )
            await session.execute(
                update(ReanalysisItem)
                .where(
                    ReanalysisItem.reanalysis_batch_id == batch_id,
                    ReanalysisItem.status == "pending",
                )
                .values(status="stopped", error_code=None)
            )
            batch = await session.get(ReanalysisBatch, batch_id)
            if batch is not None and batch.status == "stopping":
                batch.status = "stopped"
            await session.commit()

    async def _finish_content(self, batch_id: str) -> None:
        try:
            async with self._profile_retry_guard():
                async with self.database.session() as session:
                    unfinished = await session.scalar(
                        select(ReanalysisItem.id)
                        .where(
                            ReanalysisItem.reanalysis_batch_id == batch_id,
                            ReanalysisItem.status.in_(("pending", "running")),
                        )
                        .limit(1)
                    )
                    batch = await session.get(ReanalysisBatch, batch_id)
                    if (
                        unfinished is not None
                        or batch is None
                        or batch.status not in {"pending", "running"}
                    ):
                        return
                    batch.status = "content_completed_profile_failed"
                    await session.commit()
                await self.publisher.retry_profile(batch_id)
        except Exception:
            # The durable content-complete state exposes profile-only retry.
            logger.exception("History reanalysis profile rebuild failed")
            return

    @asynccontextmanager
    async def _profile_retry_guard(self):
        guard = getattr(self.task_coordinator, "profile_retry_guard", None)
        if guard is None:
            yield
            return
        async with guard():
            yield

    async def _has_active_version(self, batch_id: str) -> bool:
        async with self.database.session() as session:
            return (
                await session.scalar(
                    select(AnalysisVersion.id)
                    .where(
                        AnalysisVersion.reanalysis_batch_id == batch_id,
                        AnalysisVersion.status.in_(("pending", "running")),
                    )
                    .limit(1)
                )
                is not None
            )

    async def _next_pending_item(self, batch_id: str) -> ReanalysisItem | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(ReanalysisItem)
                .where(
                    ReanalysisItem.reanalysis_batch_id == batch_id,
                    ReanalysisItem.status == "pending",
                    ReanalysisItem.analysis_version_id.is_(None),
                )
                .order_by(ReanalysisItem.position, ReanalysisItem.id)
                .limit(1)
            )

    async def _set_running(self, batch_id: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(ReanalysisBatch)
                .where(
                    ReanalysisBatch.id == batch_id,
                    ReanalysisBatch.status == "pending",
                )
                .values(status="running")
            )
            await session.commit()

    async def _pause_snapshot_changed(
        self, batch_id: str, item_id: str, error_code: str
    ) -> None:
        async with self.database.session() as session:
            batch = await session.get(ReanalysisBatch, batch_id)
            item = await session.get(ReanalysisItem, item_id)
            if batch is not None and batch.status in {"pending", "running"}:
                batch.status = "paused_rules_changed"
            if item is not None and item.status == "pending":
                item.error_code = error_code
            await session.commit()

    async def _compatible_event_map(
        self,
        source_batch_id: str,
        fixed_rules_hash: str,
        fixed_rule_hashes: dict[str, str],
        transcript_sha256: str,
    ) -> tuple[str | None, str | None]:
        async with self.database.session() as session:
            source = await session.get(Batch, source_batch_id)
            if source is None or source.current_analysis_version_id is None:
                return None, None
            version = await session.get(
                AnalysisVersion, source.current_analysis_version_id
            )
            if (
                version is None
                or version.fixed_rules_hash != fixed_rules_hash
                or not version.event_map_json
                or not version.event_map_hash
                or hashlib.sha256(version.event_map_json.encode("utf-8")).hexdigest()
                != version.event_map_hash
            ):
                return None, None
            try:
                source_snapshot = json.loads(version.prompt_snapshot_json)
                source_metadata = source_snapshot["_reanalysis"]
            except (json.JSONDecodeError, KeyError, TypeError):
                return None, None
            if (
                source_metadata.get("fixed_rule_hashes") != fixed_rule_hashes
                or source_metadata.get("transcript_fingerprints", {}).get(
                    source_batch_id
                )
                != transcript_sha256
            ):
                return None, None
            rows = (
                await session.execute(
                    select(JobFile.position, Transcript.segment_index)
                    .join(Transcript, Transcript.job_file_id == JobFile.id)
                    .where(JobFile.job_id == source.job_id)
                )
            ).all()
        transcript_ids = {f"seg_{position}_{index}" for position, index in rows}
        try:
            event_map = EventMap.model_validate_json(version.event_map_json)
        except (ValidationError, ValueError):
            return None, None
        evidence_ids = {
            segment_id
            for event in event_map.events
            for segment_id in event.evidence_segment_ids
        } | set(event_map.unassigned_segment_ids)
        if evidence_ids != transcript_ids:
            return None, None
        return version.event_map_json, version.event_map_hash
