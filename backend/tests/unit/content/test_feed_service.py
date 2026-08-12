from __future__ import annotations

import json

import pytest

from audio_memory.config import AppPaths
from audio_memory.content.service import ContentService
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, Batch, Card


class UnusedAnswerer:
    async def answer(self, **kwargs):
        raise AssertionError("feed must not invoke the question answerer")


async def seed_version(
    database: Database,
    *,
    job_id: str,
    batch_id: str,
    version_id: str,
    uploaded_at: str,
    natural_date: str,
    external_sources_json: str | None = None,
) -> None:
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="completed"))
        session.add(
            Batch(
                id=batch_id,
                job_id=job_id,
                uploaded_at=uploaded_at,
                natural_date=natural_date,
            )
        )
        await session.flush()
        session.add(
            AnalysisVersion(
                id=version_id,
                source_job_id=job_id,
                batch_id=batch_id,
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                external_sources_json=external_sources_json,
                status="completed",
            )
        )
        await session.flush()
        batch = await session.get(Batch, batch_id)
        assert batch is not None
        batch.current_analysis_version_id = version_id
        await session.commit()


@pytest.mark.asyncio
async def test_new_feed_orders_overview_first_and_resolves_card_sources(
    tmp_path,
) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    source = {
        "source_id": "source_real_001",
        "provider_id": "kimi",
        "provider_result_id": "result_42",
        "title": "Primary research",
        "url": "https://example.org/research",
        "publisher": "Example Institute",
        "published_at": "2025-01-02",
        "support_statement": "A grounded claim.",
        "search_round": 1,
    }
    await seed_version(
        database,
        job_id="job-new",
        batch_id="batch-new",
        version_id="version-new",
        uploaded_at="2026-08-12T08:00:00+00:00",
        natural_date="2026-08-12",
        external_sources_json=json.dumps([source]),
    )
    async with database.session() as session:
        session.add_all(
            [
                Card(
                    id="card-analysis",
                    batch_id="batch-new",
                    analysis_version_id="version-new",
                    scene_id="analysis",
                    position=1,
                    payload_json=json.dumps(
                        {
                            "scene_id": "analysis",
                            "cards": [
                                {
                                    "title": "Grounded card",
                                    "external_source_ids": ["source_real_001"],
                                }
                            ],
                        }
                    ),
                ),
                Card(
                    id="card-overview",
                    batch_id="batch-new",
                    analysis_version_id="version-new",
                    scene_id="batch_overview",
                    position=0,
                    payload_json=json.dumps(
                        {
                            "scene_id": "batch_overview",
                            "kind": "batch_overview",
                            "overview": {
                                "title": "本次概览",
                                "summary": "今天的录音摘要。",
                                "scene_ids": ["free-scene"],
                            },
                        },
                        ensure_ascii=False,
                    ),
                ),
            ]
        )
        await session.commit()

    feed = await ContentService(database, paths, UnusedAnswerer()).feed()
    cards = feed["days"][0]["cards"]

    assert [card["scene_id"] for card in cards] == [
        "batch_overview",
        "analysis",
    ]
    assert cards[0]["sources"] == []
    assert cards[1]["sources"] == [source]
    await database.dispose()


@pytest.mark.asyncio
async def test_legacy_feed_card_keeps_its_original_shape(tmp_path) -> None:
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    database = Database(paths.database)
    await database.create_schema()
    await seed_version(
        database,
        job_id="job-old",
        batch_id="batch-old",
        version_id="version-old",
        uploaded_at="2026-08-01T08:00:00+00:00",
        natural_date="2026-08-01",
    )
    payload = {"scene_id": "analysis", "cards": [{"title": "Historic card"}]}
    async with database.session() as session:
        session.add(
            Card(
                id="card-old",
                batch_id="batch-old",
                analysis_version_id="version-old",
                scene_id="analysis",
                position=0,
                payload_json=json.dumps(payload),
            )
        )
        await session.commit()

    feed = await ContentService(database, paths, UnusedAnswerer()).feed()

    assert feed == {
        "todos": [],
        "days": [
            {
                "date": "2026-08-01",
                "cards": [
                    {
                        "id": "card-old",
                        "batch_id": "batch-old",
                        "scene_id": "analysis",
                        "uploaded_at": "2026-08-01T08:00:00+00:00",
                        "payload": payload,
                        "evidence": [],
                        "qa": [],
                    }
                ],
            }
        ],
    }
    await database.dispose()
