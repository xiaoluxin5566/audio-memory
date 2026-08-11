from __future__ import annotations

import pytest

from audio_memory.analysis.result_import import convert_external_analysis


TRANSCRIPT = {
    "seg_0_1": "目标一直没有明确下来",
    "seg_0_2": "我下一次会先确认成功标准",
}


def payload() -> dict[str, object]:
    return {
        "status": "complete",
        "cards": [
            {
                "card_kind": "event",
                "scene_types": ["meeting", "work_conversation"],
                "title": "目标不清正在消耗执行力",
                "summary": "同一问题在会议和闲聊中重复出现。",
                "time_range": {"start": None, "end": None},
                "findings": [
                    {
                        "type": "fact",
                        "content": "主要成人说话者明确表示目标没有确定。",
                        "confidence": "high",
                        "evidence_segment_ids": ["seg_0_1"],
                    }
                ],
                "analysis": [
                    {
                        "title": "问题如何形成",
                        "content": "目标缺失使取舍没有稳定依据。",
                        "evidence_segment_ids": ["seg_0_1", "seg_0_2"],
                    }
                ],
                "quotes": [
                    {
                        "quote": "目标一直没有明确下来",
                        "why_it_matters": "这是问题的直接表述。",
                        "evidence_segment_ids": ["seg_0_1"],
                    }
                ],
                "actions": [
                    {
                        "title": "先确认成功标准",
                        "why": "避免带着不同目标推进。",
                        "steps": ["会前写出目标和验收标准"],
                        "suggested_language": "我先确认一下成功标准。",
                        "success_signal": "参会者能复述同一目标。",
                        "caveat": None,
                    }
                ],
            }
        ],
    }


def test_convert_external_analysis_preserves_meaning_and_metadata() -> None:
    result = convert_external_analysis(payload(), TRANSCRIPT)

    card = result.cards[0]
    assert card.title == "目标不清正在消耗执行力"
    assert card.evidence_segment_ids == ["seg_0_1", "seg_0_2"]
    assert card.content[0].type == "external_meta"
    assert card.content[0].body == '{"card_kind":"event","scene_types":["meeting","work_conversation"]}'
    assert card.content[1].type == "finding:fact:high"
    assert card.content[1].body == "主要成人说话者明确表示目标没有确定。"
    assert card.content[2].type == "analysis"
    assert card.content[2].title == "问题如何形成"
    assert card.quotes[0].quote == "目标一直没有明确下来"
    assert card.quotes[0].analysis == "这是问题的直接表述。"
    assert card.recommendations[0].actions == ["会前写出目标和验收标准"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(status="need_segments"), "complete"),
        (lambda value: value.update(cards=[]), "card"),
        (
            lambda value: value["cards"][0]["findings"][0].update(
                evidence_segment_ids=["seg_missing"]
            ),
            "unknown evidence",
        ),
        (
            lambda value: value["cards"][0]["quotes"][0].update(
                quote="目标一直没有明确下来，并且资源不足"
            ),
            "verbatim",
        ),
    ],
)
def test_convert_external_analysis_rejects_invalid_input(mutate, message: str) -> None:
    value = payload()
    mutate(value)

    with pytest.raises(ValueError, match=message):
        convert_external_analysis(value, TRANSCRIPT)
