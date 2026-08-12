from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_memory.prompts.day_map_schema import (
    AutonomousDayMap,
    AutonomousScene,
    BatchOverview,
    NativeSearchDecision,
    NativeSearchQuery,
)


def scene(scene_id: str, title: str) -> AutonomousScene:
    return AutonomousScene(
        scene_id=scene_id,
        title=title,
        description="模型自主发现的真实片段。",
        evidence_segment_ids=["seg_1", "seg_2"],
        file_ids=["file_1"],
        start_ms=0,
        end_ms=1_000,
        recommend_deep_analysis=True,
        recommendation_reason="这个单元值得单独展开。",
    )


def test_day_map_accepts_arbitrary_scene_names_and_one_batch_overview() -> None:
    result = AutonomousDayMap(
        overview=BatchOverview(
            title="本次概览",
            summary="今天的音频包含两个不同的生活片段。",
            scene_ids=["scene_tea", "scene_meteorology"],
        ),
        scenes=[scene("scene_tea", "给旧茶壶除垢"), scene("scene_meteorology", "讨论雷暴云")],
        search_action=NativeSearchDecision(action="finalize", rationale="录音已足够支持分析。"),
    )

    assert result.scenes[0].title == "给旧茶壶除垢"
    assert result.overview.title == "本次概览"


def test_batch_overview_must_use_the_single_fixed_visible_title() -> None:
    with pytest.raises(ValidationError, match="本次概览"):
        BatchOverview(title="今日总结", summary="摘要", scene_ids=[])


def test_search_decision_accepts_a_valid_search_request() -> None:
    decision = NativeSearchDecision(
        action="search",
        rationale="需要核验节目发布日期。",
        queries=[NativeSearchQuery(query="节目 X 首播日期", purpose="核验录音中的日期")],
    )

    assert decision.action == "search"


def test_search_decision_limits_each_round_to_five_queries() -> None:
    with pytest.raises(ValidationError, match="at most 5"):
        NativeSearchDecision(
            action="search",
            rationale="核验。",
            queries=[NativeSearchQuery(query=f"query {index}", purpose="核验") for index in range(6)],
        )
