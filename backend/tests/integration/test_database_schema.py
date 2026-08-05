import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database, run_migrations
from audio_memory.models import ProviderMetadata


def migration_config(database_path: Path) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def seed_v1_transcript(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO analysis_jobs "
            "(id, stage, prompt_snapshot_json, staged_results_json, created_at, updated_at) "
            "VALUES ('job-1', 'transcribing', '{}', '[]', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO job_files "
            "(id, job_id, original_name, extension, size_bytes, sha256, position, temporary_path) "
            "VALUES ('file-1', 'job-1', 'a.mp3', '.mp3', 10, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0, '/tmp/a.mp3')"
        )
        connection.execute(
            "INSERT INTO transcripts "
            "(id, job_file_id, segment_index, start_ms, end_ms, text, words_json) "
            "VALUES ('transcript-1', 'file-1', 7, 0, 1000, '你好', '[]')"
        )
        connection.commit()


def test_initial_migration_creates_all_phase_one_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "audio-memory.sqlite3"

    run_migrations(database_path)
    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "provider_metadata",
        "analysis_jobs",
        "job_files",
        "transcripts",
        "batches",
        "cards",
        "todos",
        "qa_messages",
        "profile_facts",
        "prompt_versions",
        "temp_file_manifest",
        "feedback_index",
    }.issubset(tables)


def test_structured_transcript_migration_adds_required_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "structured.sqlite3"

    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        transcript_column_rows = list(
            connection.execute("PRAGMA table_info(transcripts)")
        )
        transcript_columns = {row[1] for row in transcript_column_rows}
        transcript_not_null = {row[1]: row[3] for row in transcript_column_rows}
        job_file_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(job_files)")
        }
        job_file_not_null = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(job_files)")
        }

    assert {"segment_uid", "speaker_id"} <= transcript_columns
    assert transcript_not_null["segment_uid"] == 1
    assert {
        "recording_started_at",
        "recording_time_source",
        "timezone",
        "speech_mapping_json",
    } <= job_file_columns
    assert job_file_not_null["recording_time_source"] == 1
    assert job_file_not_null["speech_mapping_json"] == 1


def test_structured_transcript_migration_backfills_stable_segment_uid(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "backfill.sqlite3"
    config = migration_config(database_path)
    command.upgrade(config, "0001")
    seed_v1_transcript(database_path)

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        segment_uid, recording_time_source, speech_mapping_json = connection.execute(
            "SELECT transcripts.segment_uid, job_files.recording_time_source, "
            "job_files.speech_mapping_json "
            "FROM transcripts JOIN job_files ON job_files.id = transcripts.job_file_id "
            "WHERE transcripts.id = 'transcript-1'"
        ).fetchone()
    assert segment_uid == "file-1:7"
    assert recording_time_source == "unknown"
    assert speech_mapping_json == "[]"


def test_structured_transcript_segment_uid_is_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "unique.sqlite3"
    config = migration_config(database_path)
    command.upgrade(config, "0001")
    seed_v1_transcript(database_path)
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO transcripts "
                "(id, job_file_id, segment_index, segment_uid, start_ms, end_ms, text, words_json) "
                "VALUES ('transcript-2', 'file-1', 8, 'file-1:7', 1000, 2000, '世界', '[]')"
            )


@pytest.mark.asyncio
async def test_only_one_provider_can_be_active(tmp_path: Path) -> None:
    database = Database(tmp_path / "active.sqlite3")
    await database.create_schema()

    try:
        async with database.session() as session:
            session.add_all(
                [
                    ProviderMetadata(provider_id="kimi", active=True),
                    ProviderMetadata(provider_id="openai", active=True),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await database.dispose()
