import pytest

from audio_memory.analysis.direct_report_annotations import (
    parse_report_blocks,
    validate_annotations,
)
from audio_memory.prompts.direct_report_annotation_schema import (
    DirectReportAnnotations,
)


REPORT = """# 页面标题

## 今天发生了什么，重点改进什么

概览正文。

| 阶段 | 事件 | 改进 |
| --- | --- | --- |
| 下午 | 面试 | 核实范围 |

## 工作

普通正文。

### 下一步

> “真实原话”

- 补发简历
- 确认流程

1. 第一步
2. 第二步
"""


def annotations_for(blocks):
    return DirectReportAnnotations.model_validate({
        "annotations": [
            {"block_id": block.block_id, "type": block.inferred_type}
            for block in blocks
        ]
    })


def test_blocks_have_stable_ids_and_reconstruct_exact_markdown():
    blocks = parse_report_blocks(REPORT)
    assert "".join(item.markdown for item in blocks) == REPORT
    assert [(item.block_id, item.inferred_type) for item in blocks] == [
        ("block_001", "page_title"),
        ("block_002", "overview"),
        ("block_003", "paragraph"),
        ("block_004", "table"),
        ("block_005", "section_heading"),
        ("block_006", "paragraph"),
        ("block_007", "subheading"),
        ("block_008", "quote"),
        ("block_009", "bullet_list"),
        ("block_010", "numbered_list"),
    ]


def test_valid_annotations_cover_every_block_once():
    blocks = parse_report_blocks(REPORT)
    result = validate_annotations(blocks, annotations_for(blocks))
    assert len(result) == len(blocks)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_annotations_reject_incomplete_or_invalid_block_coverage(mutation):
    blocks = parse_report_blocks(REPORT)
    payload = annotations_for(blocks).model_dump(mode="json")
    if mutation == "missing":
        payload["annotations"].pop()
    elif mutation == "duplicate":
        payload["annotations"].append(dict(payload["annotations"][0]))
    else:
        payload["annotations"][-1]["block_id"] = "block_999"
    annotations = DirectReportAnnotations.model_validate(payload)
    with pytest.raises(ValueError):
        validate_annotations(blocks, annotations)
