from audio_memory.analysis.direct_markdown_quality import evaluate_direct_markdown_quality


COMPLETE = """# 一天的分析

## 今天发生了什么，重点改进什么

今天有两条重要主线。

| 阶段 | 发生的事 | 对应的改进 |
| --- | --- | --- |
| 下午 | 参加面试 | 核实岗位范围 |

## 工作与面试

这里是完整分析。

## 家庭沟通

这里是完整分析。

## 数据范围与判断边界

部分说话人身份待核实。
"""


def test_quality_gate_accepts_complete_markdown_contract():
    result = evaluate_direct_markdown_quality(COMPLETE, transcript_chars=2_000)
    assert result.passed is True
    assert result.failures == ()


def test_quality_gate_rejects_missing_overview_table_and_boundary():
    result = evaluate_direct_markdown_quality(
        "# 标题\n\n## 工作\n\n只有一段。", transcript_chars=2_000
    )
    assert set(result.failures) == {"overview_table", "detail_depth", "judgment_boundary"}


def test_quality_gate_flags_abnormal_compression_for_large_transcript():
    result = evaluate_direct_markdown_quality(COMPLETE, transcript_chars=300_000)
    assert result.passed is False
    assert "analysis_depth" in result.failures
