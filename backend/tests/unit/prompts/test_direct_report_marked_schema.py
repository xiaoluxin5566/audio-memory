import json

from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.direct_report_marked_schema import DirectReportMarkedDocument
from audio_memory.analysis.direct_report_marked_document import marked_report_markdown


def valid_document():
    return {
        "schema_version": 1,
        "title": "一天中的工作与家庭",
        "overview": {
            "summary": "全天概览。",
            "rows": [{
                "phase": "下午", "event": "参加面试", "improvement": "核实岗位范围",
                "evidence_segment_ids": ["seg-1"],
            }],
        },
        "sections": [{
            "title": "工作与面试",
            "blocks": [
                {"type": "paragraph", "text": "完整分析正文。"},
                {"type": "subheading", "title": "可以重点改进的地方"},
                {"type": "quote", "text": "我确实挺感兴趣的", "evidence_segment_ids": ["seg-1"]},
                {"type": "bullet_list", "items": ["核实 scope"]},
                {"type": "numbered_list", "items": ["发送简历"]},
                {"type": "table", "columns": ["机会", "判断"], "rows": [["教育公司", "继续了解"]]},
            ],
        }],
        "todos": [], "evidence_segment_ids": [], "external_source_ids": [],
    }


def test_marked_schema_accepts_only_report_markers_the_ui_renders():
    document = DirectReportMarkedDocument.model_validate(valid_document())
    schema_text = json.dumps(document.model_json_schema(), ensure_ascii=False)

    assert len(document.sections[0].blocks) == 6
    assert "suggested_wording" not in schema_text
    assert "subsection" not in schema_text


def test_marked_prompt_preserves_baseline_analysis_rules_without_ui_styling():
    request = PromptComposer().compose_direct_report_marked(
        transcript_markdown="# 全天录音逐字稿\n\n[seg-1] 参加面试。",
        profile=[],
        user_analysis_prompt="完整分析。",
    )

    assert request.response_format == "json_object"
    assert "请完整阅读逐字稿，把复杂的一天讲清楚" in request.instructions
    assert "paragraph" in request.schema_json
    assert "subheading" in request.schema_json
    assert "quote" in request.schema_json
    assert "bullet_list" in request.schema_json
    assert "numbered_list" in request.schema_json
    assert "table" in request.schema_json
    assert "suggested_wording" not in request.schema_json
    assert "字号" not in request.instructions
    assert "颜色" not in request.instructions


def test_marked_document_serializes_all_supported_markers_without_losing_text():
    markdown = marked_report_markdown(DirectReportMarkedDocument.model_validate(valid_document()))

    assert "# 一天中的工作与家庭" in markdown
    assert "## 工作与面试" in markdown
    assert "### 可以重点改进的地方" in markdown
    assert "> “我确实挺感兴趣的”" in markdown
    assert "- 核实 scope" in markdown
    assert "1. 发送简历" in markdown
    assert "| 机会 | 判断 |" in markdown
