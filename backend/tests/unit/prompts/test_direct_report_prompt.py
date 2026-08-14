import json

from audio_memory.analysis.direct_report_sections import split_report_sections
from audio_memory.analysis.direct_report_annotations import parse_report_blocks
from audio_memory.prompts.composer import PromptComposer


def test_direct_report_prompt_keeps_analysis_depth_and_requests_semantic_json() -> None:
    request = PromptComposer().compose_direct_report(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 讨论交付。",
        profile=[{"dimension": "工作", "value": "关注 AI 硬件"}],
        user_analysis_prompt="重点分析工作和家庭。",
    )

    assert request.scene_id == "direct-report"
    assert request.max_tokens == 32_768
    assert request.timeout_seconds == 900
    assert request.response_format == "json_object"
    assert json.loads(request.schema_json)["properties"]["schema_version"]["const"] == 1
    assert "关注 AI 硬件" in request.user_data
    assert "seg_0_0" in request.user_data
    assert "不代表本次事实" in request.instructions
    assert "我猜测" in request.instructions
    assert "工作与正式沟通" in request.instructions
    assert "家庭沟通" in request.instructions
    assert "亲子教育与互动" in request.instructions
    assert "情绪与心理状态" in request.instructions
    assert "健康与身体状态" in request.instructions
    assert "内容消费" in request.instructions
    assert "只输出一个符合 Schema 的 JSON 对象" in request.instructions
    assert "source_quote" in request.instructions
    assert "suggested_wording" in request.instructions
    assert "不要输出 Markdown" in request.instructions
    assert "不能因为结构化输出而压缩分析深度" in request.instructions
    assert "事实与不确定性边界" in request.instructions
    assert "原因、影响、具体动作、适用边界和成功信号" in request.instructions
    assert "type 必须逐字使用 Schema 中的枚举值" in request.instructions
    assert "不能改写成 bulleted_list" in request.instructions


def test_direct_report_prompt_does_not_delegate_ui_to_model() -> None:
    request = PromptComposer().compose_direct_report(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 讨论交付。",
        profile=[],
        user_analysis_prompt="完整分析。",
    )

    forbidden = (
        "字号",
        "字体",
        "颜色",
        "间距",
        "分隔线",
        "卡片样式",
        "圆角",
        "阴影",
        "响应式布局",
        "移动端布局",
    )
    assert not any(term in request.instructions for term in forbidden)


def test_light_direct_report_keeps_quality_rules_but_removes_block_contract() -> None:
    request = PromptComposer().compose_direct_report_light(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 讨论交付。",
        profile=[],
        user_analysis_prompt="完整分析。",
    )

    schema = json.loads(request.schema_json)
    section = schema["$defs"]["LightReportSection"]["properties"]
    assert set(section) == {"title", "content", "evidence_segment_ids"}
    assert "blocks" not in request.schema_json
    assert "source_quote" not in request.schema_json
    assert "不能因为结构化输出而压缩分析深度" in request.instructions
    assert "不允许借此省略重要事实、推理、证据或可执行建议" in request.instructions
    assert request.response_format == "json_object"


def test_markdown_report_uses_only_the_ui_markers_the_frontend_renders() -> None:
    request = PromptComposer().compose_direct_report_markdown(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 讨论交付。",
        profile=[],
        user_analysis_prompt="完整分析。",
    )

    assert request.response_format == "text"
    assert "`#`：页面大标题" in request.instructions
    assert "`##`：一级标题" in request.instructions
    assert "`###`：二级标题" in request.instructions
    assert "`>`：逐字稿原文引用" in request.instructions
    assert "Markdown 表格" in request.instructions
    assert "Markdown 图片" not in request.instructions
    assert "卡片样式" not in request.instructions
    assert "行动总结型" in request.instructions
    assert "下一份工作要选对，和孩子沟通要慢一点" in request.instructions
    assert "一天录音里的" in request.instructions


def test_review_prompt_reads_full_transcript_and_requests_only_local_revisions() -> None:
    draft = """# 标题

## 今天发生了什么，重点改进什么

概览。

## 工作与求职

初稿内容。
"""
    request = PromptComposer().compose_direct_report_review(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 我答应补发 PDF 简历。",
        profile=[{"dimension": "工作", "value": "AI 产品"}],
        user_analysis_prompt="完整分析。",
        initial_report_markdown=draft,
        sections=split_report_sections(draft),
        gate_failures=("analysis_depth",),
        segment_count=1,
    )

    schema = json.loads(request.schema_json)
    assert request.scene_id == "direct-report-review"
    assert request.response_format == "json_object"
    assert schema["title"] == "DirectReportReview"
    assert "seg_0_0" in request.user_data
    assert "初稿内容" in request.user_data
    assert "section_002" in request.user_data
    assert "AI 产品" in request.user_data
    assert "完整修订章节" in request.instructions
    assert "不要输出整篇报告" in request.instructions
    assert "检查页面大标题" in request.instructions
    assert "revised_title" in request.schema_json


def test_annotation_prompt_receives_blocks_but_never_transcript_or_profile() -> None:
    blocks = parse_report_blocks("# 标题\n\n## 工作\n\n正文。")
    request = PromptComposer().compose_direct_report_annotations(blocks=blocks)

    schema = json.loads(request.schema_json)
    assert request.scene_id == "direct-report-annotations"
    assert request.response_format == "json_object"
    assert schema["title"] == "DirectReportAnnotations"
    assert "block_001" in request.user_data
    assert "# 标题" in request.user_data
    assert "transcript" not in request.user_data
    assert "profile" not in request.user_data
    assert '"text"' not in request.schema_json
