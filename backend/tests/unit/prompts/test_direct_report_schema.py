from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from audio_memory.prompts.direct_report_schema import DirectReportDocument


def valid_document() -> dict:
    return {
        "schema_version": 1,
        "title": "离开、面试与数学辅导的一天",
        "overview": {
            "summary": "你同时在处理职业选择、面试判断和亲子沟通。",
            "rows": [
                {
                    "phase": "午后",
                    "event": "你参加了一次学习机岗位面试。",
                    "improvement": "把岗位信息转成可以验证的问题。",
                    "evidence_segment_ids": ["seg-1"],
                }
            ],
        },
        "sections": [
            {
                "title": "职业选择",
                "blocks": [
                    {
                        "type": "paragraph",
                        "text": "你需要先分清离开的原因和下一步选择标准。",
                    },
                    {
                        "type": "source_quote",
                        "text": "这个岗位我确实很感兴趣",
                        "evidence_segment_ids": ["seg-1"],
                    },
                    {
                        "type": "suggested_wording",
                        "text": "我想进一步确认团队目标和岗位边界。",
                    },
                    {
                        "type": "bullet_list",
                        "items": ["确认汇报线", "确认产品阶段"],
                    },
                    {
                        "type": "numbered_list",
                        "items": ["整理问题", "联系面试官"],
                    },
                    {
                        "type": "table",
                        "columns": ["需要确认", "验证方式"],
                        "rows": [["岗位边界", "向负责人提问"]],
                    },
                    {
                        "type": "subsection",
                        "title": "下一步验证",
                        "blocks": [
                            {
                                "type": "paragraph",
                                "text": "先完成低成本的信息核验。",
                            }
                        ],
                    },
                ],
            }
        ],
        "todos": [
            {
                "text": "向面试官确认岗位边界",
                "action": "确认",
                "object": "岗位边界",
                "owner_type": "user",
                "source_scene_id": "scene-1",
                "evidence_segment_ids": ["seg-1"],
                "confidence": 0.9,
            }
        ],
        "evidence_segment_ids": ["seg-1"],
        "external_source_ids": [],
    }


def test_schema_exposes_bounded_versioned_discriminated_contract() -> None:
    schema = DirectReportDocument.model_json_schema()

    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["sections"]["minItems"] == 1
    assert schema["properties"]["sections"]["maxItems"] == 12
    assert "discriminator" in json.dumps(schema)


def test_accepts_all_supported_semantic_blocks() -> None:
    document = DirectReportDocument.model_validate(valid_document())

    assert document.schema_version == 1
    assert len(document.sections[0].blocks) == 7
    assert document.todos[0].action == "确认"


@pytest.mark.parametrize(
    "title",
    ["核心结论", "完整报告", "今天发生了什么", "今天发生了什么，重点改进什么"],
)
def test_rejects_reserved_section_titles(title: str) -> None:
    value = valid_document()
    value["sections"][0]["title"] = title

    with pytest.raises(ValidationError, match="reserved"):
        DirectReportDocument.model_validate(value)


def test_rejects_duplicate_titles_after_trimming() -> None:
    value = valid_document()
    value["sections"].append({"title": " 职业选择 ", "blocks": [{"type": "paragraph", "text": "补充"}]})

    with pytest.raises(ValidationError, match="unique"):
        DirectReportDocument.model_validate(value)


@pytest.mark.parametrize("text", ["“这是原话”", '"这是原话"', "'这是原话'"])
def test_rejects_source_quotes_that_already_have_wrapping_quotes(text: str) -> None:
    value = valid_document()
    value["sections"][0]["blocks"][1]["text"] = text

    with pytest.raises(ValidationError, match="wrapping quotation"):
        DirectReportDocument.model_validate(value)


def test_rejects_source_quote_without_evidence() -> None:
    value = valid_document()
    value["sections"][0]["blocks"][1]["evidence_segment_ids"] = []

    with pytest.raises(ValidationError):
        DirectReportDocument.model_validate(value)


def test_rejects_table_rows_that_do_not_match_columns() -> None:
    value = valid_document()
    value["sections"][0]["blocks"][5]["rows"] = [["只有一格"]]

    with pytest.raises(ValidationError, match="width"):
        DirectReportDocument.model_validate(value)


@pytest.mark.parametrize("field", ["evidence_segment_ids", "external_source_ids"])
def test_rejects_duplicate_document_source_ids(field: str) -> None:
    value = valid_document()
    value[field] = ["duplicate", "duplicate"]

    with pytest.raises(ValidationError, match="unique"):
        DirectReportDocument.model_validate(value)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<script>alert(1)</script>", "unsafe"),
        ("用户表示应该继续面试。", "second-person"),
        ("从语气可以判断你很焦虑。", "nonverbal"),
    ],
)
def test_rejects_unsafe_or_unsupported_report_claims(text: str, message: str) -> None:
    value = valid_document()
    value["sections"][0]["blocks"][0]["text"] = text

    with pytest.raises(ValidationError, match=message):
        DirectReportDocument.model_validate(value)
