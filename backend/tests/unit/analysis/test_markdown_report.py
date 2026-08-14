import pytest

from audio_memory.analysis.markdown_report import MarkdownReportResult


def test_markdown_report_derives_card_metadata_without_json_wrapper() -> None:
    result = MarkdownReportResult.from_markdown(
        "# 7月29日全天录音分析\n\n## 核心结论\n\n今天最值得关注的是工作选择与亲子辅导。\n\n## 工作\n\n仍有两个问题未闭环。"
    )

    assert result.title == "7月29日全天录音分析"
    assert result.summary == "今天最值得关注的是工作选择与亲子辅导。"
    assert result.report_markdown.startswith("# 7月29日")


def test_markdown_report_allows_verified_https_images() -> None:
    result = MarkdownReportResult.from_markdown(
        "# 今日报告\n\n## 核心结论\n\n![概念示意图](https://images.example.com/map.png)"
    )
    assert "![概念示意图]" in result.report_markdown


@pytest.mark.parametrize(
    "markdown",
    [
        "# 今日报告\n\n![危险](javascript:alert(1))",
        "# 今日报告\n\n![本地](file:///tmp/a.png)",
        "# 今日报告\n\n<script>alert(1)</script>",
    ],
)
def test_markdown_report_rejects_unsafe_active_content(markdown: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        MarkdownReportResult.from_markdown(markdown)
