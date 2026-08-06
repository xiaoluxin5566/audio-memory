import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion, Card, Todo
from audio_memory.prompts.schemas import StrictTodoDraft
from audio_memory.prompts.store import PROMPT_SCENES


def migration_config(database_path: Path) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def seed_version_0002_database(database_path: Path) -> Config:
    config = migration_config(database_path)
    command.upgrade(config, "0002")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, provider_id, model_id, prompt_snapshot_json, "
            "staged_results_json, created_at, updated_at) VALUES "
            "('job-legacy', 'completed', 'kimi', 'moonshot-v1', "
            "'{\"meeting\": {\"content\": \"legacy prompt\"}}', '[]', "
            "'2026-08-01T08:00:00+00:00', '2026-08-01T08:10:00+00:00')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-legacy', 'job-legacy', 'kimi', 'moonshot-v1', "
            "'2026-08-01T08:10:00+00:00', '2026-08-01')"
        )
        connection.execute(
            "INSERT INTO job_files "
            "(id, job_id, original_name, extension, size_bytes, sha256, "
            "recording_time_source, speech_mapping_json, position, temporary_path) "
            "VALUES ('file-legacy', 'job-legacy', 'legacy.mp3', '.mp3', 11, "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'unknown', '[]', 0, '/app/audio/legacy.mp3')"
        )
        connection.execute(
            "INSERT INTO transcripts "
            "(id, job_file_id, segment_index, segment_uid, start_ms, end_ms, "
            "text, words_json) VALUES "
            "('transcript-legacy', 'file-legacy', 0, 'file-legacy:0', 0, 1000, "
            "'旧转写', '[]')"
        )
        connection.executemany(
            "INSERT INTO cards (id, batch_id, scene_id, position, payload_json) "
            "VALUES (?, 'batch-legacy', ?, ?, ?)",
            [
                ('card-legacy-1', 'meeting', 0, '{"title": "旧会议"}'),
                ('card-legacy-2', 'growth', 1, '{"title": "旧建议"}'),
            ],
        )
        connection.execute(
            "INSERT INTO qa_messages "
            "(id, card_id, role, content, position, created_at) VALUES "
            "('qa-legacy', 'card-legacy-1', 'user', '这是什么？', 0, "
            "'2026-08-01T09:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO todos "
            "(id, batch_id, source_card_id, text, due_at, completed, created_at) "
            "VALUES ('todo-legacy', 'batch-legacy', 'card-legacy-1', '跟进会议', "
            "NULL, 0, '2026-08-01T08:10:00+00:00')"
        )
        connection.commit()
    return config


def seed_version_0005_completed_database(database_path: Path) -> Config:
    config = migration_config(database_path)
    command.upgrade(config, "0005")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, provider_id, model_id, prompt_snapshot_json, "
            "staged_results_json, created_at, updated_at) VALUES "
            "('job-pre-0006', 'completed', 'kimi', 'model-old', '{}', '{}', "
            "'2026-08-01T08:00:00+00:00', '2026-08-01T08:10:00+00:00')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) VALUES "
            "('batch-pre-0006', 'job-pre-0006', 'kimi', 'model-old', "
            "'2026-08-01T08:10:00+00:00', '2026-08-01')"
        )
        connection.execute(
            "INSERT INTO analysis_versions "
            "(id, source_job_id, batch_id, provider_id, model_id, "
            "credential_generation, prompt_snapshot_json, profile_snapshot_json, "
            "fixed_rules_hash, staged_results_json, priority, status, created_at, "
            "completed_at) VALUES "
            "('version-pre-0006', 'job-pre-0006', 'batch-pre-0006', 'kimi', "
            "'model-old', 1, '{}', '[]', 'rules', '{}', 10, 'completed', "
            "'2026-08-01T08:00:00+00:00', '2026-08-01T08:10:00+00:00')"
        )
        connection.execute(
            "UPDATE batches SET current_analysis_version_id='version-pre-0006' "
            "WHERE id='batch-pre-0006'"
        )
        connection.execute(
            "INSERT INTO cards "
            "(id, batch_id, analysis_version_id, scene_id, position, payload_json) "
            "VALUES ('card-pre-0006', 'batch-pre-0006', 'version-pre-0006', "
            "'meeting', 0, '{}')"
        )
        connection.execute(
            "INSERT INTO todo_candidates "
            "(id, analysis_version_id, source_job_id, source_event_id, "
            "evidence_segment_ids_json, normalized_action, normalized_object, "
            "normalized_assignee, text, due_at, source_fingerprint) VALUES "
            "('candidate-pre-0006', 'version-pre-0006', 'job-pre-0006', "
            "'event_planning', '[\"seg-1\"]', 'send notes', 'meeting notes', "
            "'user', 'send notes', '2026-08-10T09:00:00+08:00', "
            "'candidate-fingerprint')"
        )
        connection.execute(
            "INSERT INTO todos "
            "(id, batch_id, analysis_version_id, source_job_id, source_event_id, "
            "evidence_segment_ids_json, normalized_action, normalized_object, "
            "normalized_assignee, source_fingerprint, user_edited, completion_source, "
            "text, due_at, completed, created_at) VALUES "
            "('todo-pre-0006', 'batch-pre-0006', 'version-pre-0006', "
            "'job-pre-0006', 'event_planning', '[\"seg-1\"]', 'send notes', "
            "'meeting notes', 'user', 'todo-fingerprint', 0, 'model', "
            "'send notes', '2026-08-10T09:00:00+08:00', 0, "
            "'2026-08-01T08:10:00+00:00')"
        )
        connection.commit()
    return config


@dataclass(frozen=True)
class PublicationResult:
    scene_id: str
    should_generate: bool = False
    todos: tuple = ()

    def model_dump_for_frontend(self) -> dict[str, object]:
        return {
            "scene_id": self.scene_id,
            "should_generate": self.should_generate,
            "cards": [],
            "todos": [],
        }


def complete_results_with_todo() -> list[PublicationResult]:
    draft = StrictTodoDraft(
        text="updated notes",
        action="send notes",
        object="meeting notes",
        owner_type="user",
        assignee_text="user",
        due_at="2026-08-10T09:00:00+08:00",
        due_text="Monday",
        intent_type="commitment",
        source_event_id="event_planning",
        source_context="explicit commitment",
        evidence_segment_ids=["seg-2"],
        confidence=0.9,
    )
    return [
        PublicationResult(
            scene_id,
            todos=(draft,) if scene_id == "todo" else (),
        )
        for scene_id in PROMPT_SCENES
    ]


def test_0003_backfills_one_current_version_without_copying_source_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    config = seed_version_0002_database(database_path)

    with sqlite3.connect(database_path) as connection:
        before_source_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("analysis_jobs", "job_files", "transcripts")
        }
        before_audio_paths = list(
            connection.execute("SELECT temporary_path FROM job_files ORDER BY id")
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "analysis_versions",
            "todo_candidates",
            "todo_tombstones",
            "profile_candidates",
            "reanalysis_batches",
            "reanalysis_items",
        } <= tables

        version = connection.execute(
            "SELECT id, source_job_id, batch_id, provider_id, model_id, "
            "prompt_snapshot_json, status, completed_at "
            "FROM analysis_versions WHERE batch_id = 'batch-legacy'"
        ).fetchone()
        assert version is not None
        version_id = version[0]
        assert version[1:] == (
            "job-legacy",
            "batch-legacy",
            "kimi",
            "moonshot-v1",
            '{"meeting": {"content": "legacy prompt"}}',
            "completed",
            "2026-08-01T08:10:00+00:00",
        )
        assert connection.execute(
            "SELECT current_analysis_version_id FROM batches "
            "WHERE id = 'batch-legacy'"
        ).fetchone() == (version_id,)
        assert connection.execute(
            "SELECT analysis_version_id FROM cards ORDER BY id"
        ).fetchall() == [(version_id,), (version_id,)]
        assert connection.execute(
            "SELECT card_id FROM qa_messages WHERE id = 'qa-legacy'"
        ).fetchone() == ("card-legacy-1",)
        assert connection.execute(
            "SELECT analysis_version_id, user_edited, source_fingerprint "
            "FROM todos WHERE id = 'todo-legacy'"
        ).fetchone() == (version_id, 1, "legacy:todo-legacy")

        after_source_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("analysis_jobs", "job_files", "transcripts")
        }
        after_audio_paths = list(
            connection.execute("SELECT temporary_path FROM job_files ORDER BY id")
        )
        assert after_source_counts == before_source_counts
        assert after_audio_paths == before_audio_paths


def test_head_normalizes_all_checkpoint_payloads_and_adds_queue_priority(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoint-shape.sqlite3"
    config = seed_version_0002_database(database_path)

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        analysis_job_payload = connection.execute(
            "SELECT staged_results_json FROM analysis_jobs WHERE id = 'job-legacy'"
        ).fetchone()
        version_payload = connection.execute(
            "SELECT staged_results_json, priority FROM analysis_versions "
            "WHERE source_job_id = 'job-legacy'"
        ).fetchone()
        assert analysis_job_payload == ("{}",)
        assert version_payload == ("{}", 10)


def test_0005_adds_durable_generation_and_worker_lease_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "durable-owner.sqlite3"
    config = seed_version_0002_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO provider_metadata "
            "(provider_id, active, validation_status, default_model_id) "
            "VALUES ('kimi', 1, 'available', 'kimi-k2.5')"
        )
        connection.commit()
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        provider_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(provider_metadata)")
        }
        version_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(analysis_versions)")
        }
        assert provider_columns["credential_generation"][3] == 1
        assert {
            "worker_owner_id",
            "lease_expires_at",
        } <= version_columns.keys()
        assert connection.execute(
            "SELECT credential_generation FROM provider_metadata ORDER BY provider_id"
        ).fetchall() == [(0,)]

    command.downgrade(config, "0004")

    with sqlite3.connect(database_path) as connection:
        assert "credential_generation" not in {
            row[1] for row in connection.execute("PRAGMA table_info(provider_metadata)")
        }
        version_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(analysis_versions)")
        }
        assert "worker_owner_id" not in version_columns
        assert "lease_expires_at" not in version_columns


def test_0006_adds_immutable_publication_counts_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "published-outcome.sqlite3"
    config = seed_version_0005_completed_database(database_path)

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        version_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(analysis_versions)")
        }
        assert {
            "published_card_count",
            "published_todo_count",
        } <= version_columns.keys()
        assert connection.execute(
            "SELECT published_card_count, published_todo_count "
            "FROM analysis_versions WHERE id='version-pre-0006'"
        ).fetchone() == (1, 1)

    command.downgrade(config, "0005")

    with sqlite3.connect(database_path) as connection:
        version_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(analysis_versions)")
        }
        assert "published_card_count" not in version_columns
        assert "published_todo_count" not in version_columns
        assert connection.execute(
            "SELECT source_job_id, batch_id, provider_id, model_id, status "
            "FROM analysis_versions WHERE id='version-pre-0006'"
        ).fetchone() == (
            "job-pre-0006",
            "batch-pre-0006",
            "kimi",
            "model-old",
            "completed",
        )
        assert connection.execute(
            "SELECT id, analysis_version_id FROM cards"
        ).fetchall() == [("card-pre-0006", "version-pre-0006")]
        assert connection.execute(
            "SELECT id, text, analysis_version_id FROM todos"
        ).fetchall() == [
            ("todo-pre-0006", "send notes", "version-pre-0006")
        ]


@pytest.mark.asyncio
async def test_migrated_completed_outcome_stays_fixed_after_later_publication(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "frozen-published-outcome.sqlite3"
    config = seed_version_0005_completed_database(database_path)
    command.upgrade(config, "head")
    database = Database(database_path)
    publisher = VersionPublisher(database)

    async with database.session() as session:
        session.add(
            AnalysisVersion(
                id="version-post-0006",
                source_job_id="job-pre-0006",
                batch_id="batch-pre-0006",
                provider_id="kimi",
                model_id="model-new",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                status="running",
            )
        )
        await session.commit()

    await publisher.publish(
        "version-post-0006", complete_results_with_todo(), []
    )
    async with database.session() as session:
        old_card = await session.get(Card, "card-pre-0006")
        assert old_card is not None
        await session.delete(old_card)
        await session.commit()

    old_outcome = await publisher.publish(
        "version-pre-0006", complete_results_with_todo(), []
    )

    async with database.session() as session:
        todo = await session.scalar(select(Todo))
    assert todo is not None
    assert todo.analysis_version_id == "version-post-0006"
    assert old_outcome.card_count == 1
    assert old_outcome.todo_count == 1
    await database.dispose()


def test_0003_downgrade_restores_0002_data_and_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.sqlite3"
    config = seed_version_0002_database(database_path)
    command.upgrade(config, "head")

    command.downgrade(config, "0002")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "analysis_versions" not in tables
        assert "todo_candidates" not in tables
        assert "current_analysis_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        assert "analysis_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(cards)")
        }
        assert connection.execute(
            "SELECT id FROM cards ORDER BY id"
        ).fetchall() == [("card-legacy-1",), ("card-legacy-2",)]
        assert connection.execute(
            "SELECT card_id FROM qa_messages WHERE id = 'qa-legacy'"
        ).fetchone() == ("card-legacy-1",)
        assert connection.execute(
            "SELECT id, text FROM todos"
        ).fetchone() == ("todo-legacy", "跟进会议")


def test_0003_backfills_exactly_one_version_for_each_legacy_batch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "multiple-batches.sqlite3"
    config = seed_version_0002_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, provider_id, model_id, prompt_snapshot_json, "
            "staged_results_json, created_at, updated_at) VALUES "
            "('job-no-prompt', 'completed', 'openai', 'gpt-5', '', '[]', "
            "'2026-07-31T08:00:00+00:00', '2026-07-31T08:10:00+00:00')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-no-prompt', 'job-no-prompt', 'openai', 'gpt-5', "
            "'2026-07-31T08:10:00+00:00', '2026-07-31')"
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        versions_per_batch = connection.execute(
            "SELECT batch_id, count(*) FROM analysis_versions "
            "GROUP BY batch_id ORDER BY batch_id"
        ).fetchall()
        assert versions_per_batch == [
            ("batch-legacy", 1),
            ("batch-no-prompt", 1),
        ]
        assert connection.execute(
            "SELECT prompt_snapshot_json FROM analysis_versions "
            "WHERE batch_id = 'batch-no-prompt'"
        ).fetchone() == ("{}",)


def test_0003_rejects_orphan_batch_before_any_schema_or_data_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "orphan-batch.sqlite3"
    config = seed_version_0002_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-orphan-z', 'job-missing-z', NULL, NULL, 'now', '2026-08-05')"
        )
        connection.execute(
            "INSERT INTO batches "
            "(id, job_id, provider_id, model_id, uploaded_at, natural_date) "
            "VALUES ('batch-orphan-a', 'job-missing-a', NULL, NULL, 'now', '2026-08-05')"
        )
        connection.commit()

    with pytest.raises(
        RuntimeError,
        match=r"orphan.*batch-orphan-a.*batch-orphan-z",
    ):
        command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "analysis_versions" not in tables
        assert "current_analysis_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0002",)
        assert connection.execute(
            "SELECT id FROM batches ORDER BY id"
        ).fetchall() == [
            ("batch-legacy",),
            ("batch-orphan-a",),
            ("batch-orphan-z",),
        ]
