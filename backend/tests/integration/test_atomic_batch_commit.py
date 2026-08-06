from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.analysis.profile import ProfileDelta
from audio_memory.config import AppPaths
from audio_memory.content.service import ContentService
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    ProfileCandidate,
    ProfileFact,
    QAMessage,
    ReanalysisBatch,
    ReanalysisItem,
    Todo,
    TodoCandidate,
)
from audio_memory.prompts.schemas import StrictTodoDraft
from audio_memory.prompts.store import PROMPT_SCENES
from audio_memory.repositories import BatchRepository


@dataclass(frozen=True)
class PublicationResult:
    scene_id: str
    should_generate: bool = False
    todos: tuple = ()

    def model_dump_for_frontend(self) -> dict[str, object]:
        return {
            "scene_id": self.scene_id,
            "should_generate": self.should_generate,
            "cards": ([{"title": f"new {self.scene_id}"}] if self.should_generate else []),
            "todos": [],
        }


class StaticAnswerer:
    async def answer(self, **kwargs) -> str:
        return "new answer"


class FailingProfileRebuilder:
    async def rebuild(self, current_versions):
        raise RuntimeError("profile rebuild failed")

    async def swap_active(self, facts) -> None:
        raise AssertionError("swap must not run after rebuild failure")


def complete_results(*visible: str) -> list[PublicationResult]:
    return [
        PublicationResult(scene_id, should_generate=scene_id in visible)
        for scene_id in PROMPT_SCENES
    ]


async def seed_reanalysis(database: Database, paths: AppPaths) -> None:
    source = paths.audio / "source" / "file-1.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"one audio")
    async with database.session() as session:
        session.add(
            AnalysisJob(
                id="job-versioned",
                stage="completed",
                provider_id="mutable-old-provider",
                model_id="mutable-old-model",
                prompt_snapshot_json='{"mutable":"old"}',
            )
        )
        session.add(
            Batch(
                id="batch-versioned",
                job_id="job-versioned",
                provider_id="old-provider",
                model_id="old-model",
                natural_date="2026-08-01",
                uploaded_at="2026-08-01T08:00:00+00:00",
            )
        )
        session.add(
            JobFile(
                id="file-versioned",
                job_id="job-versioned",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=9,
                sha256="a" * 64,
                position=0,
                temporary_path=str(source),
            )
        )
        await session.flush()
        session.add_all(
            [
                AnalysisVersion(
                    id="version-old",
                    source_job_id="job-versioned",
                    batch_id="batch-versioned",
                    provider_id="old-provider",
                    model_id="old-model",
                    credential_generation=1,
                    prompt_snapshot_json='{"meeting":{"content":"old prompt"}}',
                    profile_snapshot_json="[]",
                    fixed_rules_hash="rules",
                    staged_results_json="{}",
                    status="completed",
                    completed_at="2026-08-01T08:10:00+00:00",
                ),
                AnalysisVersion(
                    id="version-new",
                    source_job_id="job-versioned",
                    batch_id="batch-versioned",
                    provider_id="new-provider",
                    model_id="new-model",
                    credential_generation=2,
                    prompt_snapshot_json='{"meeting":{"content":"new prompt"}}',
                    profile_snapshot_json="[]",
                    fixed_rules_hash="rules",
                    staged_results_json="{}",
                    status="running",
                ),
            ]
        )
        await session.flush()
        batch = await session.get(Batch, "batch-versioned")
        assert batch is not None
        batch.current_analysis_version_id = "version-old"
        session.add_all(
            [
                Card(
                    id="card-old-meeting",
                    batch_id="batch-versioned",
                    analysis_version_id="version-old",
                    scene_id="meeting",
                    position=0,
                    payload_json=json.dumps({"title": "old meeting"}),
                ),
                Card(
                    id="card-old-content",
                    batch_id="batch-versioned",
                    analysis_version_id="version-old",
                    scene_id="content",
                    position=1,
                    payload_json=json.dumps({"title": "old content"}),
                ),
                QAMessage(
                    id="qa-old",
                    card_id="card-old-meeting",
                    role="user",
                    content="old question",
                    position=0,
                ),
            ]
        )
        await session.commit()


async def make_history_item_with_profile_candidate(database: Database) -> None:
    async with database.session() as session:
        session.add(
            ReanalysisBatch(
                id="history-1",
                status="running",
                provider_id="new-provider",
                model_id="new-model",
                credential_generation=2,
                prompt_snapshot_json="{}",
                profile_snapshot_json='[{"dimension":"old"}]',
                fixed_rules_hash="rules",
                snapshot_hash="snapshot",
            )
        )
        await session.flush()
        version = await session.get(AnalysisVersion, "version-new")
        assert version is not None
        version.reanalysis_batch_id = "history-1"
        session.add(
            ReanalysisItem(
                id="history-item-1",
                reanalysis_batch_id="history-1",
                source_batch_id="batch-versioned",
                analysis_version_id="version-new",
                position=0,
                status="running",
            )
        )
        session.add_all(
            [
                ProfileCandidate(
                    id="profile-candidate-new",
                    analysis_version_id="version-new",
                    subject_id="user",
                    dimension="role",
                    value_json='{"name":"new"}',
                    confidence=0.95,
                    evidence_segment_ids_json='["seg_0_0"]',
                    origin="explicit",
                ),
                ProfileFact(
                    id="profile-old",
                    subject_id="user",
                    dimension="role",
                    value_json='{"name":"old"}',
                    confidence=0.8,
                    source_audio_json='["job-old"]',
                    first_seen_at="2026-07-01T00:00:00+00:00",
                    last_seen_at="2026-07-01T00:00:00+00:00",
                    evidence_count=1,
                    origin="explicit",
                    status="active",
                ),
            ]
        )
        await session.commit()


@pytest.fixture
def meeting_card() -> dict[str, object]:
    return {
        "id": "card-meeting",
        "scene_id": "meeting",
        "position": 0,
        "payload": {"title": "产品评审", "summary": "确认第一阶段范围"},
    }


@pytest.mark.asyncio
async def test_create_job_rejects_unknown_stage(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-stage.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        with pytest.raises(ValueError, match="Unsupported job stage"):
            await repository.create_job(stage="nonsense")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_uncommitted_batch_is_absent_from_feed(
    tmp_path: Path,
    meeting_card: dict[str, object],
) -> None:
    database = Database(tmp_path / "draft.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        job = await repository.create_job(stage="analyzing")
        await repository.stage_card(job.id, meeting_card)

        assert await repository.list_feed_batches() == []
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_publish_batch_exposes_all_staged_cards_atomically(
    tmp_path: Path,
    meeting_card: dict[str, object],
) -> None:
    database = Database(tmp_path / "publish.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        job = await repository.create_job(stage="ready_to_commit")
        await repository.stage_card(job.id, meeting_card)

        batch = await repository.publish_batch(job.id, batch_id="batch-1")

        assert batch.id == "batch-1"
        assert batch.card_count == 1
        feed = await repository.list_feed_batches()
        assert [(item.id, item.card_count) for item in feed] == [("batch-1", 1)]
        persisted_job = await repository.get_job(job.id)
        assert persisted_job.stage == "completed"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_publish_failure_rolls_back_batch_cards_and_job_state(
    tmp_path: Path,
    meeting_card: dict[str, object],
) -> None:
    database = Database(tmp_path / "rollback.sqlite3")
    await database.create_schema()
    repository = BatchRepository(database)

    try:
        job = await repository.create_job(stage="ready_to_commit")
        await repository.stage_card(job.id, meeting_card)
        await repository.stage_card(job.id, meeting_card)

        with pytest.raises(Exception):
            await repository.publish_batch(job.id, batch_id="batch-rollback")

        assert await repository.list_feed_batches() == []
        persisted_job: AnalysisJob = await repository.get_job(job.id)
        assert persisted_job.stage == "ready_to_commit"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_new_version_replaces_feed_atomically_and_preserves_old_qa(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)

    outcome = await VersionPublisher(database, paths).publish(
        "version-new", complete_results("meeting"), []
    )
    feed = await ContentService(database, paths, StaticAnswerer()).feed()
    new_card_id = feed["days"][0]["cards"][0]["id"]

    assert outcome.card_count == 1
    assert [card["id"] for card in feed["days"][0]["cards"]] == [new_card_id]
    assert new_card_id != "card-old-meeting"
    assert feed["days"][0]["cards"][0]["qa"] == []
    async with database.session() as session:
        assert await session.get(Card, "card-old-meeting") is not None
        old_qa = list(
            await session.scalars(
                select(QAMessage).where(QAMessage.card_id == "card-old-meeting")
            )
        )
        batch = await session.get(Batch, "batch-versioned")
        stored_file = await session.get(JobFile, "file-versioned")
    assert [message.id for message in old_qa] == ["qa-old"]
    assert batch is not None and batch.current_analysis_version_id == "version-new"
    assert stored_file is not None
    assert stored_file.temporary_path.endswith("source/file-1.mp3")
    await database.dispose()


@pytest.mark.asyncio
async def test_old_card_remains_queryable_and_accepts_version_scoped_qa(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)
    publisher = VersionPublisher(database, paths)
    await publisher.publish("version-new", complete_results("meeting"), [])
    service = ContentService(database, paths, StaticAnswerer())

    messages = await service.ask("card-old-meeting", "follow up")

    assert messages == [
        {"role": "user", "content": "old question"},
        {"role": "user", "content": "follow up"},
        {"role": "assistant", "content": "new answer"},
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_should_generate_false_removes_scene_only_from_current_feed(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)

    await VersionPublisher(database, paths).publish(
        "version-new", complete_results(), []
    )

    feed = await ContentService(database, paths, StaticAnswerer()).feed()
    assert feed["days"] == []
    async with database.session() as session:
        old_cards = int(
            await session.scalar(
                select(func.count(Card.id)).where(
                    Card.analysis_version_id == "version-old"
                )
            )
            or 0
        )
    assert old_cards == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_incomplete_scene_set_leaves_old_version_visible_without_partial_cards(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)
    incomplete = complete_results("meeting")[:-1]

    with pytest.raises(ValueError, match="six scenes"):
        await VersionPublisher(database, paths).publish(
            "version-new", incomplete, []
        )

    async with database.session() as session:
        batch = await session.get(Batch, "batch-versioned")
        partial = int(
            await session.scalar(
                select(func.count(Card.id)).where(
                    Card.analysis_version_id == "version-new"
                )
            )
            or 0
        )
    assert batch is not None and batch.current_analysis_version_id == "version-old"
    assert partial == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_last_history_item_swaps_profile_after_content_publication(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)
    await make_history_item_with_profile_candidate(database)

    await VersionPublisher(database, paths).publish(
        "version-new", complete_results(), []
    )

    async with database.session() as session:
        history = await session.get(ReanalysisBatch, "history-1")
        item = await session.get(ReanalysisItem, "history-item-1")
        batch = await session.get(Batch, "batch-versioned")
        facts = list(await session.scalars(select(ProfileFact)))
    assert history is not None and history.status == "completed"
    assert item is not None and item.status == "succeeded"
    assert batch is not None and batch.current_analysis_version_id == "version-new"
    assert [(fact.dimension, json.loads(fact.value_json)) for fact in facts] == [
        ("role", {"name": "new"})
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_profile_failure_keeps_old_profile_and_marks_content_completed(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)
    await make_history_item_with_profile_candidate(database)

    await VersionPublisher(
        database,
        paths,
        profile_rebuilder=FailingProfileRebuilder(),
    ).publish("version-new", complete_results(), [])

    async with database.session() as session:
        history = await session.get(ReanalysisBatch, "history-1")
        batch = await session.get(Batch, "batch-versioned")
        facts = list(await session.scalars(select(ProfileFact)))
    assert history is not None
    assert history.status == "content_completed_profile_failed"
    assert batch is not None and batch.current_analysis_version_id == "version-new"
    assert [fact.id for fact in facts] == ["profile-old"]
    await database.dispose()


@pytest.mark.asyncio
async def test_profile_only_retry_rebuilds_without_republishing_content(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_reanalysis(database, paths)
    await make_history_item_with_profile_candidate(database)
    await VersionPublisher(
        database,
        paths,
        profile_rebuilder=FailingProfileRebuilder(),
    ).publish("version-new", complete_results("meeting"), [])
    async with database.session() as session:
        card_count_before = int(
            await session.scalar(select(func.count(Card.id))) or 0
        )

    await VersionPublisher(database, paths).retry_profile("history-1")

    async with database.session() as session:
        history = await session.get(ReanalysisBatch, "history-1")
        card_count_after = int(
            await session.scalar(select(func.count(Card.id))) or 0
        )
        facts = list(await session.scalars(select(ProfileFact)))
    assert history is not None and history.status == "completed"
    assert card_count_after == card_count_before
    assert [(fact.dimension, json.loads(fact.value_json)) for fact in facts] == [
        ("role", {"name": "new"})
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_first_publication_preserves_profile_and_adds_verified_delta(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "new-upload-profile.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-new-upload", stage="analyzing"))
        session.add(
            AnalysisVersion(
                id="version-new-upload",
                source_job_id="job-new-upload",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="running",
            )
        )
        session.add(
            ProfileFact(
                id="profile-existing",
                subject_id="user",
                dimension="role",
                value_json='{"name":"existing"}',
                confidence=0.8,
                source_audio_json='["job-existing"]',
                first_seen_at="2026-07-01T00:00:00+00:00",
                last_seen_at="2026-07-01T00:00:00+00:00",
                evidence_count=1,
                origin="explicit",
                status="active",
            )
        )
        await session.commit()

    await VersionPublisher(database).publish(
        "version-new-upload",
        complete_results(),
        [
            ProfileDelta(
                subject_id="user",
                dimension="interest",
                value={"topic": "AI"},
                confidence=0.9,
                explicit=True,
            )
        ],
    )

    async with database.session() as session:
        facts = list(
            await session.scalars(select(ProfileFact).order_by(ProfileFact.dimension))
        )
    assert [(fact.dimension, json.loads(fact.value_json)) for fact in facts] == [
        ("interest", {"topic": "AI"}),
        ("role", {"name": "existing"}),
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_retry_version_recovers_audio_moved_before_failed_publication(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    staged = paths.staging / "retry.mp3"
    staged.write_bytes(b"audio")
    async with database.session() as session:
        session.add(AnalysisJob(id="job-retry-move", stage="analyzing"))
        session.add(
            JobFile(
                id="file-retry-move",
                job_id="job-retry-move",
                original_name="retry.mp3",
                extension=".mp3",
                size_bytes=5,
                sha256="f" * 64,
                position=0,
                temporary_path=str(staged),
            )
        )
        session.add(
            AnalysisVersion(
                id="version-failed-move",
                source_job_id="job-retry-move",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="running",
            )
        )
        await session.commit()
    invalid_delta = ProfileDelta(
        subject_id=None,  # type: ignore[arg-type]
        dimension="role",
        value={"name": "invalid"},
        confidence=0.9,
        explicit=True,
    )

    with pytest.raises(IntegrityError):
        await VersionPublisher(database, paths).publish(
            "version-failed-move", complete_results(), [invalid_delta]
        )

    async with database.session() as session:
        failed = await session.get(AnalysisVersion, "version-failed-move")
        assert failed is not None
        failed.status = "failed"
        session.add(
            AnalysisVersion(
                id="version-retry-move",
                source_job_id="job-retry-move",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="running",
            )
        )
        await session.commit()

    outcome = await VersionPublisher(database, paths).publish(
        "version-retry-move", complete_results(), []
    )

    async with database.session() as session:
        stored_file = await session.get(JobFile, "file-retry-move")
        batches = list(await session.scalars(select(Batch)))
    assert outcome.batch_id == batches[0].id
    assert stored_file is not None
    assert Path(stored_file.temporary_path).read_bytes() == b"audio"
    assert len(batches) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_publication_keeps_same_source_todos_with_incompatible_deadlines(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "todo-candidates.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-todo-candidates", stage="analyzing"))
        session.add(
            AnalysisVersion(
                id="version-todo-candidates",
                source_job_id="job-todo-candidates",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="running",
            )
        )
        await session.commit()

    def draft(text: str, due_at: str) -> StrictTodoDraft:
        return StrictTodoDraft(
            text=text,
            action="send notes",
            owner_type="user",
            assignee_text="user",
            due_at=due_at,
            due_text=due_at,
            intent_type="commitment",
            source_event_id="event_planning",
            source_context="explicit commitment",
            evidence_segment_ids=["seg_0_0"],
            confidence=0.9,
        )

    results = complete_results()
    results[0] = PublicationResult(
        "todo",
        should_generate=True,
        todos=(
            draft("send notes Monday", "2026-08-10T09:00:00+08:00"),
            draft("send notes Wednesday", "2026-08-12T09:00:00+08:00"),
        ),
    )

    outcome = await VersionPublisher(database).publish(
        "version-todo-candidates", results, []
    )

    async with database.session() as session:
        candidates = list(await session.scalars(select(TodoCandidate)))
        todos = list(await session.scalars(select(Todo)))
    assert outcome.todo_count == 2
    assert len(candidates) == 2
    assert {candidate.due_at[:10] for candidate in candidates} == {
        "2026-08-10",
        "2026-08-12",
    }
    assert len(todos) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_publication_keeps_same_action_todos_for_different_objects(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "todo-objects.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-todo-objects", stage="analyzing"))
        session.add(
            AnalysisVersion(
                id="version-todo-objects",
                source_job_id="job-todo-objects",
                provider_id="kimi",
                model_id="model",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="running",
            )
        )
        await session.commit()

    def draft(text: str, object_text: str) -> StrictTodoDraft:
        return StrictTodoDraft(
            text=text,
            action="send",
            object=object_text,
            owner_type="user",
            assignee_text="user",
            due_at="2026-08-10T09:00:00+08:00",
            due_text="Monday",
            intent_type="commitment",
            source_event_id="event_planning",
            source_context="explicit commitment",
            evidence_segment_ids=["seg_0_0"],
            confidence=0.9,
        )

    results = complete_results()
    results[0] = PublicationResult(
        "todo",
        should_generate=True,
        todos=(
            draft("send meeting notes", "meeting notes"),
            draft("send budget report", "budget report"),
        ),
    )

    await VersionPublisher(database).publish(
        "version-todo-objects", results, []
    )

    async with database.session() as session:
        candidates = list(await session.scalars(select(TodoCandidate)))
        todos = list(await session.scalars(select(Todo)))
    assert {candidate.normalized_object for candidate in candidates} == {
        "meeting notes",
        "budget report",
    }
    assert len(todos) == 2
    await database.dispose()
