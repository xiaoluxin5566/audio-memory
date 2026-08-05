from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.orchestrator import AnalysisOrchestrator
from audio_memory.analysis.publisher import AnalysisPublisher
from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, Card, JobFile, ProfileFact, Todo, Transcript
from audio_memory.prompts.schemas import CardShell, SceneResult, TodoDraft
from audio_memory.prompts.store import PromptStore


class FakeSceneAnalyzer:
    def __init__(self, *, fail_scene: str | None = None) -> None:
        self.fail_scene = fail_scene
        self.calls: list[tuple[str, str]] = []

    async def analyze(self, scene_id, request, provider_snapshot):
        self.calls.append((scene_id, provider_snapshot["provider_id"]))
        if scene_id == self.fail_scene:
            raise RuntimeError("provider failed")
        if scene_id == "todo":
            return SceneResult(
                scene_id="todo",
                should_generate=True,
                card=CardShell(title="待办", summary="回复客户"),
                todos=[TodoDraft(text="回复客户邮件")],
                confidence=0.9,
            )
        if scene_id == "meeting":
            return SceneResult(
                scene_id="meeting",
                should_generate=True,
                card=CardShell(title="项目评审", summary="确认一期范围"),
                confidence=0.9,
            )
        return SceneResult(
            scene_id=scene_id,
            should_generate=False,
            card=None,
            confidence=0.8,
        )


class FakeProfileExtractor:
    async def extract(self, transcript, existing, provider_snapshot):
        return [
            {
                "subject_id": "user",
                "dimension": "work_focus",
                "value": {"topic": "Always-on 产品"},
                "confidence": 0.85,
                "explicit": False,
            }
        ]


async def seed_transcribed_job(database: Database, tmp_path: Path) -> str:
    job_id = str(uuid4())
    file_id = str(uuid4())
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id=job_id,
                stage=JobStage.ANALYZING.value,
                provider_id="kimi",
                model_id="kimi-k2.5",
            )
        )
        session.add(
            JobFile(
                id=file_id,
                job_id=job_id,
                original_name="meeting.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="b" * 64,
                duration_ms=2000,
                position=0,
                temporary_path=str(tmp_path / "meeting.mp3"),
            )
        )
        session.add(
            Transcript(
                id=str(uuid4()),
                job_file_id=file_id,
                segment_index=0,
                start_ms=0,
                end_ms=2000,
                text="我们决定第一期先做 macOS。提醒我下午回复客户邮件。",
                words_json="[]",
            )
        )
        await session.commit()
    return job_id


@pytest.mark.asyncio
async def test_pipeline_omits_empty_cards_and_publishes_todos_separately(tmp_path: Path) -> None:
    database = Database(tmp_path / "analysis.sqlite3")
    await database.create_schema()
    job_id = await seed_transcribed_job(database, tmp_path)
    analyzer = FakeSceneAnalyzer()
    orchestrator = AnalysisOrchestrator(
        database=database,
        prompt_store=PromptStore(tmp_path / "prompts"),
        analyzer=analyzer,
        profile_extractor=FakeProfileExtractor(),
        publisher=AnalysisPublisher(database),
    )

    outcome = await orchestrator.run(
        job_id, {"provider_id": "kimi", "model_id": "kimi-k2.5"}
    )

    async with database.session() as session:
        card_scenes = list(
            await session.scalars(select(Card.scene_id).order_by(Card.position))
        )
        todo_count = await session.scalar(select(func.count(Todo.id)))
        profile_count = await session.scalar(select(func.count(ProfileFact.id)))
    assert outcome.card_count == 1
    assert card_scenes == ["meeting"]
    assert todo_count == 1
    assert profile_count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_provider_failure_keeps_transcript_for_provider_switch_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "retry.sqlite3")
    await database.create_schema()
    job_id = await seed_transcribed_job(database, tmp_path)
    failing = FakeSceneAnalyzer(fail_scene="parenting")
    orchestrator = AnalysisOrchestrator(
        database=database,
        prompt_store=PromptStore(tmp_path / "prompts"),
        analyzer=failing,
        profile_extractor=FakeProfileExtractor(),
        publisher=AnalysisPublisher(database),
    )

    with pytest.raises(RuntimeError):
        await orchestrator.run(job_id, {"provider_id": "kimi", "model_id": "kimi-k2.5"})
    failing.fail_scene = None
    await orchestrator.run(
        job_id, {"provider_id": "deepseek", "model_id": "deepseek-v4-flash"}
    )

    async with database.session() as session:
        transcript_count = await session.scalar(select(func.count(Transcript.id)))
        job = await session.get(AnalysisJob, job_id)
    assert transcript_count == 1
    assert job.stage == JobStage.COMPLETED.value
    assert job.provider_id == "deepseek"
    assert len(failing.calls) > 6
    await database.dispose()
