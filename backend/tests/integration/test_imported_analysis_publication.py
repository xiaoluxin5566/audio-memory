from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.result_import import import_latest_analysis
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    ProfileFact,
    Todo,
    Transcript,
)


def valid_payload() -> dict[str, object]:
    return {
        "status": "complete",
        "cards": [
            {
                "card_kind": "insight",
                "scene_types": ["work_conversation"],
                "title": "目标不清正在消耗执行力",
                "summary": "这是一个跨片段洞察。",
                "time_range": {"start": None, "end": None},
                "findings": [
                    {
                        "type": "fact",
                        "content": "录音明确提到目标不清。",
                        "confidence": "high",
                        "evidence_segment_ids": ["seg_0_1"],
                    }
                ],
                "analysis": [],
                "quotes": [
                    {
                        "quote": "目标一直没有明确下来",
                        "why_it_matters": "这是直接证据。",
                        "evidence_segment_ids": ["seg_0_1"],
                    }
                ],
                "actions": [],
            }
        ],
    }


async def seed(database: Database) -> None:
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="completed"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="recording.mp3",
                extension=".mp3",
                size_bytes=100,
                sha256="a" * 64,
                duration_ms=1000,
                position=0,
                temporary_path="/tmp/recording.mp3",
            )
        )
        session.add(
            Transcript(
                id="transcript-1",
                job_file_id="file-1",
                segment_index=1,
                start_ms=0,
                end_ms=1000,
                text="目标一直没有明确下来",
                words_json="[]",
                risk_classified=True,
            )
        )
        session.add(
            AnalysisVersion(
                id="version-old",
                source_job_id="job-1",
                batch_id=None,
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="old",
                staged_results_json="{}",
                published_card_count=1,
                published_todo_count=0,
                status="completed",
            )
        )
        session.add(
            Batch(
                id="batch-1",
                job_id="job-1",
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                current_analysis_version_id="version-old",
                uploaded_at="2026-08-11T10:00:00+00:00",
                natural_date="2026-08-11",
            )
        )
        session.add(
            Card(
                id="card-old",
                batch_id="batch-1",
                analysis_version_id="version-old",
                scene_id="analysis",
                position=0,
                payload_json='{"scene_id":"analysis","cards":[]}',
            )
        )
        session.add(
            ProfileFact(
                id="profile-1",
                subject_id="user",
                dimension="preference",
                value_json='{"value":"clarity"}',
                confidence=0.8,
                source_audio_json="[]",
                first_seen_at="2026-08-01T00:00:00+00:00",
                last_seen_at="2026-08-01T00:00:00+00:00",
                evidence_count=1,
                origin="explicit",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_import_publishes_new_current_version_without_mutating_history(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "import.sqlite3")
    await database.create_schema()
    await seed(database)

    version_id = await import_latest_analysis(database, valid_payload())

    async with database.session() as session:
        batch = await session.get(Batch, "batch-1")
        version = await session.get(AnalysisVersion, version_id)
        old_version = await session.get(AnalysisVersion, "version-old")
        profile = await session.get(ProfileFact, "profile-1")
        current_cards = list(
            await session.scalars(
                select(Card).where(Card.analysis_version_id == version_id)
            )
        )
        todo_count = await session.scalar(select(func.count()).select_from(Todo))

    assert batch is not None and batch.current_analysis_version_id == version_id
    assert version is not None and version.status == "completed"
    assert version.published_card_count == 1
    assert old_version is not None and old_version.status == "completed"
    assert profile is not None and profile.last_seen_at == "2026-08-01T00:00:00+00:00"
    assert len(current_cards) == 1
    assert todo_count == 0
    await database.dispose()


@pytest.mark.asyncio
async def test_import_rolls_back_when_evidence_is_unknown(tmp_path: Path) -> None:
    database = Database(tmp_path / "rollback.sqlite3")
    await database.create_schema()
    await seed(database)
    value = valid_payload()
    value["cards"][0]["findings"][0]["evidence_segment_ids"] = ["missing"]

    with pytest.raises(ValueError, match="unknown evidence"):
        await import_latest_analysis(database, value)

    async with database.session() as session:
        batch = await session.get(Batch, "batch-1")
        version_count = await session.scalar(
            select(func.count()).select_from(AnalysisVersion)
        )
        card_count = await session.scalar(select(func.count()).select_from(Card))
    assert batch is not None and batch.current_analysis_version_id == "version-old"
    assert version_count == 1
    assert card_count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_import_cli_reports_version_and_card_count(tmp_path: Path) -> None:
    database_path = tmp_path / "cli.sqlite3"
    database = Database(database_path)
    await database.create_schema()
    await seed(database)
    await database.dispose()
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(valid_payload()), encoding="utf-8")
    root = Path(__file__).resolve().parents[3]
    environment = {**os.environ, "PYTHONPATH": str(root / "backend" / "src")}

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "import_analysis_result.py"),
            "--database",
            str(database_path),
            "--input",
            str(input_path),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["cards"] == 1
    assert report["version_id"]
