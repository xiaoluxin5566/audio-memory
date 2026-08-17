import json

from audio_memory.analysis.direct_report_sections import split_report_sections
from audio_memory.analysis.direct_report_annotations import parse_report_blocks
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.direct_report_audit_schema import ReportAudit
from audio_memory.prompts.direct_report_revision_schema import TargetedReportRevision


def audit(mode: str = "full_v1_audit", *, with_issue: bool = True) -> ReportAudit:
    issues = []
    unresolved = []
    if with_issue:
        issues = [{
            "issue_id": "issue_001",
            "severity": "major",
            "issue_type": "factual_error",
            "section_id": "section_002",
            "problem": "职业选择被写错。",
            "importance": "影响核心判断。",
            "required_change": "改为加入创业公司。",
            "affected_claims": ["准备自己创业。"],
            "evidence_segment_ids": ["seg_0_0"],
            "evidence_excerpts": [{"segment_id": "seg_0_0", "text": "我考虑加入创业公司。"}],
            "context_excerpts": [],
            "allow_deletion_or_compression": False,
        }]
        unresolved = ["issue_001"]
    return ReportAudit.model_validate({
        "audit_mode": mode,
        "rubric_version": 1,
        "passed": not with_issue,
        "scores": {
            "factual_accuracy": 20 if with_issue else 28,
            "important_coverage": 20 if with_issue else 23,
            "analysis_depth": 15 if with_issue else 18,
            "actionability": 9 if with_issue else 13,
            "expression_structure": 5 if with_issue else 9,
            "total": 69 if with_issue else 91,
        },
        "deductions": [{"dimension": "factual_accuracy", "points": 10, "reason": "职业选择错误。"}] if with_issue else [],
        "coverage": {
            "full_transcript_reviewed": mode == "full_v1_audit",
            "reviewed_segment_count": 1 if mode in {"full_v1_audit", "chunk_v1_audit"} else None,
            "total_segment_count": 1 if mode in {"full_v1_audit", "chunk_v1_audit"} else None,
            "unreviewed_ranges": [],
            "summary": "全量检查完成。" if mode == "full_v1_audit" else "完成有界终审。",
        },
        "issues": issues,
        "unresolved_issue_ids": unresolved,
        "summary": "审核结果。",
    })


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
    assert "标题只表达当天最重要的一项进展、矛盾或判断" in request.instructions
    assert "不要为了覆盖全文而拼接两个不相关主题" in request.instructions
    assert "结构服务内容" in request.instructions
    assert "每一点需要较长解释时" in request.instructions
    assert "合理推断" in request.instructions
    assert "通用知识" in request.instructions
    assert "一天录音里的" in request.instructions


def test_markdown_report_prompt_hides_internal_analysis_and_requires_user_ready_advice() -> None:
    request = PromptComposer().compose_direct_report_markdown(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 讨论交付。",
        profile=[],
        user_analysis_prompt="完整分析。",
    )

    assert "不要向读者展示你的证据审查或转写处理过程" in request.instructions
    assert "低价值且不能改变读者行动的线索，直接不写" in request.instructions
    assert "不够人话、必须避免的标题" in request.instructions
    assert "建议必须交付可直接使用的内容" in request.instructions
    assert "三项及以上可横向比较的信息，优先使用 Markdown 表格" in request.instructions
    assert "今天发生了什么，一些改进建议" in request.instructions
    assert "不要强制输出“数据范围与判断边界”章节" in request.instructions
    assert "这是一个示例，供你参考" in request.instructions
    assert "所有场景一律遵循同一原则" in request.instructions
    assert "真实出现，且存在值得读者关注的事实、问题、机会或决策价值" in request.instructions
    assert "重点场景只是扫描与召回提示，不是必写栏目" in request.instructions
    assert "工作与职业、家庭与关系、学习与内容消费、生活事务与消费、健康与安全、待办与计划" in request.instructions
    assert "不代表最终报告必须写这些内容" in request.instructions
    assert "面试、谈判、亲子辅导等分析规则" not in request.instructions
    assert "面试、沟通、谈判或亲子教育" not in request.instructions
    assert "尽量提炼其中对读者有用的知识" in request.instructions
    assert "相关的延伸知识、选择建议或相关推荐" in request.instructions
    assert "证据能支持到哪一层" in request.instructions
    assert "不得自行补齐金额单位" in request.instructions
    assert "建议是否应该存在" in request.instructions
    assert "媒体预测不能直接变成购买建议" in request.instructions
    assert "模型建议不是读者待办" in request.instructions
    assert "每个被选入报告的重要主题" in request.instructions
    assert "超出事实复述的新价值" in request.instructions
    assert "概念解释、运行机制、为什么重要" in request.instructions
    assert "不要机械要求每个主题都补充定义" in request.instructions
    assert "分析深度不是越高越好" in request.instructions
    assert "不得为了显得深入而制造复杂解释" in request.instructions


def test_audit_prompt_blocks_internal_process_leaks_and_empty_advice() -> None:
    request = PromptComposer().compose_full_report_audit(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 我考虑加入创业公司。",
        profile=[],
        user_analysis_prompt="完整分析。",
        v1_markdown="# 标题\n\n## 工作\n\n初稿。",
        sections=split_report_sections("# 标题\n\n## 工作\n\n初稿。"),
        gate_failures=(),
        segment_count=1,
    )

    assert "内部过程泄露" in request.instructions
    assert "空泛建议或未交付" in request.instructions
    assert "硬凑低价值章节" in request.instructions
    assert "至少记为一个 `major` 问题" in request.instructions
    assert "所有场景使用同一审核标准" in request.instructions
    assert "没有出现或没有重点问题的场景不生成、不补写、不扣分" in request.instructions
    assert "重点场景清单只用于检查是否漏看" in request.instructions
    assert "不能据此要求补齐栏目" in request.instructions
    assert "report_section_<三位章节号>" in request.instructions
    assert "报告正文证据" in request.instructions
    assert "每一个 evidence_segment_ids 中的 ID" in request.instructions
    assert "必须在 evidence_excerpts 中恰好有对应原文" in request.instructions
    assert "建议读者筛选、拆分、排除或改变录音内容" in request.instructions
    assert "录音大部分来自" in request.instructions
    assert "枚举、层级和分类关系" in request.instructions
    assert "不得把两组并列概念合并成一组" in request.instructions
    assert "数据类型与记忆层级" in request.instructions
    assert "一条摘录不得拼接相邻片段" in request.instructions
    assert "面试、关键沟通、亲子冲突、健康选择" not in request.instructions
    assert "先判断这条建议是否应该存在" in request.instructions
    assert "报告内部自相矛盾" in request.instructions
    assert "普通转述不要求逐字一致" in request.instructions
    assert "结论强度" in request.instructions
    assert "分析深度不是越高越好" in request.instructions
    assert "过度分析" in request.instructions
    assert "过度建议" in request.instructions


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


def test_full_v1_audit_receives_transcript_and_scored_audit_schema() -> None:
    draft = "# 标题\n\n## 工作\n\n初稿。"
    request = PromptComposer().compose_full_report_audit(
        transcript_markdown="# 全天录音逐字稿\n\n[seg_0_0] 我考虑加入创业公司。",
        profile=[{"dimension": "工作", "value": "AI 产品"}],
        user_analysis_prompt="完整分析。",
        v1_markdown=draft,
        sections=split_report_sections(draft),
        gate_failures=("analysis_depth",),
        segment_count=1,
    )

    assert request.scene_id == "direct-report-audit-v1"
    assert json.loads(request.schema_json)["title"] == "ReportAudit"
    assert "full_v1_audit" in request.instructions
    assert '"expected_total_segment_count":1' in request.user_data
    assert "seg_0_0" in request.user_data
    assert "初稿" in request.user_data
    assert "AI 产品" in request.user_data


def test_segmented_audit_uses_one_chunk_template_and_a_merge_request() -> None:
    composer = PromptComposer()
    draft = "# 标题\n\n## 工作\n\n初稿。"
    sections = split_report_sections(draft)
    chunk = composer.compose_report_audit_chunk(
        transcript_markdown="# 分段逐字稿\n\n[seg_0_0] 考虑离职。",
        profile=[], user_analysis_prompt="完整分析。",
        v1_markdown=draft, sections=sections, gate_failures=(),
        chunk_index=1, chunk_count=3, segment_count=1,
        total_segment_count=3,
    )

    assert chunk.scene_id == "direct-report-audit-chunk"
    assert "audit_mode=chunk_v1_audit" in chunk.instructions
    assert '"chunk_index":1' in chunk.user_data
    assert '"chunk_count":3' in chunk.user_data
    assert "证据是否足以支持结论" in chunk.instructions
    assert "价值提升机会" in chunk.instructions
    assert "包括遗漏问题" in chunk.instructions
    assert "不重新定义全天主题" in chunk.instructions

    chunk_audit = audit(mode="chunk_v1_audit", with_issue=False)
    merged = composer.compose_merged_report_audit(
        v1_markdown=draft, sections=sections, gate_failures=(),
        chunk_audits=[chunk_audit], total_segment_count=3,
    )

    assert merged.scene_id == "direct-report-audit-merge"
    assert "audit_mode=full_v1_audit" in merged.instructions
    assert "原子问题" in merged.instructions
    assert "全局主编" in merged.instructions
    assert "不得输出新的场景底图" in merged.instructions
    assert "chunk_v1_audit" in merged.user_data


def test_targeted_revision_receives_issue_evidence_without_full_transcript() -> None:
    request = PromptComposer().compose_targeted_report_revision(
        v1_title="标题",
        section_outline=[{"section_id": "section_002", "title": "工作"}],
        editable_sections=[{"section_id": "section_002", "title": "工作", "markdown": "## 工作\n\n准备自己创业。"}],
        adjacent_sections=[],
        audit=audit(),
        allowed_segment_ids={"seg_0_0"},
    )

    assert request.scene_id == "direct-report-revision"
    assert json.loads(request.schema_json)["title"] == "TargetedReportRevision"
    assert "seg_0_0" in request.user_data
    assert "我考虑加入创业公司" in request.user_data
    assert "untrusted_transcript_markdown" not in request.user_data
    assert "每个原子问题的错误位置" in request.instructions
    assert "不得只在另一处做类似修改" in request.instructions
    assert "这是一个示例，供你参考" in request.instructions
    assert "内容消费" in request.instructions
    assert "未选中的章节必须原样保留" in request.instructions
    assert "allow_section_rewrite" in request.instructions


def test_final_audit_receives_revision_diff_without_full_transcript() -> None:
    revision = TargetedReportRevision.model_validate({
        "revisions": [{
            "section_id": "section_002",
            "title": "工作",
            "revised_markdown": "## 工作\n\n考虑加入创业公司。",
            "issues_resolved": ["issue_001"],
            "evidence_segment_ids": ["seg_0_0"],
            "removes_repetition": False,
            "repetition_reason": None,
        }],
        "unresolved_issue_ids": [],
        "revision_summary": "修正职业选择。",
    })
    request = PromptComposer().compose_revision_final_audit(
        v2_markdown="# 标题\n\n## 工作\n\n考虑加入创业公司。",
        section_diffs=[{
            "section_id": "section_002",
            "before": "## 工作\n\n准备自己创业。",
            "after": "## 工作\n\n考虑加入创业公司。",
        }],
        v1_audit=audit(),
        revision=revision,
    )

    assert request.scene_id == "direct-report-audit-final"
    assert "revision_final_audit" in request.instructions
    assert "seg_0_0" in request.user_data
    assert "untrusted_transcript_markdown" not in request.user_data
    assert "targeted_revision" not in request.user_data
    assert "revised_markdown" not in request.user_data
    assert "不得要求第二次修改" in request.instructions
    assert "不是对全量逐字稿的重新回归" in request.instructions
    assert "不得声称已完成全量事实和覆盖重审" in request.instructions


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
