from __future__ import annotations

import pytest

from audio_memory.analysis.autonomous_context import (
    DirectContext,
    LongContextPlan,
    plan_autonomous_context,
)
from audio_memory.analysis.runner import AnalysisRunner
from audio_memory.analysis.provider import ProviderAnalysisError
from audio_memory.prompts.autonomous_schema import (
    AutonomousAnalysisResult,
    AutonomousRetrievalPlan,
    InformationNotebook,
)


def segment(index: int, text: str) -> dict[str, object]:
    return {
        "segment_id": f"seg_{index}",
        "file_id": "file-1",
        "file_name": "recording.m4a",
        "start_ms": index * 1_000,
        "end_ms": (index + 1) * 1_000,
        "text": text,
    }


def test_normal_28470_character_transcript_uses_one_complete_source_request() -> None:
    transcript = [segment(0, "录" * 28_470)]

    context = plan_autonomous_context(transcript)

    assert isinstance(context, DirectContext)
    assert context.transcript == transcript


def test_long_transcript_is_partitioned_in_source_order_without_splitting_segments() -> None:
    transcript = [segment(index, "录" * 6_000) for index in range(6)]

    context = plan_autonomous_context(transcript)

    assert isinstance(context, LongContextPlan)
    assert [[item["segment_id"] for item in window.segments] for window in context.windows] == [
        ["seg_0", "seg_1"],
        ["seg_2", "seg_3"],
        ["seg_4", "seg_5"],
    ]
    assert [item["segment_id"] for window in context.windows for item in window.segments] == [
        item["segment_id"] for item in transcript
    ]
    assert all(window.character_count <= 12_000 for window in context.windows)


def test_an_oversized_single_segment_remains_intact_in_its_own_window() -> None:
    transcript = [
        segment(0, "录" * 20_000),
        segment(1, "录" * 12_000),
    ]

    context = plan_autonomous_context(transcript)

    assert isinstance(context, LongContextPlan)
    assert [[item["segment_id"] for item in window.segments] for window in context.windows] == [
        ["seg_0"],
        ["seg_1"],
    ]
    assert context.windows[0].oversized_single_segment is True
    assert context.windows[1].oversized_single_segment is False


class LongRouteProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def analyze_autonomous_notes(self, request, provider_snapshot):
        window_id = request.scene_id.split(":", 1)[1]
        self.calls.append(request.scene_id)
        index = int(window_id.rsplit("_", 1)[1])
        ids = [f"seg_{(index - 1) * 2}", f"seg_{(index - 1) * 2 + 1}"]
        return InformationNotebook.model_validate({
            "window_id": window_id,
            "notes": [{"topic": "原文索引", "details": ["高保真事实"], "evidence_segment_ids": ids}],
        })

    async def analyze_autonomous_retrieval_plan(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        return AutonomousRetrievalPlan.model_validate({
            "cards": [{
                "title": "完整任务",
                "analysis_task": "形成一张可独立行动的分析卡",
                "required_segment_ids": ["seg_1", "seg_4"],
            }]
        })

    async def analyze_autonomous_final(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        return AutonomousAnalysisResult.model_validate({
            "cards": [{
                "title": "完整任务",
                "summary": "基于回取原文形成判断",
                "content": [
                    {"type": "scene_reconstruction", "title": "场景还原", "body": "背景", "evidence_segment_ids": ["seg_1"]},
                    {"type": "analysis", "title": "分析", "body": "判断", "evidence_segment_ids": ["seg_4"]},
                ],
                "quotes": [], "recommendations": [],
                "evidence_segment_ids": ["seg_1", "seg_4"],
            }]
        })


class IsolatedLongRunner(AnalysisRunner):
    def __init__(self, provider) -> None:
        self.provider = provider
        from audio_memory.prompts.composer import PromptComposer
        self.composer = PromptComposer()
        self.saved: list[dict[str, object]] = []

    async def _require_ownership(self, *args):
        return None

    async def _require_generation(self, *args):
        return None

    async def _save_staged(self, version_id, staged, worker_owner_id):
        import copy
        self.saved.append(copy.deepcopy(staged))


@pytest.mark.asyncio
async def test_long_route_stages_notes_retrieves_exact_source_in_order_and_resumes() -> None:
    transcript = [segment(index, "录" * 6_000) for index in range(6)]
    context = plan_autonomous_context(transcript)
    assert isinstance(context, LongContextPlan)
    provider = LongRouteProvider()
    runner = IsolatedLongRunner(provider)
    version = type("Version", (), {"id": "version-1"})()
    staged: dict[str, object] = {}

    result, retrieved = await runner._long_autonomous(
        version, context, transcript, [], {"provider_id": "deepseek"}, staged, None
    )

    assert [item["segment_id"] for item in retrieved] == ["seg_1", "seg_4"]
    assert result.cards[0].evidence_segment_ids == ["seg_1", "seg_4"]
    assert provider.calls == [
        "autonomous-notes:window_0001", "autonomous-notes:window_0002",
        "autonomous-notes:window_0003", "autonomous-retrieval-plan", "autonomous-final",
    ]
    assert len(runner.saved) == 5

    provider.calls.clear()
    resumed, resumed_source = await runner._long_autonomous(
        version, context, transcript, [], {"provider_id": "deepseek"}, staged, None
    )
    assert provider.calls == []
    assert resumed == result
    assert resumed_source == retrieved


@pytest.mark.asyncio
async def test_long_route_rejects_invalid_staged_note_before_resume() -> None:
    transcript = [segment(index, "录" * 6_000) for index in range(6)]
    context = plan_autonomous_context(transcript)
    assert isinstance(context, LongContextPlan)
    runner = IsolatedLongRunner(LongRouteProvider())
    version = type("Version", (), {"id": "version-1"})()
    staged = {
        "autonomous_notes": {
            "window_0001": {
                "window_id": "window_0001",
                "notes": [{
                    "topic": "污染的恢复数据", "details": ["不应继续"],
                    "evidence_segment_ids": ["seg_5"],
                }],
            }
        }
    }

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner._long_autonomous(
            version, context, transcript, [], {"provider_id": "deepseek"}, staged, None
        )

    assert raised.value.code == "autonomous_notes_evidence_invalid"
