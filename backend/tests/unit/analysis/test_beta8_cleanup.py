from audio_memory.analysis.beta8_cleanup import (
    normalize_markdown_heading_title,
    remove_redundant_section_labels,
)
from audio_memory.prompts.beta8_scene_schema import parse_beta8_card_markdown


def test_remove_redundant_section_labels_keeps_summary_content() -> None:
    original = """# 标题

核心摘要：这是需要保留的摘要。

正文：

## 详情

这是详情。
"""

    cleaned = remove_redundant_section_labels(original)

    assert "核心摘要：" not in cleaned
    assert "正文：" not in cleaned
    assert "这是需要保留的摘要。" in cleaned
    assert parse_beta8_card_markdown(cleaned).summary == "这是需要保留的摘要。"


def test_remove_redundant_section_labels_handles_bold_standalone_label() -> None:
    original = """# 标题

**核心摘要**：

摘要内容。

## 详情

详情内容。
"""

    cleaned = remove_redundant_section_labels(original)

    assert "核心摘要" not in cleaned
    assert parse_beta8_card_markdown(cleaned).summary == "摘要内容。"


def test_normalize_markdown_heading_title_removes_historical_manual_ordinals() -> None:
    assert normalize_markdown_heading_title("一、议题") == "议题"
    assert normalize_markdown_heading_title("二、后续") == "后续"
    assert normalize_markdown_heading_title("1. 决定") == "决定"
    assert normalize_markdown_heading_title("1、行动") == "行动"
    assert normalize_markdown_heading_title("（一）风险") == "风险"
    assert normalize_markdown_heading_title("(1) 复盘") == "复盘"


def test_normalize_markdown_heading_title_keeps_meaningful_text_unchanged() -> None:
    assert normalize_markdown_heading_title("2026 年计划") == "2026 年计划"
    assert normalize_markdown_heading_title("第 1 次讨论") == "第 1 次讨论"
    assert normalize_markdown_heading_title("1.5 倍提升") == "1.5 倍提升"
    assert normalize_markdown_heading_title("2.0 模型") == "2.0 模型"
    assert normalize_markdown_heading_title("2026.09 计划") == "2026.09 计划"
