import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from audio_memory.db import Database, run_migrations
from audio_memory.models import ProviderMetadata


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
