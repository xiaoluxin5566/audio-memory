import json

import pytest

from audio_memory.analysis.parser import SceneOutputError, parse_autonomous_output
from audio_memory.analysis.runner import AnalysisRunner
from audio_memory.prompts.autonomous_schema import AutonomousAnalysisResult


def rich_payload() -> dict[str, object]:
    return {
        "cards": [
            {
                "title": "目标、资源和权责正在形成一个危险循环",
                "summary": "几段工作交流共同揭示了投入下降的原因。",
                "content": [
                    {
                        "type": "scene_reconstruction",
                        "title": "场景还原与核心观点",
                        "body": "几段工作交流都围绕目标、资源和权责展开。",
                        "items": ["目标反复变化", "资源需要临时借用"],
                        "evidence_segment_ids": ["seg_0_1", "seg_1_4"],
                    },
                    {
                        "type": "analysis",
                        "title": "当前工作的主要矛盾",
                        "body": "问题不是单纯工作量大，而是权责与资源不匹配。",
                        "items": ["目标反复变化", "资源需要临时借用"],
                        "evidence_segment_ids": ["seg_0_1", "seg_1_4"],
                    }
                ],
                "quotes": [
                    {
                        "quote": "我不知道最终要到哪里。",
                        "context": "讨论项目方向时",
                        "analysis": "这句话指向目标缺失，而不只是执行困难。",
                        "evidence_segment_ids": ["seg_0_1"],
                    }
                ],
                "recommendations": [
                    {
                        "title": "建立最小专业闭环",
                        "reason": "避免分歧继续消耗交付质量。",
                        "actions": ["写清目标和约束", "让决策者确认方向"],
                        "suggested_language": "我把两个方案和代价写下来，请你确认方向。",
                        "success_signal": "方向和取舍得到书面确认。",
                        "caveat": "不包含公司机密。",
                        "evidence_segment_ids": ["seg_0_1", "seg_1_4"],
                    }
                ],
                "evidence_segment_ids": ["seg_0_1", "seg_1_4"],
            }
        ]
    }


def test_autonomous_schema_accepts_exactly_one_report() -> None:
    result = AutonomousAnalysisResult.model_validate(rich_payload())
    assert result.cards[0].content[0].type == "scene_reconstruction"

    with pytest.raises(ValueError):
        AutonomousAnalysisResult.model_validate({"cards": []})
    with pytest.raises(ValueError):
        AutonomousAnalysisResult.model_validate(
            {"cards": [rich_payload()["cards"][0], rich_payload()["cards"][0]]}
        )


def test_autonomous_schema_accepts_fixed_no_value_report() -> None:
    payload = rich_payload()
    card = payload["cards"][0]
    card["title"] = "本次内容报告"
    card["summary"] = "本次内容无有价值信息"
    card["content"] = [{
        "type": "empty",
        "title": "本次内容无有价值信息",
        "body": "本次内容无有价值信息",
        "items": [],
        "evidence_segment_ids": [],
    }]
    card["quotes"] = []
    card["recommendations"] = []
    card["evidence_segment_ids"] = []

    result = AutonomousAnalysisResult.model_validate(payload)

    assert result.cards[0].content[0].type == "empty"


def test_autonomous_schema_requires_scene_reconstruction_then_analysis() -> None:
    payload = rich_payload()
    payload["cards"][0]["content"] = [
        payload["cards"][0]["content"][1],
        payload["cards"][0]["content"][1],
    ]
    with pytest.raises(ValueError, match="scene_reconstruction"):
        AutonomousAnalysisResult.model_validate(payload)


def test_autonomous_json_schema_exposes_three_stage_section_constraints() -> None:
    schema = AutonomousAnalysisResult.model_json_schema()
    content = schema["$defs"]["AutonomousCard"]["properties"]["content"]

    assert content["minItems"] == 1

    payload = rich_payload()
    payload["cards"][0]["content"] = [
        payload["cards"][0]["content"][0],
        payload["cards"][0]["content"][0],
    ]
    with pytest.raises(ValueError, match="analysis section"):
        AutonomousAnalysisResult.model_validate(payload)


def test_autonomous_schema_rejects_duplicate_evidence_ids() -> None:
    payload = rich_payload()
    payload["cards"][0]["evidence_segment_ids"] = ["seg_0_1", "seg_0_1"]
    with pytest.raises(ValueError, match="evidence_segment_ids must be unique"):
        AutonomousAnalysisResult.model_validate(payload)


def test_parse_autonomous_output_requires_raw_valid_json() -> None:
    parsed = parse_autonomous_output(json.dumps(rich_payload(), ensure_ascii=False))
    assert parsed.cards[0].quotes[0].quote == "我不知道最终要到哪里。"

    with pytest.raises(SceneOutputError, match="raw JSON"):
        parse_autonomous_output("```json\n{}\n```")
    with pytest.raises(SceneOutputError):
        parse_autonomous_output('{"cards":[{"title":""}]}')


def test_quote_evidence_tolerates_asr_punctuation_but_not_rewriting() -> None:
    result = AutonomousAnalysisResult.model_validate(rich_payload())
    transcript = [
        {"segment_id": "seg_0_1", "text": "我不知道，最终要到哪里"},
        {"segment_id": "seg_1_4", "text": "资源需要临时借用"},
    ]
    AnalysisRunner._validate_autonomous_evidence(result, transcript)

    result.cards[0].quotes[0].quote = "我已经明确知道最终方向"
    with pytest.raises(ValueError, match="not verbatim"):
        AnalysisRunner._validate_autonomous_evidence(result, transcript)


def test_sanitizer_drops_only_invalid_quote_and_unknown_evidence() -> None:
    result = AutonomousAnalysisResult.model_validate(rich_payload())
    result.cards[0].quotes[0].quote = "被模型改写的句子"
    result.cards[0].quotes[0].evidence_segment_ids.append("invented")
    transcript = [
        {"segment_id": "seg_0_1", "text": "我不知道最终要到哪里"},
        {"segment_id": "seg_1_4", "text": "资源需要临时借用"},
    ]
    cleaned = AnalysisRunner._sanitize_autonomous_evidence(result, transcript)
    assert len(cleaned.cards) == 1
    assert cleaned.cards[0].quotes == []
    assert cleaned.cards[0].content[1].title == "当前工作的主要矛盾"
