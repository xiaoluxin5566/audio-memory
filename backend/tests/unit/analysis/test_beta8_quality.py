from types import SimpleNamespace

import pytest

from audio_memory.analysis.beta8_quality import (
    CardStructureError,
    inspect_card_markdown,
    validate_card_markdown,
    validate_v1_structure,
)


def _structured_markdown() -> str:
    return """# 发布准备

- 发布前先核对固定输入。
- 完成后保留可追溯记录。

## 关键项

| 项目 | 状态 |
| --- | --- |
| 输入 | 已固定 |

### 执行步骤

1. 核对输入。
2. 运行检查。
3. 留存结果。

> 有异常时暂停发布。
"""


def test_rejects_manual_ordinals_and_duplicate_h1_with_locations() -> None:
    markdown = "# 标题\n\n结论\n\n## 一、议题\n\n# 重复标题"

    with pytest.raises(CardStructureError) as raised:
        validate_card_markdown(markdown)

    assert "manual ordinal heading at line 5" in str(raised.value)
    assert "duplicate H1 at line 7" in str(raised.value)


def test_rejects_complex_card_as_one_long_paragraph() -> None:
    markdown = "# 标题\n\n核心判断\n\n## 分析\n\n" + "复杂信息" * 150

    with pytest.raises(CardStructureError, match="long paragraph at line 7"):
        validate_card_markdown(markdown)


def test_accepts_core_information_block_with_multiple_judgments() -> None:
    markdown = "# 标题\n\n- 结论 A\n- 结论 B\n\n## 依据\n\n事实。"

    signals = inspect_card_markdown(markdown)

    assert signals.h2_count == 1
    validate_card_markdown(markdown)


def test_reports_structures_without_claiming_semantic_correctness() -> None:
    signals = inspect_card_markdown(_structured_markdown())

    assert signals.table_count == 1
    assert signals.ordered_step_count == 3
    assert signals.blockquote_count == 1
    assert signals.semantic_correctness is None


def test_accepts_a_well_formed_table_with_data_rows() -> None:
    validate_card_markdown(_structured_markdown())


def test_ignores_fenced_code_when_extracting_markdown_structure() -> None:
    markdown = """# 标题

核心判断。

## 依据

```markdown
# 这不是重复标题
## 一、这不是手工编号
| 这不是 | 表格 |
1. 这不是步骤
```

事实。
"""

    signals = inspect_card_markdown(markdown)

    assert signals.h2_count == 1
    assert signals.table_count == 0
    assert signals.ordered_step_count == 0
    assert signals.manual_ordinal_headings == []
    validate_card_markdown(markdown)


def test_rejects_an_obviously_malformed_table() -> None:
    markdown = "# 标题\n\n核心判断。\n\n## 详情\n\n| 项目 | 状态 |\n| 输入 | 已固定 |"

    with pytest.raises(CardStructureError, match="malformed table at line 7"):
        validate_card_markdown(markdown)


def test_validate_v1_structure_identifies_the_failing_card() -> None:
    result = SimpleNamespace(scene_results=[
        SimpleNamespace(
            scene_id="work_communication",
            cards=[SimpleNamespace(markdown="# 标题\n\n缺少章节")],
        ),
    ])

    with pytest.raises(CardStructureError, match="work_communication card 1"):
        validate_v1_structure(result)


def test_ignores_shorter_same_symbol_fence_inside_a_longer_fence() -> None:
    markdown = """# 标题

核心判断。

## 依据

````markdown
# 仍在代码块内
```
## 一、仍在代码块内
| 仍在 | 代码块内 |
1. 仍在代码块内
````

事实。
"""

    signals = inspect_card_markdown(markdown)

    assert signals.h2_count == 1
    assert signals.table_count == 0
    assert signals.ordered_step_count == 0
    assert signals.manual_ordinal_headings == []
    validate_card_markdown(markdown)


@pytest.mark.parametrize(
    "block_lines",
    [
        ["- " + "列表项" * 80, "- " + "另一项" * 80],
        ["字段 | 值", "--- | ---", "状态 | " + "已确认" * 180],
        ["> " + "引用内容" * 120],
    ],
    ids=["list", "table", "blockquote"],
)
def test_does_not_merge_a_plain_intro_with_an_adjacent_block_into_a_long_paragraph(
    block_lines: list[str],
) -> None:
    markdown = "# 标题\n\n核心判断。\n\n## 详情\n\n引言没有空行。\n" + "\n".join(block_lines)

    signals = inspect_card_markdown(markdown)

    assert signals.long_paragraph_count == 0
    validate_card_markdown(markdown)


def test_recognizes_unbordered_gfm_tables_with_alignment_delimiters() -> None:
    markdown = """# 标题

核心判断。

## 详情

项目 | 状态
:--- | ---:
输入 | 已固定
"""

    signals = inspect_card_markdown(markdown)

    assert signals.table_count == 1
    validate_card_markdown(markdown)


def test_does_not_treat_ordinary_two_line_pipe_text_as_a_table() -> None:
    markdown = """# 标题

核心判断。

## 详情

甲 | 乙
丙 | 丁
"""

    assert inspect_card_markdown(markdown).table_count == 0
    validate_card_markdown(markdown)


def test_rejects_unbordered_table_with_an_invalid_delimiter_row() -> None:
    markdown = """# 标题

核心判断。

## 详情

项目 | 状态
:-- | -:
输入 | 已固定
"""

    with pytest.raises(CardStructureError, match="malformed table at line 7"):
        validate_card_markdown(markdown)


def test_core_information_block_excludes_thematic_breaks_and_html_comments() -> None:
    markdown = """# 标题

<!-- 仅供渲染器使用 -->

---

## 详情

事实。
"""

    with pytest.raises(CardStructureError, match="missing core information block before H2 at line 7"):
        validate_card_markdown(markdown)


def test_allows_version_and_date_headings_without_losing_manual_ordinal_gate() -> None:
    markdown = """# 标题

核心判断。

## 1.0 版本现状

事实。

### 2026.09 发布节奏

事实。
"""

    signals = inspect_card_markdown(markdown)

    assert signals.manual_ordinal_headings == []
    validate_card_markdown(markdown)

    with pytest.raises(CardStructureError, match="manual ordinal heading"):
        validate_card_markdown("# 标题\n\n核心判断。\n\n## 1. 议题\n\n事实。")


@pytest.mark.parametrize("heading", ["1、议题", "（一）议题", "一、议题"])
def test_rejects_other_manual_ordinal_heading_forms(heading: str) -> None:
    markdown = f"# 标题\n\n核心判断。\n\n## {heading}\n\n事实。"

    with pytest.raises(CardStructureError, match="manual ordinal heading at line 5"):
        validate_card_markdown(markdown)


@pytest.mark.parametrize("heading", ["1.Topic", "1、Topic", "（1）Topic"])
def test_rejects_english_manual_ordinal_headings_without_rejecting_versions(heading: str) -> None:
    markdown = f"# 标题\n\n核心判断。\n\n## {heading}\n\n事实。"

    with pytest.raises(CardStructureError, match="manual ordinal heading at line 5"):
        validate_card_markdown(markdown)

    validate_card_markdown("# 标题\n\n核心判断。\n\n## 1.0 Version status\n\n事实。")


def test_plain_pipe_text_does_not_split_a_genuine_long_paragraph() -> None:
    long_text = "连续正文" * 70
    markdown = (
        "# 标题\n\n核心判断。\n\n## 详情\n\n"
        + long_text
        + "\n甲 | 乙\n"
        + long_text
    )

    signals = inspect_card_markdown(markdown)

    assert signals.long_paragraph_count == 1
    with pytest.raises(CardStructureError, match="long paragraph at line 7"):
        validate_card_markdown(markdown)
