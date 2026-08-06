from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select, update

from audio_memory.analysis.profile import ProfileDelta
from audio_memory.analysis.profile_rebuild import ProfileRebuilder
from audio_memory.analysis.todos import reconcile_todos
from audio_memory.analysis.versions import require_card_version
from audio_memory.config import AppPaths
from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    ProfileCandidate,
    ProfileFact,
    ReanalysisBatch,
    ReanalysisItem,
    TempFileManifest,
    Todo,
    TodoCandidate,
    TodoTombstone,
)
from audio_memory.prompts.schemas import SceneResultBase, StrictTodoDraft
from audio_memory.prompts.store import PROMPT_SCENES


CARD_ORDER = ("meeting", "parenting", "content", "growth", "inspiration")


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    batch_id: str
    card_count: int
    todo_count: int


class VersionPublisher:
    def __init__(
        self,
        database: Database,
        paths: AppPaths | None = None,
        profile_rebuilder: ProfileRebuilder | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.profile_rebuilder = profile_rebuilder or ProfileRebuilder(database)

    async def retry_profile(self, reanalysis_batch_id: str) -> None:
        async with self.database.session() as session:
            history = await session.get(
                ReanalysisBatch, reanalysis_batch_id
            )
            if history is None:
                raise LookupError(
                    f"Unknown reanalysis batch: {reanalysis_batch_id}"
                )
            if history.status != "content_completed_profile_failed":
                raise ValueError(
                    "Profile retry requires content_completed_profile_failed"
                )
        rebuilt = await self._rebuild_profile(
            reanalysis_batch_id, datetime.now(UTC)
        )
        if not rebuilt:
            raise RuntimeError("Profile rebuild failed")

    async def publish(
        self,
        version_id: str,
        results: list[SceneResultBase],
        profile_candidates: list[ProfileDelta],
        *,
        worker_owner_id: str | None = None,
    ) -> AnalysisOutcome:
        by_scene = self._validated_scenes(results)
        visible = [
            by_scene[scene_id]
            for scene_id in CARD_ORDER
            if by_scene[scene_id].should_generate
        ]
        drafts = [todo for result in results for todo in result.todos]
        now = datetime.now(UTC)
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is None:
                raise LookupError(f"Unknown analysis version: {version_id}")
            job_id = version.source_job_id
            batch_id = version.batch_id or str(
                uuid5(NAMESPACE_URL, f"audio-memory-batch:{job_id}")
            )
            if version.status == "completed":
                return await self._completed_outcome(session, version, batch_id)
            if (
                version.status != "running"
                or worker_owner_id is not None
                and version.worker_owner_id != worker_owner_id
            ):
                raise RuntimeError("Analysis worker lease was lost before publication")
            files = list(
                await session.scalars(
                    select(JobFile)
                    .where(JobFile.job_id == job_id)
                    .order_by(JobFile.position)
                )
            )

        destinations = self._move_first_publication_audio(
            version, batch_id, files
        )
        rebuild_history_id: str | None = None
        rebuild_profile = False
        todo_count = 0
        async with self.database.session() as session:
            async with session.begin():
                await self._fence_worker(session, version_id, worker_owner_id)
                version = await session.get(AnalysisVersion, version_id)
                if version is None:
                    raise LookupError(f"Unknown analysis version: {version_id}")
                if version.status == "completed":
                    return await self._completed_outcome(session, version, batch_id)
                job = await session.get(AnalysisJob, version.source_job_id)
                if job is None:
                    raise LookupError(f"Unknown analysis job: {version.source_job_id}")
                batch = await session.get(Batch, batch_id)
                if batch is None:
                    batch = Batch(
                        id=batch_id,
                        job_id=job.id,
                        provider_id=version.provider_id,
                        model_id=version.model_id,
                        uploaded_at=now.isoformat(),
                        natural_date=now.date().isoformat(),
                    )
                    session.add(batch)
                    await session.flush()
                version.batch_id = batch.id
                await session.flush()
                await require_card_version(
                    session,
                    version_id=version.id,
                    expected_batch_id=batch.id,
                )

                await self._finalize_audio_rows(
                    session, job.id, files, destinations
                )
                await self._insert_cards(session, version, batch, visible)
                candidates = await self._insert_todo_candidates(
                    session, version, drafts
                )
                todo_count = await self._reconcile_todos(
                    session, batch, candidates
                )
                if version.reanalysis_batch_id is None:
                    await self._insert_profile_facts(
                        session, version, profile_candidates, now
                    )

                version.status = "completed"
                version.error_code = None
                version.completed_at = now.isoformat()
                version.published_card_count = len(visible)
                version.published_todo_count = todo_count
                version.worker_owner_id = None
                version.lease_expires_at = None
                batch.current_analysis_version_id = version.id
                batch.provider_id = version.provider_id
                batch.model_id = version.model_id
                job.provider_id = version.provider_id
                job.model_id = version.model_id
                job.stage = JobStage.COMPLETED.value
                job.error_code = None
                if version.reanalysis_batch_id is not None:
                    rebuild_history_id = version.reanalysis_batch_id
                    rebuild_profile = await self._complete_history_item(
                        session, version, now
                    )

        if rebuild_profile:
            await self._rebuild_profile(rebuild_history_id, now)
        return AnalysisOutcome(batch_id, len(visible), todo_count)

    @staticmethod
    def _validated_scenes(
        results: list[SceneResultBase],
    ) -> dict[str, SceneResultBase]:
        scene_ids = [result.scene_id for result in results]
        if len(scene_ids) != len(PROMPT_SCENES) or set(scene_ids) != set(
            PROMPT_SCENES
        ):
            raise ValueError("Publication requires all six scenes exactly once")
        return {result.scene_id: result for result in results}

    async def _completed_outcome(
        self, session, version: AnalysisVersion, batch_id: str
    ) -> AnalysisOutcome:
        if (
            version.published_card_count is None
            or version.published_todo_count is None
        ):
            raise RuntimeError(
                "Completed analysis version is missing its published outcome"
            )
        return AnalysisOutcome(
            batch_id,
            version.published_card_count,
            version.published_todo_count,
        )

    def _move_first_publication_audio(
        self,
        version: AnalysisVersion,
        batch_id: str,
        files: list[JobFile],
    ) -> dict[str, str]:
        if self.paths is None or version.batch_id is not None:
            return {}
        destination_root = self.paths.audio / batch_id
        destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        destinations: dict[str, str] = {}
        for source_file in files:
            source = Path(source_file.temporary_path)
            destination = destination_root / f"{source_file.id}{source_file.extension}"
            if source != destination and source.exists() and not destination.exists():
                os.replace(source, destination)
            if not destination.exists():
                raise FileNotFoundError(f"Missing publication audio: {source}")
            destinations[source_file.id] = str(destination)
        return destinations

    @staticmethod
    async def _fence_worker(session, version_id: str, worker_owner_id: str | None) -> None:
        if worker_owner_id is None:
            return
        fenced = await session.execute(
            update(AnalysisVersion)
            .where(
                AnalysisVersion.id == version_id,
                AnalysisVersion.status == "running",
                AnalysisVersion.worker_owner_id == worker_owner_id,
            )
            .values(worker_owner_id=worker_owner_id)
        )
        if int(fenced.rowcount) != 1:
            raise RuntimeError("Analysis worker lease was lost before publication")

    @staticmethod
    async def _finalize_audio_rows(
        session,
        job_id: str,
        files: list[JobFile],
        destinations: dict[str, str],
    ) -> None:
        for source_file in files:
            stored_file = await session.get(JobFile, source_file.id)
            if stored_file is not None and source_file.id in destinations:
                stored_file.temporary_path = destinations[source_file.id]
        manifests = list(
            await session.scalars(
                select(TempFileManifest).where(TempFileManifest.task_uuid == job_id)
            )
        )
        for manifest in manifests:
            await session.delete(manifest)

    @staticmethod
    async def _insert_cards(session, version, batch, visible) -> None:
        for position, result in enumerate(visible):
            card_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"audio-memory-card:{version.id}:{result.scene_id}:{position}",
                )
            )
            if await session.get(Card, card_id) is None:
                session.add(
                    Card(
                        id=card_id,
                        batch_id=batch.id,
                        analysis_version_id=version.id,
                        scene_id=result.scene_id,
                        position=position,
                        payload_json=json.dumps(
                            result.model_dump_for_frontend(), ensure_ascii=False
                        ),
                    )
                )

    async def _insert_todo_candidates(
        self,
        session,
        version: AnalysisVersion,
        drafts: list[StrictTodoDraft],
    ) -> list[TodoCandidate]:
        candidates: list[TodoCandidate] = []
        prepared = [
            (
                self._todo_fingerprint(version.source_job_id, draft),
                (
                    draft.due_at.date().isoformat()
                    if draft.due_at is not None
                    else "no-due-date"
                ),
                draft,
            )
            for draft in drafts
        ]
        seen_candidates: set[tuple[str, str]] = set()
        used_fingerprints: set[str] = set()
        for base_fingerprint, due_key, draft in sorted(
            prepared, key=lambda item: (item[0], item[1], item[2].text)
        ):
            candidate_key = (base_fingerprint, due_key)
            if candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            fingerprint = base_fingerprint
            if fingerprint in used_fingerprints:
                fingerprint = sha256(
                    f"{base_fingerprint}\0{due_key}".encode("utf-8")
                ).hexdigest()
            used_fingerprints.add(fingerprint)
            candidate_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"audio-memory-todo-candidate:{version.id}:{fingerprint}",
                )
            )
            candidate = await session.get(TodoCandidate, candidate_id)
            if candidate is None:
                candidate = TodoCandidate(
                    id=candidate_id,
                    analysis_version_id=version.id,
                    source_job_id=version.source_job_id,
                    source_event_id=draft.source_event_id,
                    evidence_segment_ids_json=json.dumps(
                        draft.evidence_segment_ids, ensure_ascii=False
                    ),
                    normalized_action=self._normalize(draft.action),
                    normalized_object=self._normalize(draft.object),
                    normalized_assignee=self._normalize(draft.assignee_text),
                    text=draft.text,
                    due_at=(
                        draft.due_at.isoformat() if draft.due_at is not None else None
                    ),
                    source_fingerprint=fingerprint,
                )
                session.add(candidate)
            candidates.append(candidate)
        await session.flush()
        return candidates

    @classmethod
    def _todo_fingerprint(cls, job_id: str, draft: StrictTodoDraft) -> str:
        identity = json.dumps(
            {
                "source_job_id": job_id,
                "source_event_id": draft.source_event_id,
                "normalized_action": cls._normalize(draft.action),
                "normalized_object": cls._normalize(draft.object),
                "normalized_assignee": cls._normalize(draft.assignee_text),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize(value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    @staticmethod
    async def _reconcile_todos(session, batch, candidates) -> int:
        existing = list(
            await session.scalars(select(Todo).where(Todo.batch_id == batch.id))
        )
        tombstones = list(await session.scalars(select(TodoTombstone)))
        reconciled = reconcile_todos(batch.id, candidates, existing, tombstones)
        known_ids = {todo.id for todo in existing}
        session.add_all([todo for todo in reconciled if todo.id not in known_ids])
        await session.flush()
        if not candidates:
            return 0
        return int(
            await session.scalar(
                select(func.count(Todo.id)).where(
                    Todo.analysis_version_id
                    == candidates[0].analysis_version_id
                )
            )
            or 0
        )

    @staticmethod
    async def _insert_profile_facts(
        session,
        version: AnalysisVersion,
        profile_candidates: list[ProfileDelta],
        now: datetime,
    ) -> None:
        for position, candidate in enumerate(profile_candidates):
            fact_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"audio-memory-profile:{version.id}:{position}",
                )
            )
            if await session.get(ProfileFact, fact_id) is not None:
                continue
            session.add(
                ProfileFact(
                    id=fact_id,
                    subject_id=candidate.subject_id,
                    dimension=candidate.dimension,
                    value_json=json.dumps(candidate.value, ensure_ascii=False),
                    confidence=candidate.confidence,
                    source_audio_json=json.dumps(
                        [version.source_job_id], ensure_ascii=False
                    ),
                    first_seen_at=now.isoformat(),
                    last_seen_at=now.isoformat(),
                    evidence_count=1,
                    origin="explicit" if candidate.explicit else "inferred",
                    status="active",
                )
            )

    @staticmethod
    async def _complete_history_item(
        session, version: AnalysisVersion, now: datetime
    ) -> bool:
        item = await session.scalar(
            select(ReanalysisItem).where(
                ReanalysisItem.analysis_version_id == version.id
            )
        )
        if item is not None:
            item.status = "succeeded"
            item.error_code = None
            item.completed_at = now.isoformat()
        remaining = await session.scalar(
            select(ReanalysisItem.id)
            .where(
                ReanalysisItem.reanalysis_batch_id == version.reanalysis_batch_id,
                ReanalysisItem.status.in_(("pending", "running")),
                ReanalysisItem.id != (item.id if item is not None else ""),
            )
            .limit(1)
        )
        if remaining is not None:
            return False
        history = await session.get(
            ReanalysisBatch, version.reanalysis_batch_id
        )
        if history is not None:
            # This durable state makes a crash between content publication and
            # the independent profile transaction recoverable via profile-only
            # retry. A successful swap replaces it immediately.
            history.status = "content_completed_profile_failed"
        return True

    async def _rebuild_profile(
        self, history_id: str | None, now: datetime
    ) -> bool:
        try:
            async with self.database.session() as session:
                current_versions = list(
                    await session.scalars(
                        select(AnalysisVersion)
                        .join(
                            Batch,
                            Batch.current_analysis_version_id == AnalysisVersion.id,
                        )
                        .order_by(AnalysisVersion.id)
                    )
                )
            facts = await self.profile_rebuilder.rebuild(current_versions)
            await self.profile_rebuilder.swap_active(facts)
        except Exception:
            if history_id is not None:
                await self._set_history_terminal(
                    history_id, "content_completed_profile_failed", now
                )
                return False
            raise
        if history_id is not None:
            async with self.database.session() as session:
                failed = int(
                    await session.scalar(
                        select(func.count(ReanalysisItem.id)).where(
                            ReanalysisItem.reanalysis_batch_id == history_id,
                            ReanalysisItem.status == "failed",
                        )
                    )
                    or 0
                )
            await self._set_history_terminal(
                history_id,
                "completed_with_failures" if failed else "completed",
                now,
            )
        return True

    async def _set_history_terminal(
        self, history_id: str, status: str, now: datetime
    ) -> None:
        async with self.database.session() as session:
            history = await session.get(ReanalysisBatch, history_id)
            if history is not None:
                history.status = status
                history.completed_at = now.isoformat()
                await session.commit()


# Compatibility import for Task 5 call sites; the production implementation is
# now the strict, version-keyed publisher above.
AnalysisPublisher = VersionPublisher
