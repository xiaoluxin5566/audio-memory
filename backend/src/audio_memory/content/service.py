from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select

from audio_memory.config import AppPaths
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
        self, database: Database, paths: AppPaths, answerer: QuestionAnswerer
    ) -> None:
        self.database = database
        self.paths = paths
        self.answerer = answerer

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
                        select(Batch, Card)
                        .join(
                            Card,
                            (Card.batch_id == Batch.id)
                            & (
                                Card.analysis_version_id
                                == Batch.current_analysis_version_id
                            ),
                        )
                        .where(~pending_risk_review_exists(Batch.job_id))
                        .order_by(Batch.uploaded_at.desc(), Card.position)
                    )
                ).all()
            )
            card_ids = [card.id for _, card in rows]
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
        for batch, card in rows:
            days[batch.natural_date].append(
                {
                    "id": card.id,
                    "batch_id": batch.id,
                    "scene_id": card.scene_id,
                    "uploaded_at": batch.uploaded_at,
                    "payload": json.loads(card.payload_json),
                    "qa": qa_by_card[card.id],
                }
            )
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
