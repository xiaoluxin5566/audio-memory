from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import delete, select, text, update

from audio_memory.db import Database
from audio_memory.models import AnalysisVersion, ReanalysisBatch, ReanalysisItem
from audio_memory.reanalysis.preview import (
    PreviewTokenExpiredError,
    PreviewTokenInvalidError,
    ReanalysisPreviewBuilder,
)
from audio_memory.reanalysis.types import ReanalysisBatchView, ReanalysisItemView


ACTIVE_BATCH_STATES = frozenset(
    {
        "pending",
        "running",
        "paused",
        "stopping",
        # Accepted durable Task 5 states. The API normalizes them to paused.
        "paused_credential_changed",
        "paused_rules_changed",
        "paused_error",
    }
)


class SnapshotChangedError(RuntimeError):
    pass


class PreviewBlockedError(RuntimeError):
    def __init__(self, blockers: list[str]) -> None:
        super().__init__("Reanalysis preview is blocked")
        self.blockers = blockers


class ReanalysisStateError(RuntimeError):
    pass


class ReanalysisNotFoundError(LookupError):
    pass


class ReanalysisService:
    def __init__(
        self,
        *,
        database: Database,
        preview_builder: ReanalysisPreviewBuilder,
        provider_coordinator,
        task_coordinator=None,
        worker=None,
        publisher=None,
    ) -> None:
        self.database = database
        self.preview_builder = preview_builder
        self.provider_coordinator = provider_coordinator
        self.task_coordinator = task_coordinator
        self.worker = worker
        self.publisher = publisher
        self._mutation_lock = asyncio.Lock()

    async def preview(self):
        return await self.preview_builder.build()

    async def create_batch(self, preview_token: str) -> ReanalysisBatchView:
        async with self._mutation_lock:
            active = await self._active_batch()
            if active is not None:
                return await self._view(active.id)
            try:
                supplied = self.preview_builder.signer.verify(preview_token)
            except (PreviewTokenExpiredError, PreviewTokenInvalidError) as exc:
                raise SnapshotChangedError(str(exc)) from exc

            provider_id = supplied.get("provider_id")
            if not isinstance(provider_id, str):
                raise SnapshotChangedError("Preview token has no provider binding")
            if not provider_id:
                raise PreviewBlockedError(["no_active_provider"])
            validation = await self.provider_coordinator.validate_saved(provider_id)
            if not bool(getattr(validation, "ok", False)):
                raise PreviewBlockedError(["provider_validation_failed"])

            batch_id = str(uuid4())
            with self.preview_builder.prompt_store.freeze():
                async with self._active_provider_guard() as provider_binding:
                    async with self.database.session() as session:
                        await session.execute(text("BEGIN IMMEDIATE"))
                        try:
                            current = await self.preview_builder.build(
                                provider_binding=provider_binding
                            )
                            self._require_matching_snapshot(supplied, current)
                            if current.blockers:
                                raise PreviewBlockedError(current.blockers)
                            snapshot = current.snapshot
                            persisted_prompts = dict(snapshot.prompt_snapshot)
                            persisted_prompts["_reanalysis"] = {
                                "fixed_rule_hashes": snapshot.fixed_rule_hashes,
                                "profile_hash": snapshot.profile_hash,
                                "transcript_fingerprints": {
                                    source.batch_id: source.transcript_sha256
                                    for source in snapshot.sources
                                },
                            }
                    # The process-local lock is authoritative because the app's
                    # instance lock permits only one backend process.
                            active = await session.scalar(
                                select(ReanalysisBatch)
                                .where(ReanalysisBatch.status.in_(ACTIVE_BATCH_STATES))
                                .order_by(ReanalysisBatch.created_at.desc())
                                .limit(1)
                            )
                            if active is not None:
                                batch_id = active.id
                            else:
                                session.add(
                                    ReanalysisBatch(
                                        id=batch_id,
                                        status="pending",
                                        provider_id=snapshot.provider_id,
                                        model_id=snapshot.model_id,
                                        credential_generation=(
                                            snapshot.credential_generation
                                        ),
                                        prompt_snapshot_json=json.dumps(
                                            persisted_prompts,
                                            ensure_ascii=False,
                                            sort_keys=True,
                                        ),
                                        profile_snapshot_json=json.dumps(
                                            list(snapshot.profile_snapshot),
                                            ensure_ascii=False,
                                            sort_keys=True,
                                        ),
                                        fixed_rules_hash=snapshot.fixed_rules_hash,
                                        snapshot_hash=current.snapshot_hash,
                                    )
                                )
                                session.add_all(
                                    ReanalysisItem(
                                        id=str(uuid4()),
                                        reanalysis_batch_id=batch_id,
                                        source_batch_id=source.batch_id,
                                        position=position,
                                        status="pending",
                                    )
                                    for position, source in enumerate(snapshot.sources)
                                )
                            await session.commit()
                        except BaseException:
                            await session.rollback()
                            raise
            if self.worker is not None:
                await self.worker.notify()
            return await self._view(batch_id)

    @staticmethod
    def _require_matching_snapshot(supplied, current) -> None:
        current_payload = current.snapshot.canonical_payload()
        comparable = {
            "scope": current.snapshot.scope,
            "source_batch_ids": [
                source.batch_id for source in current.snapshot.sources
            ],
            "provider_id": current.snapshot.provider_id,
            "model_id": current.snapshot.model_id,
            "credential_generation": current.snapshot.credential_generation,
            "prompt_hashes": current.snapshot.prompt_hashes,
            "prompt_bindings": current_payload["prompt_bindings"],
            "fixed_rule_hashes": current.snapshot.fixed_rule_hashes,
            "fixed_rules_hash": current.snapshot.fixed_rules_hash,
            "profile_hash": current.snapshot.profile_hash,
            "counts": current_payload["counts"],
            "snapshot_hash": current.snapshot_hash,
        }
        if any(supplied.get(key) != value for key, value in comparable.items()):
            raise SnapshotChangedError(
                "History or analysis configuration changed after preview"
            )

    @asynccontextmanager
    async def _active_provider_guard(self):
        guard = getattr(self.provider_coordinator, "active_snapshot_guard", None)
        if guard is None:
            yield await self.provider_coordinator.snapshot_active_with_generation()
            return
        async with guard() as binding:
            yield binding

    async def current(self) -> ReanalysisBatchView | None:
        active = await self._active_batch()
        if active is not None:
            return await self._view(active.id)
        async with self.database.session() as session:
            latest = await session.scalar(
                select(ReanalysisBatch)
                .order_by(ReanalysisBatch.created_at.desc(), ReanalysisBatch.id.desc())
                .limit(1)
            )
        return None if latest is None else await self._view(latest.id)

    async def stop(self, batch_id: str) -> ReanalysisBatchView:
        async with self._mutation_lock:
            async with self.database.session() as session:
                batch = await session.get(ReanalysisBatch, batch_id)
                if batch is None:
                    raise ReanalysisNotFoundError(
                        f"Unknown reanalysis batch: {batch_id}"
                    )
                if batch.status in {"stopped", "completed", "completed_with_failures"}:
                    return await self._view(batch_id)
                if batch.status == "content_completed_profile_failed":
                    raise ReanalysisStateError("Content is already complete")
                batch.status = "stopping"
                await session.commit()
            if self.worker is not None:
                await self.worker.notify()
                await self.worker.tick()
            else:
                await self._finish_idle_stop(batch_id)
            return await self._view(batch_id)

    async def resume(self, batch_id: str) -> ReanalysisBatchView:
        async with self._mutation_lock:
            async with self.database.session() as session:
                batch = await session.get(ReanalysisBatch, batch_id)
                if batch is None:
                    raise ReanalysisNotFoundError(
                        f"Unknown reanalysis batch: {batch_id}"
                    )
                pause_reason = await session.scalar(
                    select(ReanalysisItem.error_code)
                    .where(
                        ReanalysisItem.reanalysis_batch_id == batch_id,
                        ReanalysisItem.error_code.is_not(None),
                    )
                    .order_by(ReanalysisItem.position)
                    .limit(1)
                )
                if batch.status == "paused_rules_changed" or pause_reason in {
                    "fixed_rules_changed",
                    "analysis_schema_changed",
                    "transcript_changed",
                }:
                    raise ReanalysisStateError(
                        "Fixed analysis rules changed; stop and obtain a fresh preview"
                    )
                if batch.status not in {
                    "paused",
                    "paused_credential_changed",
                    "paused_error",
                    "stopped",
                }:
                    raise ReanalysisStateError(
                        f"Cannot resume reanalysis from {batch.status}"
                    )
                provider_id = batch.provider_id
                expected_model = batch.model_id
                expected_fixed = batch.fixed_rules_hash

            validation = await self.provider_coordinator.validate_saved(provider_id)
            if not bool(getattr(validation, "ok", False)):
                raise ReanalysisStateError("Provider validation must succeed before resume")
            provider, generation = (
                await self.provider_coordinator.snapshot_active_with_generation()
            )
            if provider.provider_id != provider_id or provider.model_id != expected_model:
                raise ReanalysisStateError(
                    "Provider or model changed; stop and obtain a fresh preview"
                )
            from audio_memory.prompts.composer import PromptComposer

            if PromptComposer.fixed_rules_hash() != expected_fixed:
                raise ReanalysisStateError(
                    "Fixed analysis rules changed; stop and obtain a fresh preview"
                )

            async with self.database.session() as session:
                async with session.begin():
                    batch = await session.get(ReanalysisBatch, batch_id)
                    if batch is None:
                        raise ReanalysisNotFoundError(
                            f"Unknown reanalysis batch: {batch_id}"
                        )
                    items = list(
                        await session.scalars(
                            select(ReanalysisItem).where(
                                ReanalysisItem.reanalysis_batch_id == batch_id,
                                ReanalysisItem.status.in_(("pending", "stopped")),
                            )
                        )
                    )
                    obsolete_ids = {
                        item.analysis_version_id
                        for item in items
                        if item.analysis_version_id is not None
                    }
                    for item in items:
                        item.analysis_version_id = None
                        item.status = "pending"
                        item.error_code = None
                        item.completed_at = None
                    if obsolete_ids:
                        await session.execute(
                            delete(AnalysisVersion).where(
                                AnalysisVersion.id.in_(obsolete_ids),
                                AnalysisVersion.status.not_in(("completed", "running")),
                            )
                        )
                    batch.credential_generation = generation
                    batch.status = "running"
                    batch.completed_at = None
            if self.worker is not None:
                await self.worker.notify()
            return await self._view(batch_id)

    async def retry_profile(self, batch_id: str) -> ReanalysisBatchView:
        if self.publisher is None:
            raise ReanalysisStateError("Profile publisher is unavailable")
        async with self._mutation_lock:
            async with self.database.session() as session:
                batch = await session.get(ReanalysisBatch, batch_id)
                if batch is None:
                    raise ReanalysisNotFoundError(
                        f"Unknown reanalysis batch: {batch_id}"
                    )
                if batch.status != "content_completed_profile_failed":
                    raise ReanalysisStateError(
                        "Profile retry requires content_completed_profile_failed"
                    )
            async with self._profile_retry_guard():
                async with self.database.session() as session:
                    batch = await session.get(ReanalysisBatch, batch_id)
                    if batch is None:
                        raise ReanalysisNotFoundError(
                            f"Unknown reanalysis batch: {batch_id}"
                        )
                    if batch.status != "content_completed_profile_failed":
                        raise ReanalysisStateError(
                            "Profile retry requires content_completed_profile_failed"
                        )
                await self.publisher.retry_profile(batch_id)
            return await self._view(batch_id)

    @asynccontextmanager
    async def _profile_retry_guard(self):
        coordinator = self.task_coordinator
        if coordinator is None and self.worker is not None:
            coordinator = getattr(self.worker, "task_coordinator", None)
        guard = getattr(coordinator, "profile_retry_guard", None)
        if guard is None:
            yield
            return
        async with guard():
            yield

    async def _finish_idle_stop(self, batch_id: str) -> None:
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

    async def _active_batch(self) -> ReanalysisBatch | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(ReanalysisBatch)
                .where(ReanalysisBatch.status.in_(ACTIVE_BATCH_STATES))
                .order_by(ReanalysisBatch.created_at.desc(), ReanalysisBatch.id.desc())
                .limit(1)
            )

    async def _view(self, batch_id: str) -> ReanalysisBatchView:
        async with self.database.session() as session:
            batch = await session.get(ReanalysisBatch, batch_id)
            if batch is None:
                raise ReanalysisNotFoundError(f"Unknown reanalysis batch: {batch_id}")
            rows = list(
                await session.scalars(
                    select(ReanalysisItem)
                    .where(ReanalysisItem.reanalysis_batch_id == batch_id)
                    .order_by(ReanalysisItem.position)
                )
            )
        counts = Counter(row.status for row in rows)
        normalized_status = (
            "paused" if batch.status.startswith("paused_") else batch.status
        )
        running = next((row.id for row in rows if row.status == "running"), None)
        return ReanalysisBatchView(
            id=batch.id,
            status=normalized_status,
            provider_id=batch.provider_id,
            model_id=batch.model_id,
            credential_generation=batch.credential_generation,
            snapshot_hash=batch.snapshot_hash,
            total=len(rows),
            pending=counts["pending"],
            running=counts["running"],
            succeeded=counts["succeeded"],
            failed=counts["failed"],
            stopped=counts["stopped"],
            current_item_id=running,
            items=tuple(
                ReanalysisItemView(
                    id=row.id,
                    source_batch_id=row.source_batch_id,
                    position=row.position,
                    status=row.status,
                    error_code=row.error_code,
                )
                for row in rows
            ),
            created_at=batch.created_at,
            updated_at=batch.updated_at,
            completed_at=batch.completed_at,
        )
