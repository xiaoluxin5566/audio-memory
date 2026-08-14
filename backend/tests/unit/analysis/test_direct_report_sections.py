from dataclasses import dataclass

import pytest

from audio_memory.analysis.direct_report_sections import (
    apply_section_revisions,
    split_report_sections,
)


REPORT = """# 一天的分析

开场文字。

## 今天发生了什么，重点改进什么

| 阶段 | 发生的事 | 对应的改进 |
| --- | --- | --- |
| 下午 | 面试 | 核实范围 |

## 工作与求职

原工作分析。

> “原话”

## 数据范围与判断边界

身份待核实。
"""


@dataclass(frozen=True)
class Revision:
    section_id: str
    title: str
    revised_markdown: str
    evidence_segment_ids: tuple[str, ...] = ("seg_0_1",)
    removes_repetition: bool = False
    repetition_reason: str | None = None


def test_sections_receive_stable_ids_and_exact_source_slices():
    sections = split_report_sections(REPORT)

    assert [(item.section_id, item.title) for item in sections] == [
        ("section_001", "今天发生了什么，重点改进什么"),
        ("section_002", "工作与求职"),
        ("section_003", "数据范围与判断边界"),
    ]
    for section in sections:
        assert REPORT[section.start:section.end] == section.markdown


def test_replacing_one_section_keeps_other_report_bytes_unchanged():
    original = split_report_sections(REPORT)
    revision = Revision(
        section_id="section_002",
        title="工作与求职",
        revised_markdown="## 工作与求职\n\n原工作分析。补充明确待办和证据。\n\n> “原话”\n",
    )

    revised = apply_section_revisions(REPORT, (revision,), {"seg_0_1"})
    current = split_report_sections(revised)

    assert current[0].markdown == original[0].markdown
    assert current[2].markdown == original[2].markdown
    assert "补充明确待办和证据" in current[1].markdown
    assert revised[: original[0].start] == REPORT[: original[0].start]


def test_replacement_without_trailing_newline_keeps_next_heading_separate():
    revision = Revision(
        section_id="section_002",
        title="工作与求职",
        revised_markdown="## 工作与求职\n\n原工作分析。补充明确待办和证据。",
    )

    revised = apply_section_revisions(REPORT, (revision,), {"seg_0_1"})

    assert "证据。\n\n## 数据范围与判断边界" in revised
    assert [item.title for item in split_report_sections(revised)] == [
        "今天发生了什么，重点改进什么",
        "工作与求职",
        "数据范围与判断边界",
    ]


@pytest.mark.parametrize(
    "revision, valid_ids, error",
    [
        (Revision("section_999", "工作与求职", "## 工作与求职\n\n足够长的修订正文。"), {"seg_0_1"}, "unknown section"),
        (Revision("section_002", "错误标题", "## 错误标题\n\n足够长的修订正文。"), {"seg_0_1"}, "title mismatch"),
        (Revision("section_002", "工作与求职", "## 工作与求职\n\n短。"), {"seg_0_1"}, "abnormally short"),
        (Revision("section_002", "工作与求职", "## 工作与求职\n\n原工作分析。补充明确内容。", ("seg_fake",)), {"seg_0_1"}, "unknown evidence"),
    ],
)
def test_invalid_local_revisions_are_rejected(revision, valid_ids, error):
    with pytest.raises(ValueError, match=error):
        apply_section_revisions(REPORT, (revision,), valid_ids)


def test_duplicate_section_revision_is_rejected():
    revision = Revision(
        "section_002", "工作与求职", "## 工作与求职\n\n原工作分析。补充明确待办和证据。"
    )
    with pytest.raises(ValueError, match="duplicate section"):
        apply_section_revisions(REPORT, (revision, revision), {"seg_0_1"})
