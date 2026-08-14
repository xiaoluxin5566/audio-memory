from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, Card
from audio_memory.prompts.autonomous_schema import AutonomousAnalysisResult
from audio_memory.prompts.report_schema import SingleReportDraft
from audio_memory.analysis.markdown_report import MarkdownReportResult
from audio_memory.analysis.direct_report_document import StructuredReportResult
from audio_memory.prompts.direct_report_schema import DirectReportDocument


SOURCE_ID = (
    "source_"
    "686192289d21f34fd636a3e1e8fadc22aa8603dd36bf33487ff3ccc7109b83d5"
)


def autonomous_result() -> AutonomousAnalysisResult:
    return AutonomousAnalysisResult.model_validate(
        {
            "cards": [
                {
                    "title": "亲子对话",
                    "summary": "先接住情绪，再讨论规则。",
                    "external_source_ids": [SOURCE_ID],
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
            ]
        }
    )


def markdown_report() -> SingleReportDraft:
    return SingleReportDraft(
        title="今天最值得关注的三件事",
        summary="工作决策、家庭分工和亲子互动都有后续事项。",
        report_markdown=(
            "# 核心结论\n\n你今天最需要推进的是工作决策闭环。\n\n"
            "## 重要时间轴\n\n- 09:00 项目讨论\n\n"
            "## 数据范围与质量\n\n本次逐字稿覆盖完整。"
        ),
        todos=[
            {
                "text": "确认一期范围",
                "action": "确认",
                "object": "一期范围",
                "owner_type": "user",
                "assignee_text": "你",
                "due_at": None,
                "due_text": "未明确",
                "dependency": "等待设计评估",
                "next_step": "整理两个方案",
                "source_scene_id": "scene-work-1",
                "evidence_segment_ids": ["seg_0_0"],
                "confidence": 0.9,
            }
        ],
        evidence_segment_ids=["seg_0_0"],
        external_source_ids=[],
    )


@pytest.mark.asyncio
async def test_markdown_report_publishes_exactly_one_card_with_metrics(tmp_path) -> None:
    database = Database(tmp_path / "markdown-publisher.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-markdown", stage="ready_to_commit"))
        session.add(
            AnalysisVersion(
                id="version-markdown",
                source_job_id="job-markdown",
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="rules",
                staged_results_json="{}",
                pipeline_metrics_json=json.dumps(
                    {
                        "model_call_count": 6,
                        "input_tokens": 12000,
                        "output_tokens": 3000,
                        "model_duration_ms": 9000,
                    }
                ),
                status="running",
            )
        )
        await session.commit()

    outcome = await VersionPublisher(database).publish(
        "version-markdown", markdown_report(), []
    )

    async with database.session() as session:
        cards = list(await session.scalars(select(Card)))
    assert outcome.card_count == 1
    assert len(cards) == 1
    payload = json.loads(cards[0].payload_json)
    assert payload["reportMarkdown"].startswith("# 核心结论")
    assert payload["runtimeMetrics"]["model_call_count"] == 6
    assert payload["cards"][0]["title"] == "今天最值得关注的三件事"
    assert outcome.todo_count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_native_markdown_report_publishes_one_card_without_model_todos(tmp_path) -> None:
    database = Database(tmp_path / "native-markdown.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-native", stage="analyzing"))
        session.add(AnalysisVersion(
            id="version-native", source_job_id="job-native", provider_id="deepseek",
            model_id="deepseek-v4-pro", credential_generation=1,
            prompt_snapshot_json="{}", profile_snapshot_json="[]",
            fixed_rules_hash="rules", staged_results_json="{}", status="running",
        ))
        await session.commit()

    outcome = await VersionPublisher(database).publish(
        "version-native",
        MarkdownReportResult.from_markdown(
            "# 今日分析\n\n## 核心结论\n\n今天最重要的是推进项目闭环。"
        ),
        [],
    )

    async with database.session() as session:
        cards = list(await session.scalars(select(Card)))
    assert outcome.card_count == 1
    assert outcome.todo_count == 0
    assert json.loads(cards[0].payload_json)["reportMarkdown"].startswith("# 今日分析")
    await database.dispose()


@pytest.mark.asyncio
async def test_structured_report_publishes_document_and_markdown_compatibility(tmp_path) -> None:
    database = Database(tmp_path / "structured-publisher.sqlite3")
    await database.create_schema()
    async with database.session() as session:
        session.add(AnalysisJob(id="job-structured", stage="analyzing"))
        session.add(AnalysisVersion(
            id="version-structured", source_job_id="job-structured", provider_id="deepseek",
            model_id="deepseek-v4-pro", credential_generation=1,
            prompt_snapshot_json="{}", profile_snapshot_json="[]",
            fixed_rules_hash="rules", staged_results_json="{}", status="running",
        ))
        await session.commit()
    document = DirectReportDocument.model_validate(
        {
            "schema_version": 1,
            "title": "今天的三个判断",
            "overview": {
                "summary": "你需要先核实关键事实。",
                "rows": [
                    {
                        "phase": "下午",
                        "event": "完成面试。",
                        "improvement": "确认岗位边界。",
                        "evidence_segment_ids": ["seg_0_0"],
                    }
                ],
            },
            "sections": [
                {
                    "title": "面试判断",
                    "blocks": [{"type": "paragraph", "text": "你仍缺少岗位信息。"}],
                }
            ],
            "todos": [],
            "evidence_segment_ids": ["seg_0_0"],
            "external_source_ids": [],
        }
    )

    await VersionPublisher(database).publish(
        "version-structured", StructuredReportResult.from_document(document), []
    )

    async with database.session() as session:
        card = await session.scalar(select(Card))
    payload = json.loads(card.payload_json)
    assert payload["reportDocument"]["schema_version"] == 1
    assert payload["reportDocument"]["overview"]["rows"]
    assert payload["reportMarkdown"].startswith("# 今天的三个判断")
    assert payload["cards"][0]["summary"] == payload["reportDocument"]["overview"]["summary"]
    await database.dispose()


def staged_day_map() -> dict[str, object]:
    source = {
        "source_id": SOURCE_ID,
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
    assert first.card_count == 1
    assert [card.scene_id for card in cards] == ["analysis"]
    assert [card.position for card in cards] == [0]
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_id", "source_arbitrary", "source"),
        ("provider_id", "openai", "provider"),
        ("title", "Rewritten title", "title and URL"),
        ("url", "https://example.org/rewritten", "title and URL"),
    ],
)
async def test_publication_rejects_fabricated_source_provenance(
    tmp_path,
    field: str,
    value: str,
    message: str,
) -> None:
    database = Database(tmp_path / f"invalid-{field}.sqlite3")
    await database.create_schema()
    staged = staged_day_map()
    staged["search_rounds"][0]["sources"][0][field] = value
    staged["external_sources"][0][field] = value
    if field == "source_id":
        result = autonomous_result().model_copy(deep=True)
        result.cards[0].external_source_ids = [value]
    else:
        result = autonomous_result()
    async with database.session() as session:
        session.add(AnalysisJob(id=f"job-{field}", stage="ready_to_commit"))
        session.add(
            AnalysisVersion(
                id=f"version-{field}",
                source_job_id=f"job-{field}",
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

    with pytest.raises(ValueError, match=message):
        await VersionPublisher(database).publish(
            f"version-{field}", result, []
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_publication_rejects_finalize_round_with_new_results(tmp_path) -> None:
    database = Database(tmp_path / "finalize-results.sqlite3")
    await database.create_schema()
    staged = staged_day_map()
    staged["search_rounds"][0]["decision"] = {
        "action": "finalize",
        "rationale": "已有足够资料。",
        "queries": [],
    }
    async with database.session() as session:
        session.add(AnalysisJob(id="job-finalize", stage="ready_to_commit"))
        session.add(
            AnalysisVersion(
                id="version-finalize",
                source_job_id="job-finalize",
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

    with pytest.raises(ValueError, match="final search decision"):
        await VersionPublisher(database).publish(
            "version-finalize", autonomous_result(), []
        )
    await database.dispose()
