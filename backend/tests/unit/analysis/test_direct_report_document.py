from __future__ import annotations

from audio_memory.analysis.direct_report_document import (
    StructuredReportResult,
    document_to_markdown,
)
from audio_memory.prompts.direct_report_schema import DirectReportDocument


def document() -> DirectReportDocument:
    return DirectReportDocument.model_validate(
        {
            "schema_version": 1,
            "title": "一天的三个选择",
            "overview": {
                "summary": "你需要把模糊感受转成可以验证的下一步。",
                "rows": [
                    {
                        "phase": "午后",
                        "event": "岗位信息仍不完整。",
                        "improvement": "确认团队|目标。",
                        "evidence_segment_ids": ["seg-1"],
                    }
                ],
            },
            "sections": [
                {
                    "title": "职业判断",
                    "blocks": [
                        {"type": "paragraph", "text": "先区分事实和推测。"},
                        {
                            "type": "source_quote",
                            "text": "这个岗位我很感兴趣",
                            "evidence_segment_ids": ["seg-1"],
                        },
                        {
                            "type": "suggested_wording",
                            "text": "我想确认岗位的核心目标。",
                        },
                        {"type": "bullet_list", "items": ["团队规模", "汇报关系"]},
                        {"type": "numbered_list", "items": ["整理问题", "发出确认"]},
                        {
                            "type": "table",
                            "columns": ["问题", "验证方式"],
                            "rows": [["目标|边界", "询问负责人\n并记录"]],
                        },
                        {
                            "type": "subsection",
                            "title": "行动边界",
                            "blocks": [
                                {"type": "paragraph", "text": "只验证关键未知项。"}
                            ],
                        },
                    ],
                }
            ],
            "todos": [],
            "evidence_segment_ids": ["seg-1"],
            "external_source_ids": [],
        }
    )


def test_serializes_every_semantic_block_to_exact_compatibility_markdown() -> None:
    assert document_to_markdown(document()) == r"""# 一天的三个选择

## 今天发生了什么，重点改进什么

你需要把模糊感受转成可以验证的下一步。

| 时段 | 发生的事 | 可以改进 |
| --- | --- | --- |
| 午后 | 岗位信息仍不完整。 | 确认团队\|目标。 |

## 职业判断

先区分事实和推测。

> 这个岗位我很感兴趣

> 我想确认岗位的核心目标。

- 团队规模
- 汇报关系

1. 整理问题
2. 发出确认

| 问题 | 验证方式 |
| --- | --- |
| 目标\|边界 | 询问负责人 并记录 |

### 行动边界

只验证关键未知项。
"""


def test_result_derives_title_summary_and_markdown_without_inference() -> None:
    result = StructuredReportResult.from_document(document())

    assert result.title == "一天的三个选择"
    assert result.summary == "你需要把模糊感受转成可以验证的下一步。"
    assert result.report_markdown == document_to_markdown(document())
    assert "## 核心结论" not in result.report_markdown
    assert "## 完整报告" not in result.report_markdown
