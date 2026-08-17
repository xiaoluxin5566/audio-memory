from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from audio_memory.prompts.direct_report_light_schema import DirectReportLightDocument


def valid_light_document() -> dict:
    return {
        "schema_version": 1,
        "title": "一天的报告",
        "overview": {"summary": "你需要闭环两件事。", "rows": [{"phase": "下午", "event": "完成面试。", "improvement": "确认边界。", "evidence_segment_ids": ["s1"]}]},
        "sections": [{"title": "职业选择", "content": "你需要把判断变成验证。", "evidence_segment_ids": ["s1"]}],
        "todos": [], "evidence_segment_ids": ["s1"], "external_source_ids": [],
    }


def test_light_schema_contains_only_report_skeleton_inside_sections() -> None:
    document = DirectReportLightDocument.model_validate(valid_light_document())
    schema = json.dumps(document.model_json_schema(), ensure_ascii=False)

    assert document.sections[0].content == "你需要把判断变成验证。"
    assert '"content"' in schema
    assert '"blocks"' not in schema
    assert '"source_quote"' not in schema


def test_light_schema_rejects_reserved_titles_and_duplicate_evidence() -> None:
    reserved = valid_light_document()
    reserved["sections"][0]["title"] = "核心结论"
    duplicate = valid_light_document()
    duplicate["sections"][0]["evidence_segment_ids"] = ["s1", "s1"]

    with pytest.raises(ValidationError):
        DirectReportLightDocument.model_validate(reserved)
    with pytest.raises(ValidationError):
        DirectReportLightDocument.model_validate(duplicate)
