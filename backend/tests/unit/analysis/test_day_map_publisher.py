from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, Card
from audio_memory.prompts.autonomous_schema import AutonomousAnalysisResult


def autonomous_result() -> AutonomousAnalysisResult:
    return AutonomousAnalysisResult.model_validate(
        {
            "cards": [
                {
                    "title": "亲子对话",
                    "summary": "先接住情绪，再讨论规则。",
                    "external_source_ids": ["source_real_001"],
                    "content": [
                        {
                            "type": "scene_reconstruction",
                            "title": "场景还原",
                            "body": "孩子因为中断游戏而难过。",
                            "evidence_segment_ids": ["seg_0_0"],
                        },
                        {
                            "type": "analysis",
                            "title": "核心分析",
                            "body": "先命名情绪更有助于合作。",
                            "evidence_segment_ids": ["seg_0_0"],
                        },
                    ],
                    "evidence_segment_ids": ["seg_0_0"],
                },
                {
                    "title": "节目笔记",
                    "summary": "记下了一个值得延展的观点。",
                    "content": [
                        {
                            "type": "scene_reconstruction",
                            "title": "场景还原",
                            "body": "收听节目时做了口述笔记。",
                            "evidence_segment_ids": ["seg_0_1"],
                        },
                        {
                            "type": "analysis",
                            "title": "核心分析",
                            "body": "可以转成待验证的内容选题。",
                            "evidence_segment_ids": ["seg_0_1"],
                        },
                    ],
                    "evidence_segment_ids": ["seg_0_1"],
                },
            ]
        }
    )


def staged_day_map() -> dict[str, object]:
    source = {
        "source_id": "source_real_001",
        "provider_id": "kimi",
        "provider_result_id": "result_42",
        "title": "Emotion coaching and child regulation",
        "url": "https://example.org/research/emotion-coaching",
        "publisher": "Example Institute",
        "published_at": "2025-01-02",
        "support_statement": "Emotion naming can support regulation.",
        "search_round": 1,
    }
    search_round = {
        "round_number": 1,
        "decision": {
            "action": "search",
            "rationale": "核对亲子情绪辅导的研究证据。",
            "queries": [
                {
                    "query": "emotion coaching child regulation",
                    "purpose": "核对分析依据",
                }
            ],
        },
        "results": [
            {
                "provider_result_id": "result_42",
                "title": "Emotion coaching and child regulation",
                "url": "https://example.org/research/emotion-coaching",
                "publisher": "Example Institute",
                "published_at": "2025-01-02",
                "snippet": "Emotion naming can support regulation.",
            }
        ],
        "sources": [source],
        "errors": [],
    }
    return {
        "day_map": {
            "overview": {
                "title": "本次概览",
                "summary": "这段录音从亲子对话转向节目笔记。",
                "scene_ids": ["child-transition", "media-note"],
            },
            "scenes": [
                {
                    "scene_id": "child-transition",
                    "title": "游戏结束时的亲子对话",
                    "description": "孩子抗拒结束游戏。",
                    "evidence_segment_ids": ["seg_0_0"],
                    "file_ids": ["file-1"],
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "recommend_deep_analysis": True,
                    "recommendation_reason": "有具体的沟通改进机会。",
                    "external_verification_need": "核对情绪辅导依据。",
                },
                {
                    "scene_id": "media-note",
                    "title": "节目观点笔记",
                    "description": "收听后记录想法。",
                    "evidence_segment_ids": ["seg_0_1"],
                    "file_ids": ["file-1"],
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "recommend_deep_analysis": True,
                    "recommendation_reason": "可形成后续内容选题。",
                    "external_verification_need": None,
                },
            ],
            "search_action": {
                "action": "search",
                "rationale": "需要核对一项外部事实。",
                "queries": [
                    {
                        "query": "emotion coaching child regulation",
                        "purpose": "核对分析依据",
                    }
                ],
            },
        },
        "search_rounds": [search_round],
        "external_sources": [source],
    }


@pytest.mark.asyncio
async def test_day_map_publication_is_idempotent_and_persists_provenance(
    tmp_path,
) -> None:
    database = Database(tmp_path / "publisher.sqlite3")
    await database.create_schema()
    staged = staged_day_map()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="ready_to_commit"))
        session.add(
            AnalysisVersion(
                id="version-1",
                source_job_id="job-1",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json=json.dumps(staged, ensure_ascii=False),
                status="running",
            )
        )
        await session.commit()

    publisher = VersionPublisher(database)
    first = await publisher.publish("version-1", autonomous_result(), [])
    second = await publisher.publish("version-1", autonomous_result(), [])

    async with database.session() as session:
        cards = list(
            await session.scalars(
                select(Card)
                .where(Card.analysis_version_id == "version-1")
                .order_by(Card.position)
            )
        )
        version = await session.get(AnalysisVersion, "version-1")

    assert first == second
    assert first.card_count == 3
    assert [card.scene_id for card in cards] == [
        "batch_overview",
        "analysis",
        "analysis",
    ]
    assert [card.position for card in cards] == [0, 1, 2]
    assert sum(card.scene_id == "batch_overview" for card in cards) == 1
    assert json.loads(cards[0].payload_json) == {
        "scene_id": "batch_overview",
        "kind": "batch_overview",
        "overview": staged["day_map"]["overview"],
    }
    assert version is not None
    assert json.loads(version.batch_overview_json) == staged["day_map"]["overview"]
    assert json.loads(version.search_rounds_json) == staged["search_rounds"]
    assert json.loads(version.external_sources_json) == staged["external_sources"]
    await database.dispose()


@pytest.mark.asyncio
async def test_repeated_provider_source_keeps_first_round_as_canonical_origin(
    tmp_path,
) -> None:
    database = Database(tmp_path / "repeated-source.sqlite3")
    await database.create_schema()
    staged = staged_day_map()
    repeated_round = deepcopy(staged["search_rounds"][0])
    repeated_round["round_number"] = 2
    repeated_round["sources"][0]["search_round"] = 2
    staged["search_rounds"].append(repeated_round)
    # The accumulated source list is canonical and need not duplicate an
    # identical provider result that appeared in a later search round.
    assert len(staged["external_sources"]) == 1
    async with database.session() as session:
        session.add(AnalysisJob(id="job-repeat", stage="ready_to_commit"))
        session.add(
            AnalysisVersion(
                id="version-repeat",
                source_job_id="job-repeat",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json=json.dumps(staged, ensure_ascii=False),
                status="running",
            )
        )
        await session.commit()

    await VersionPublisher(database).publish(
        "version-repeat", autonomous_result(), []
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-repeat")
    assert version is not None
    assert json.loads(version.search_rounds_json) == staged["search_rounds"]
    assert json.loads(version.external_sources_json) == staged["external_sources"]
    await database.dispose()


@pytest.mark.asyncio
async def test_failure_after_card_replacement_rolls_back_cards_and_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    database = Database(tmp_path / "rollback.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-rollback", stage="ready_to_commit"))
        session.add(
            AnalysisVersion(
                id="version-rollback",
                source_job_id="job-rollback",
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json=json.dumps(staged_day_map(), ensure_ascii=False),
                status="running",
            )
        )
        await session.commit()

    async def fail_after_cards(*args, **kwargs):
        raise RuntimeError("forced failure after card replacement")

    monkeypatch.setattr(
        VersionPublisher,
        "_insert_todo_candidates",
        staticmethod(fail_after_cards),
    )
    with pytest.raises(RuntimeError, match="forced failure"):
        await VersionPublisher(database).publish(
            "version-rollback", autonomous_result(), []
        )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-rollback")
        card_count = int(
            await session.scalar(
                select(func.count(Card.id)).where(
                    Card.analysis_version_id == "version-rollback"
                )
            )
            or 0
        )
    assert version is not None
    assert version.status == "running"
    assert version.batch_id is None
    assert version.batch_overview_json is None
    assert version.search_rounds_json is None
    assert version.external_sources_json is None
    assert card_count == 0
    await database.dispose()
