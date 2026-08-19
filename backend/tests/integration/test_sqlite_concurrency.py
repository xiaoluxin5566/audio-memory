from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from audio_memory.db import Database


@pytest.mark.asyncio
async def test_second_writer_waits_for_short_lock_instead_of_failing(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "writer-contention.sqlite3")
    await database.create_schema()

    async with database.session() as blocker:
        await blocker.execute(text("BEGIN IMMEDIATE"))
        await blocker.execute(
            text(
                "INSERT INTO analysis_jobs "
                "(id, stage, prompt_snapshot_json, staged_results_json, "
                "created_at, updated_at) "
                "VALUES ('lock-holder', 'transcribing', '{}', '{}', 'now', 'now')"
            )
        )

        writer_started = asyncio.Event()

        async def write_after_wait() -> None:
            async with database.session() as writer:
                writer_started.set()
                await writer.execute(text("BEGIN IMMEDIATE"))
                await writer.execute(
                    text(
                        "INSERT INTO analysis_jobs "
                        "(id, stage, prompt_snapshot_json, staged_results_json, "
                        "created_at, updated_at) "
                        "VALUES ('waited-writer', 'transcribing', '{}', '{}', "
                        "'now', 'now')"
                    )
                )
                await writer.commit()

        waiting_writer = asyncio.create_task(write_after_wait())
        await writer_started.wait()
        await asyncio.sleep(0.05)
        assert not waiting_writer.done()
        await blocker.commit()
        await asyncio.wait_for(waiting_writer, timeout=1)

    async with database.session() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM analysis_jobs "
                "WHERE id IN ('lock-holder', 'waited-writer')"
            )
        )
    assert count == 2
    await database.dispose()
