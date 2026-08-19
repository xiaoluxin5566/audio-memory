from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select

from audio_memory.config import AppPaths, PinnedDevelopmentRoot
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    QAMessage,
    Todo,
    TodoTombstone,
    Transcript,
)
from audio_memory.transcript_safety import (
    pending_risk_review_exists,
    safe_active_profile_facts,
)


class QuestionAnswerer(Protocol):
    async def answer(self, **kwargs) -> str: ...


@dataclass(frozen=True, slots=True)
class OpenedEvidenceAudio:
    descriptor: int
    name: str
    stat_result: os.stat_result


def is_overdue(*, due_at: str | None, completed: bool, now: datetime) -> bool:
    if completed or not due_at:
        return False
    try:
        due = datetime.fromisoformat(due_at)
    except ValueError:
        return False
    if due.tzinfo is None:
        return False
    return due.astimezone(UTC) < now


class ContentService:
    def __init__(
        self,
        database: Database,
        paths: AppPaths,
        answerer: QuestionAnswerer,
        *,
        read_boundary: PinnedDevelopmentRoot | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.answerer = answerer
        self.read_boundary = read_boundary

    async def feed(self) -> dict[str, object]:
        now = datetime.now(UTC)
        async with self.database.session() as session:
            todos = list(
                await session.scalars(
                    select(Todo)
                    .join(Batch, Batch.id == Todo.batch_id)
                    .where(~pending_risk_review_exists(Batch.job_id))
                )
            )
            rows = list(
                (
                    await session.execute(
                        select(Batch, Card, AnalysisVersion)
                        .join(
                            Card,
                            (Card.batch_id == Batch.id)
                            & (
                                Card.analysis_version_id
                                == Batch.current_analysis_version_id
                            ),
                        )
                        .join(
                            AnalysisVersion,
                            AnalysisVersion.id == Card.analysis_version_id,
                        )
                        .where(~pending_risk_review_exists(Batch.job_id))
                        .order_by(Batch.uploaded_at.desc(), Card.position)
                    )
                ).all()
            )
            card_ids = [card.id for _, card, _ in rows]
            evidence_by_card = {
                card.id: await self._evidence_view(
                    session,
                    card=card,
                    version=version,
                )
                for _, card, version in rows
            }
            qa_rows = (
                list(
                    await session.scalars(
                        select(QAMessage)
                        .where(QAMessage.card_id.in_(card_ids))
                        .order_by(QAMessage.card_id, QAMessage.position)
                    )
                )
                if card_ids
                else []
            )
        qa_by_card: dict[str, list[dict[str, str]]] = defaultdict(list)
        for message in qa_rows:
            qa_by_card[message.card_id].append(
                {"role": message.role, "content": message.content}
            )
        days: dict[str, list[dict[str, object]]] = defaultdict(list)
        for batch, card, version in rows:
            payload = json.loads(card.payload_json)
            card_view = {
                "id": card.id,
                "batch_id": batch.id,
                "scene_id": card.scene_id,
                "uploaded_at": batch.uploaded_at,
                "payload": payload,
                "evidence": evidence_by_card[card.id],
                "qa": qa_by_card[card.id],
            }
            sources = self._external_source_view(version, payload)
            if sources is not None:
                card_view["sources"] = sources
            days[batch.natural_date].append(card_view)
        todos.sort(key=lambda item: item.created_at, reverse=True)
        todos.sort(
            key=lambda item: (
                2
                if item.completed
                else 0
                if is_overdue(due_at=item.due_at, completed=item.completed, now=now)
                else 1
            )
        )
        return {
            "todos": [self._todo_view(item, now=now) for item in todos],
            "days": [
                {"date": date, "cards": cards}
                for date, cards in sorted(days.items(), reverse=True)
            ],
        }

    @classmethod
    def _external_source_view(
        cls,
        version: AnalysisVersion,
        payload: object,
    ) -> list[dict[str, object]] | None:
        if version.external_sources_json is None:
            return None
        try:
            raw_sources = json.loads(version.external_sources_json)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(raw_sources, list):
            return []
        sources_by_id = {
            item["source_id"]: item
            for item in raw_sources
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
        return [
            sources_by_id[source_id]
            for source_id in cls._collect_external_source_ids(payload)
            if source_id in sources_by_id
        ]

    @classmethod
    def _collect_external_source_ids(cls, value: object) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            source_ids = value.get("external_source_ids")
            if isinstance(source_ids, list):
                found.extend(item for item in source_ids if isinstance(item, str))
            for nested in value.values():
                found.extend(cls._collect_external_source_ids(nested))
        elif isinstance(value, list):
            for nested in value:
                found.extend(cls._collect_external_source_ids(nested))
        return list(dict.fromkeys(found))

    async def evidence_audio(
        self, card_id: str, segment_id: str
    ) -> Path | OpenedEvidenceAudio:
        match = re.fullmatch(r"seg_(\d+)_(\d+)", segment_id)
        if match is None:
            raise LookupError("Unknown card evidence")
        position, segment_index = (int(value) for value in match.groups())
        async with self.database.session() as session:
            row = (
                await session.execute(
                    select(Card, Batch, AnalysisVersion)
                    .join(Batch, Batch.id == Card.batch_id)
                    .join(
                        AnalysisVersion,
                        AnalysisVersion.id == Card.analysis_version_id,
                    )
                    .where(
                        Card.id == card_id,
                        Card.analysis_version_id
                        == Batch.current_analysis_version_id,
                        ~pending_risk_review_exists(Batch.job_id),
                    )
                )
            ).one_or_none()
            if row is None:
                raise LookupError("Unknown card evidence")
            card, _, version = row
            if segment_id not in self._scene_evidence_ids(version, card.scene_id):
                raise LookupError("Unknown card evidence")
            audio_row = (
                await session.execute(
                    select(JobFile, Transcript)
                    .join(Transcript, Transcript.job_file_id == JobFile.id)
                    .where(
                        JobFile.job_id == version.source_job_id,
                        JobFile.position == position,
                        Transcript.segment_index == segment_index,
                        Transcript.risk_classified.is_(True),
                        Transcript.is_reliable.is_(True),
                    )
                )
            ).one_or_none()
        if audio_row is None:
            raise LookupError("Unknown card evidence")
        source_file, _ = audio_row
        if self.read_boundary is None:
            audio_root = self.paths.audio.resolve()
            audio_path = Path(source_file.temporary_path).resolve()
            if not audio_path.is_relative_to(audio_root) or not audio_path.is_file():
                raise LookupError("Unknown card evidence")
            return audio_path

        audio_root = Path(os.path.abspath(os.fspath(self.paths.audio)))
        audio_path = Path(
            os.path.abspath(os.fspath(Path(source_file.temporary_path)))
        )
        if not audio_path.is_relative_to(audio_root):
            raise LookupError("Unknown card evidence")
        try:
            descriptor = self.read_boundary.open_regular_file(
                audio_path, os.O_RDONLY
            )
        except OSError as exc:
            raise LookupError("Unknown card evidence") from exc
        try:
            metadata = os.fstat(descriptor)
            return OpenedEvidenceAudio(
                descriptor=descriptor,
                name=audio_path.name,
                stat_result=metadata,
            )
        except BaseException:
            os.close(descriptor)
            raise

    async def _evidence_view(
        self,
        session,
        *,
        card: Card,
        version: AnalysisVersion,
    ) -> list[dict[str, object]]:
        if card.scene_id == "analysis":
            payload = json.loads(card.payload_json)
            source_cards = payload.get("cards", []) if isinstance(payload, dict) else []
        else:
            staged = self._staged_scene(version, card.scene_id)
            source_cards = staged.get("cards", [])
        if not isinstance(source_cards, list) or not source_cards:
            return []
        requested_ids = {
            segment_id
            for source_card in source_cards
            for segment_id in self._collect_evidence_ids(source_card)
        }
        if not requested_ids:
            return []
        rows = list(
            (
                await session.execute(
                    select(JobFile.position, Transcript)
                    .join(Transcript, Transcript.job_file_id == JobFile.id)
                    .where(
                        JobFile.job_id == version.source_job_id,
                        Transcript.risk_classified.is_(True),
                        Transcript.is_reliable.is_(True),
                    )
                    .order_by(
                        JobFile.position,
                        Transcript.start_ms,
                        Transcript.segment_index,
                    )
                )
            ).all()
        )
        segments = {
            f"seg_{position}_{transcript.segment_index}": {
                "segment_id": f"seg_{position}_{transcript.segment_index}",
                "start_ms": transcript.start_ms,
                "end_ms": transcript.end_ms,
                "playback_url": (
                    f"/api/cards/{quote(card.id, safe='')}"
                    f"/evidence/{quote(f'seg_{position}_{transcript.segment_index}', safe='')}"
                    "/audio"
                ),
            }
            for position, transcript in rows
            if f"seg_{position}_{transcript.segment_index}" in requested_ids
        }
        evidence: list[dict[str, object]] = []
        for card_index, source_card in enumerate(source_cards):
            source_ids = self._collect_evidence_ids(source_card)
            card_segments = sorted(
                (
                    segments[segment_id]
                    for segment_id in source_ids
                    if segment_id in segments
                ),
                key=lambda item: (item["start_ms"], item["end_ms"]),
            )
            if card_segments:
                evidence.append(
                    {"card_index": card_index, "segments": card_segments}
                )
        return evidence

    @staticmethod
    def _staged_scene(
        version: AnalysisVersion, scene_id: str
    ) -> dict[str, object]:
        try:
            staged = json.loads(version.staged_results_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        scene = staged.get(scene_id, {}) if isinstance(staged, dict) else {}
        return scene if isinstance(scene, dict) else {}

    @classmethod
    def _scene_evidence_ids(
        cls, version: AnalysisVersion, scene_id: str
    ) -> list[str]:
        if scene_id == "analysis":
            try:
                staged = json.loads(version.staged_results_json)
            except (TypeError, json.JSONDecodeError):
                return []
            return cls._collect_evidence_ids(
                staged.get("autonomous", {}) if isinstance(staged, dict) else {}
            )
        return cls._collect_evidence_ids(cls._staged_scene(version, scene_id))

    @classmethod
    def _collect_evidence_ids(cls, value: object) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            evidence = value.get("evidence_segment_ids")
            if isinstance(evidence, list):
                found.extend(item for item in evidence if isinstance(item, str))
            for nested in value.values():
                found.extend(cls._collect_evidence_ids(nested))
        elif isinstance(value, list):
            for nested in value:
                found.extend(cls._collect_evidence_ids(nested))
        return list(dict.fromkeys(found))

    async def history(self) -> dict[str, object]:
        async with self.database.session() as session:
            rows = await session.execute(
                select(Batch, JobFile)
                .join(JobFile, JobFile.job_id == Batch.job_id)
                .order_by(Batch.uploaded_at.desc(), JobFile.position)
            )
        days: dict[str, list[dict[str, object]]] = defaultdict(list)
        for batch, file in rows:
            days[batch.natural_date].append(
                {
                    "id": file.id,
                    "original_name": file.original_name,
                    "duration_ms": file.duration_ms,
                    "uploaded_at": batch.uploaded_at,
                }
            )
        return {
            "days": [
                {"date": date, "audio": audio}
                for date, audio in sorted(days.items(), reverse=True)
            ]
        }

    async def update_todo(
        self,
        todo_id: str,
        *,
        text: str | None,
        completed: bool | None,
        due_at: str | None,
        update_due_at: bool,
    ) -> dict[str, object]:
        async with self.database.session() as session:
            todo = await session.scalar(
                select(Todo)
                .join(Batch, Batch.id == Todo.batch_id)
                .where(
                    Todo.id == todo_id,
                    ~pending_risk_review_exists(Batch.job_id),
                )
            )
            if todo is None:
                raise LookupError("Unknown todo")
            if text is not None:
                if not text.strip():
                    raise ValueError("Todo text cannot be blank")
                todo.text = text.strip()
                todo.user_edited = True
            if completed is not None:
                todo.completed = completed
                todo.completion_source = "user"
            if update_due_at:
                todo.due_at = self._normalize_due_at(due_at)
                todo.user_edited = True
            await session.commit()
            await session.refresh(todo)
            return self._todo_view(todo, now=datetime.now(UTC))

    async def delete_todo(self, todo_id: str) -> None:
        async with self.database.session() as session:
            todo = await session.scalar(
                select(Todo)
                .join(Batch, Batch.id == Todo.batch_id)
                .where(
                    Todo.id == todo_id,
                    ~pending_risk_review_exists(Batch.job_id),
                )
            )
            if todo is None:
                raise LookupError("Unknown todo")
            if todo.source_fingerprint is not None:
                tombstone = await session.get(
                    TodoTombstone, todo.source_fingerprint
                )
                if tombstone is None:
                    session.add(
                        TodoTombstone(
                            source_fingerprint=todo.source_fingerprint
                        )
                    )
            await session.delete(todo)
            await session.commit()

    async def ask(self, card_id: str, question: str) -> list[dict[str, str]]:
        if not question.strip():
            raise ValueError("Question cannot be blank")
        transcript, card = await self._card_context(card_id)
        async with self.database.session() as session:
            existing = list(
                await session.scalars(
                    select(QAMessage)
                    .where(QAMessage.card_id == card_id)
                    .order_by(QAMessage.position)
                )
            )
        history = [{"role": item.role, "content": item.content} for item in existing]
        async with self.database.session() as session:
            facts = await safe_active_profile_facts(session)
        profile = [
            {
                "dimension": item.dimension,
                "value": json.loads(item.value_json),
                "confidence": item.confidence,
            }
            for item in facts
        ]
        answer = await self.answerer.answer(
            card=card,
            transcript=transcript,
            profile=profile,
            history=history,
            question=question.strip(),
        )
        messages = [
            QAMessage(id=str(uuid4()), card_id=card_id, role="user", content=question.strip(), position=len(existing)),
            QAMessage(id=str(uuid4()), card_id=card_id, role="assistant", content=answer, position=len(existing) + 1),
        ]
        async with self.database.session() as session:
            session.add_all(messages)
            await session.commit()
        return history + [
            {"role": "user", "content": question.strip()},
            {"role": "assistant", "content": answer},
        ]

    async def feedback_context(self, card_id: str) -> dict[str, object]:
        transcript, card = await self._card_context(card_id)
        async with self.database.session() as session:
            card_row = await session.get(Card, card_id)
            batch = await session.get(Batch, card_row.batch_id) if card_row else None
            version = (
                await session.get(AnalysisVersion, card_row.analysis_version_id)
                if card_row and card_row.analysis_version_id
                else None
            )
            files = list(await session.scalars(select(JobFile).where(JobFile.job_id == batch.job_id))) if batch else []
            qa = list(await session.scalars(select(QAMessage).where(QAMessage.card_id == card_id).order_by(QAMessage.position)))
        return {
            "scene_id": card_row.scene_id if card_row else "unknown",
            "card": card,
            "audio": [{"name": item.original_name, "duration_ms": item.duration_ms} for item in files],
            "transcript": transcript,
            "qa": [{"role": item.role, "content": item.content} for item in qa],
            "provider_id": version.provider_id if version else None,
            "model_id": version.model_id if version else None,
            "prompt_snapshot": (
                json.loads(version.prompt_snapshot_json) if version else {}
            ),
        }

    async def _card_context(self, card_id: str) -> tuple[str, dict[str, object]]:
        async with self.database.session() as session:
            card_context = (
                await session.execute(
                    select(Card, Batch)
                    .join(Batch, Batch.id == Card.batch_id)
                    .where(
                        Card.id == card_id,
                        ~pending_risk_review_exists(Batch.job_id),
                    )
                )
            ).first()
            if card_context is None:
                raise LookupError("Unknown card")
            card, batch = card_context
            rows = await session.execute(
                select(Transcript.text)
                .join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(
                    JobFile.job_id == batch.job_id,
                    Transcript.risk_classified.is_(True),
                    Transcript.is_reliable.is_(True),
                )
                .order_by(JobFile.position, Transcript.segment_index)
            )
        return "\n".join(row[0] for row in rows), json.loads(card.payload_json)

    @staticmethod
    def _normalize_due_at(due_at: str | None) -> str | None:
        if due_at in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(due_at)
        except ValueError as exc:
            raise ValueError("Todo due date must be an ISO 8601 datetime") from exc
        if parsed.tzinfo is None:
            raise ValueError("Todo due date must include a timezone")
        return due_at

    @staticmethod
    def _todo_view(todo: Todo, *, now: datetime) -> dict[str, object]:
        return {
            "id": todo.id,
            "text": todo.text,
            "due_at": todo.due_at,
            "completed": todo.completed,
            "overdue": is_overdue(
                due_at=todo.due_at, completed=todo.completed, now=now
            ),
        }
