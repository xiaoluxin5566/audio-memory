from __future__ import annotations

import json

import pytest


SEGMENTS = [
    {"segment_id": "s0", "source_file": "day", "text": "开始讨论预算。"},
    {"segment_id": "s1", "source_file": "day", "text": "预算只有三万元。"},
    {"segment_id": "s2", "source_file": "day", "text": "决定比较两个方案。"},
]


class Scripted:
    def __init__(self):
        self.calls = []

    async def complete(self, **kw):
        stage = kw["stage"]
        data = json.loads(kw["user"].split(">", 1)[1].rsplit("</task_data>", 1)[0])
        self.calls.append((stage, data))
        if stage == "P4":
            assert kw["max_output_tokens"] == 24_000
        if stage == "P5":
            assert kw["max_output_tokens"] == 16_000
        await kw["before_request"]({"messages": []})
        await kw["after_response"]({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        if stage == "P1":
            value = {"status": "complete", "window_id": data["window_id"], "activities": [{"local_key": "a", "start_segment_id": "s0", "end_segment_id": "s2", "activity_kind": "conversation", "participation": "live", "purpose": "work", "subject": "预算方案", "continuation_note": None}], "topics": [{"local_key": "t1", "understanding": "预算约束", "open_questions": [], "evidence_anchor_ids": ["s1"]}, {"local_key": "t2", "understanding": "方案比较决定", "open_questions": [], "evidence_anchor_ids": ["s2"]}], "attention_signals": [{"local_key": "g", "topic_keys": ["t2"], "type": "decision", "description": "决定比较方案", "evidence_anchor_ids": ["s2"]}], "commitments": [], "context_requests": []}
        elif stage == "P2":
            assert data["activity_transcripts"][0]["complete_transcript"] == SEGMENTS
            value = {"status": "complete", "cards": [{"draft_key": "d1", "scene_id": "work_communication", "core_question": "预算约束是什么？", "source_activity_ids": ["activity_001"], "topic_assignments": [{"topic_id": "topic_001", "role": "core"}], "reader_need": "明确预算", "must_answer": ["预算是多少"], "must_cover": [{"description": "预算", "activity_id": "activity_001", "evidence_anchor_ids": ["s1"]}], "useful_deliverable": "预算边界", "research_decision": {"decision": "no_search", "reason": "原文足够", "task_keys": []}}, {"draft_key": "d2", "scene_id": "work_communication", "core_question": "如何比较？", "source_activity_ids": ["activity_001"], "topic_assignments": [{"topic_id": "topic_002", "role": "core"}], "reader_need": "开始比较", "must_answer": ["决定是什么"], "must_cover": [{"description": "比较决定", "activity_id": "activity_001", "evidence_anchor_ids": ["s2"]}], "useful_deliverable": "比较框架", "research_decision": {"decision": "no_search", "reason": "原文足够", "task_keys": []}}], "topic_dispositions": [{"topic_id": "topic_001", "state": "standalone", "card_keys": ["d1"], "reason": "独立且有用"}, {"topic_id": "topic_002", "state": "standalone", "card_keys": ["d2"], "reason": "独立且有用"}], "research_tasks": [], "context_requests": [], "budget_gaps": []}
        elif stage == "P4":
            required_anchor = data["card_brief"]["must_cover"][0]["evidence_anchor_ids"][0]
            value = {"status": "written", "card_id": data["card_id"], "title": "预算内先比较必要能力", "markdown": "预算只有三万元，因此先比较必要能力。\n\n## 比较重点\n\n先确认必要能力。", "evidence_refs": [{"body_locator": "预算只有三万元", "anchor_ids": [required_anchor]}], "research_refs": [], "uncertainties": []}
        elif stage == "P5":
            card = data["card"]
            assert "cards" not in data
            assert len(data["card_plan"]["topic_assignments"]) == 1
            value = {"status": "complete", "card": {"card_id": card["card_id"], "card_sha256": card["card_sha256"], "rubric_version": data["rubric_version"], "status": "scored", "dimensions": {"factual_accuracy": 15, "important_coverage": 15, "analysis_depth": 20, "actionability": 22, "expression_structure": 15}, "deductions": [{"dimension": "analysis_depth", "points": 5, "rule": "D1", "severity": "major", "body_locator": "因此", "reason": "缺少取舍分析", "evidence": "s1", "missing_content": "需要解释预算约束如何改变方案取舍"}, {"dimension": "actionability", "points": 8, "rule": "A1", "severity": "major", "body_locator": "比较必要能力", "reason": "没有说明如何开始", "evidence": "正文", "missing_content": "需要给出第一步比较动作和判断条件"}], "read_scope": ["s0", "s1", "s2"], "strengths": "事实准确", "weaknesses": "分析与行动不足", "missing_inputs": []}}
        else:
            pytest.fail(f"unexpected stage {stage}")
        return json.dumps(value, ensure_ascii=False)


@pytest.mark.asyncio
async def test_real_user_shape_runs_p1_p2_p4_p5_once_without_revision(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits
    transport = Scripted()
    pipeline = P1P5Pipeline(output_dir=tmp_path, transport=transport, limits=WritingLimits(allow_paid=True, max_requests=10, max_output_tokens=96_000), provider_id="deepseek", model_id="deepseek-v4-pro")
    result = await pipeline.run(SEGMENTS, user_corrections=[], report_period="today")
    assert [x[0] for x in transport.calls] == ["P1", "P2", "P4", "P4", "P5", "P5"]
    assert result["status"] == "scored_v1"
    assert result["scores"][0]["raw_total"] == 87
    assert len(result["scores"]) == 2
    assert result["metrics"]["scoring_model_request_count"] == 2
    assert result["scores"][0]["deductions"][0]["missing_content"] == "需要解释预算约束如何改变方案取舍"
    assert json.loads((tmp_path / "scoring-packet.json").read_text())["expected_scoring_model_calls"] == 2
    assert len(json.loads((tmp_path / "scores.json").read_text())["cards"]) == 2
    assert result["audit_performed"] is False
    assert result["revision_performed"] is False
    assert (tmp_path / "cards" / f"{result['cards'][0]['card_id']}.md").read_text().startswith("预算只有三万元")
    assert (tmp_path / "完整初稿.md").read_text().startswith("# 预算内先比较必要能力\n\n预算只有三万元")
    assert "P5" in [json.loads(p.read_text())["inputs"]["stage"] for p in (tmp_path / "stages").glob("*.json")]


@pytest.mark.asyncio
async def test_later_p4_failure_preserves_completed_cards_as_incomplete_draft(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits

    class InvalidSecondCard(Scripted):
        def __init__(self):
            super().__init__()
            self.p4_calls = 0

        async def complete(self, **kw):
            if kw["stage"] != "P4":
                return await super().complete(**kw)
            self.p4_calls += 1
            if self.p4_calls == 1:
                return await super().complete(**kw)
            await kw["before_request"]({"messages": []})
            await kw["after_response"]({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
            return json.dumps({"status": "written"})

    pipeline = P1P5Pipeline(
        output_dir=tmp_path,
        transport=InvalidSecondCard(),
        limits=WritingLimits(
            allow_paid=True,
            max_requests=10,
            max_output_tokens=96_000,
        ),
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
    )

    result = await pipeline.run(
        SEGMENTS,
        user_corrections=[],
        report_period="today",
    )

    assert result["status"] == "draft_incomplete"
    assert result["scoring_status"] == "pending"
    assert len(result["cards"]) == 1
    assert result["failure"]["code"] == "model_response_invalid"
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "draft_incomplete"


def test_prompt_manifest_includes_exactly_p1_through_p5():
    from hashlib import sha256
    from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts
    manifest = json.loads((P1P5Prompts.root / "MANIFEST.json").read_text())
    assert [item["prompt_id"] for item in P1P5Prompts.manifest()] == ["P1", "P2", "P3", "P4", "P5"]
    expected_hash = sha256(json.dumps({
        "prompts": manifest["prompts"],
        "p4_scene_prompts": manifest["p4_scene_prompts"],
        "input_template_sha256": manifest["input_template_sha256"],
        "rubric_sha256": manifest["rubric_sha256"],
        "protocol": P1P5Prompts.input_protocol_version,
    }, sort_keys=True).encode()).hexdigest()
    assert P1P5Prompts.fixed_rules_hash() == expected_hash
    assert "topic.ranges" not in P1P5Prompts.system("P1")
    assert "不是 Activity 的摘要" in P1P5Prompts.system("P1")
    assert "辅导一道题时孩子受挫" in P1P5Prompts.system("P1")
    assert "parent_activity_key" not in P1P5Prompts.system("P1")
    assert "程序根据 Anchor" in P1P5Prompts.system("P1")
    assert "standalone" in P1P5Prompts.system("P2")
    assert "媒体内容一律归 `content_consumption`" in P1P5Prompts.system("P2")
    assert "行动增益型搜索" in P1P5Prompts.system("P2")
    assert "内部事实不能公开搜索，不等于公开方法不能搜索" in P1P5Prompts.system("P2")
    assert "逐对比较候选卡片" in P1P5Prompts.system("P2")
    assert "反向漏失检查" in P1P5Prompts.system("P2")
    assert "P1 未登记" in P1P5Prompts.system("P2")
    assert "topic_assignments 可以为空" in P1P5Prompts.system("P2")
    assert "说话人簇只是语音分组参考" in P1P5Prompts.system("P2")
    assert "不得把 `speaker-N` 直接解释为用户" in P1P5Prompts.system("P2")
    assert "每个任务只调用一次 Kimi Search Pro" in P1P5Prompts.system("P3")
    assert "不再调用模型做二次规划或判断" in P1P5Prompts.system("P3")
    assert "任何空结果都不得伪造来源" in P1P5Prompts.system("P3")
    assert "不修改、重写" in P1P5Prompts.system("P5")
    assert "每次输入只包含一张 P4 主卡" in P1P5Prompts.system("P5")
    assert "missing_content" in P1P5Prompts.system("P5")
    assert "实体、时间和数字核对表" in P1P5Prompts.system("P5")
    assert "人物名称不一致" in P1P5Prompts.system("P4", "work_communication")
    assert "说话人簇只是语音分组参考" in P1P5Prompts.system("P4", "work_communication")
    assert "不得将其改写为‘你’" in P1P5Prompts.system("P4", "work_communication")
    assert "未经确认的说话人簇改写成‘你’" in P1P5Prompts.system("P5")


def test_p4_combines_shared_contract_with_exact_scene_prompt():
    from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts

    work = P1P5Prompts.system("P4", "work_communication")
    family = P1P5Prompts.system("P4", "parenting_family")

    assert "markdown` 中不得再写一级标题" in work
    assert "正文首个非空内容必须是一段非空的核心摘要" in work
    assert "二级至四级标题不得手工添加" in work
    assert "这些不是可选的文风建议" in work
    assert "三个以上时间节点" in work
    assert "触发条件、可能影响和对应处理" in work
    assert "避免汇报腔和旁观者口吻" in work
    assert "真正改变选择的约束及依赖" in work
    assert "真实家庭互动" not in work
    assert "真实家庭互动" in family
    assert "家庭出行或活动计划不能只复述时间表" in family
    assert "学习辅导、睡觉边界和情绪修复" in family
    assert "真正改变选择的约束及依赖" not in family


def test_p4_rejects_missing_or_unknown_scene():
    from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts

    with pytest.raises(ValueError, match="valid scene_id"):
        P1P5Prompts.system("P4")
    with pytest.raises(ValueError, match="valid scene_id"):
        P1P5Prompts.system("P4", "unknown")


def test_prepare_plan_counts_one_p5_call_per_main_card(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_cli import main

    source = tmp_path / "transcript.json"
    source.write_text(json.dumps(SEGMENTS, ensure_ascii=False))
    output = tmp_path / "prepared"

    assert main(["prepare", "--input", str(source), "--output", str(output)]) == 0
    counts = json.loads((output / "execution-plan.json").read_text())["counts"]
    assert counts["P5_scoring"] is None
    assert counts["P5_scoring_note"] == "N：每张主卡 1 次"
    assert counts["total_formula"] == "W + 1 + H + N + N"


def test_p5_losslessly_normalizes_score_fields_misplaced_beside_card(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_scoring import pending_score
    from audio_memory.analysis.beta8_writing_store import WritingLimits

    card = {"status": "written", "card_id": "c1", "title": "标题", "markdown": "正文", "evidence_refs": [], "research_refs": [], "uncertainties": []}
    score = pending_score(card, {})
    value = {
        "status": "complete",
        "card": {
            "card_id": "c1", "card_sha256": score["card_sha256"],
            "rubric_version": score["rubric_version"], "status": "scored",
            "dimensions": {"factual_accuracy": 15, "important_coverage": 15, "analysis_depth": 25, "actionability": 30, "expression_structure": 15},
        },
        "deductions": [], "read_scope": ["activity_1"], "strengths": "完整",
        "weaknesses": "无", "missing_inputs": [],
    }
    pipeline = P1P5Pipeline(output_dir=tmp_path, transport=Scripted(), limits=WritingLimits(), provider_id="deepseek", model_id="deepseek-v4-pro")

    normalized = pipeline._validate_assessment(value, card)

    assert set(normalized) == {"status", "card"}
    assert normalized["card"]["read_scope"] == ["activity_1"]
    assert normalized["card"]["raw_total"] == 100


def test_p5_losslessly_normalizes_score_fields_split_across_card_and_parent(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_scoring import pending_score
    from audio_memory.analysis.beta8_writing_store import WritingLimits

    card = {"status": "written", "card_id": "c1", "title": "标题", "markdown": "正文", "evidence_refs": [], "research_refs": [], "uncertainties": []}
    score = pending_score(card, {})
    value = {
        "status": "complete",
        "card": {
            "card_id": "c1", "card_sha256": score["card_sha256"],
            "rubric_version": score["rubric_version"], "status": "scored",
            "dimensions": {"factual_accuracy": 15, "important_coverage": 15, "analysis_depth": 25, "actionability": 30, "expression_structure": 15},
            "deductions": [],
        },
        "read_scope": ["activity_1"], "strengths": "完整",
        "weaknesses": "无", "missing_inputs": [],
    }
    pipeline = P1P5Pipeline(output_dir=tmp_path, transport=Scripted(), limits=WritingLimits(), provider_id="deepseek", model_id="deepseek-v4-pro")

    normalized = pipeline._validate_assessment(value, card)

    assert normalized["card"]["deductions"] == []
    assert normalized["card"]["read_scope"] == ["activity_1"]
    assert normalized["card"]["raw_total"] == 100


@pytest.mark.asyncio
async def test_p3_missing_default_search_key_degrades_without_blocking_report(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStore
    from audio_memory.analysis.errors import ProviderAnalysisError

    class MissingKey:
        async def complete(self, **kwargs):
            raise ProviderAnalysisError(
                "Search credential is unavailable",
                code="keychain_unavailable",
                pause_batch=True,
            )

    pipeline = P1P5Pipeline(
        output_dir=tmp_path, transport=MissingKey(),
        limits=WritingLimits(allow_paid=True, max_requests=3),
        provider_id="deepseek", model_id="deepseek-v4-pro",
        search_provider_id="kimi", search_model_id="kimi-k2.6",
    )
    pipeline.store = WritingStore(tmp_path, {"test": "missing-search-key"})
    task = {"task_key": "research-1"}

    result = await pipeline._research_task(
        {"task_key": "research-1", "question": "阿那亚有哪些适合孩子的景点？"},
        task,
    )

    assert result["status"] == "failed"
    assert result["execution_status"] == "failed"
    assert result["failure_code"] == "keychain_unavailable"


def test_p3_accepts_provider_version_constraint_alias_without_new_search(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits
    pipeline = P1P5Pipeline(
        output_dir=tmp_path, transport=Scripted(),
        limits=WritingLimits(allow_paid=True, max_requests=10),
        provider_id="deepseek", model_id="deepseek-v4-pro",
    )
    value = {
        "task_key": "r1", "status": "partial", "answer": "部分找到",
        "findings": [{"statement": "x", "source_keys": ["s1"], "applicability": "a", "limitations": "l"}],
        "sources": [{
            "local_source_key": "s1", "title": "t", "url": "https://example.com",
            "publisher": None, "published_at": None, "version_constraint": None,
            "access_level": "search_snippet", "quote": "q", "locator": None,
            "context_note": "c",
        }],
        "unresolved_questions": [],
    }

    normalized = pipeline._validate_research(value, {"task_key": "r1"})

    assert normalized["sources"][0]["version"] is None
    assert "version_constraint" not in normalized["sources"][0]


@pytest.mark.asyncio
async def test_p3_executes_one_search_pro_request_for_each_p2_research_need(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits
    pipeline = P1P5Pipeline(
        output_dir=tmp_path, transport=Scripted(),
        limits=WritingLimits(allow_paid=True, max_requests=10),
        provider_id="deepseek", model_id="deepseek-v4-pro",
    )
    calls = []

    async def research(public_task, task, *, request_limit):
        calls.append((public_task, task, request_limit))
        return {
            "task_key": task["task_key"], "status": "not_found",
            "answer": task["task_key"], "findings": [], "sources": [],
            "unresolved_questions": [],
        }

    pipeline._research_task = research
    public = {
        "task_key": "r", "question": "阿那亚有哪些适合亲子的一日活动？", "purpose": "帮助明天安排行程",
        "public_context": "目的地是秦皇岛阿那亚，同行有儿童", "source_requirements": "官方景区与场馆页面优先",
        "jurisdiction": None, "as_of": "2026-09-10",
        "version_constraint": None, "stop_condition": "找到活动特色、开放预约和交通限制", "source_limit": 5,
        "reusable_sources": [],
    }

    result = await pipeline._focused_research(public, {"task_key": "r"})

    assert len(calls) == 1
    assert calls[0][0] == public
    assert calls[0][1] == {"task_key": "r"}
    assert calls[0][2] == 1
    assert result["focused_task_count"] == 1


@pytest.mark.asyncio
async def test_p3_keeps_one_request_when_search_pro_returns_sufficient(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits

    pipeline = P1P5Pipeline(
        output_dir=tmp_path, transport=Scripted(),
        limits=WritingLimits(allow_paid=True, max_requests=10),
        provider_id="deepseek", model_id="deepseek-v4-pro",
    )
    calls = []

    async def research(public_task, task, *, request_limit):
        calls.append(public_task["task_key"])
        return {
            "task_key": task["task_key"], "status": "sufficient",
            "answer": "已满足原任务停止条件", "findings": [], "sources": [],
            "unresolved_questions": [],
        }

    pipeline._research_task = research
    public = {
        "task_key": "r", "question": "明天开放吗？", "purpose": "安排行程",
        "public_context": "阿那亚", "source_requirements": "官方页面",
        "jurisdiction": None, "as_of": "2026-09-10", "version_constraint": None,
        "stop_condition": "确认开放时间", "source_limit": 5, "reusable_sources": [],
    }

    result = await pipeline._focused_research(public, {"task_key": "r"})

    assert calls == ["r"]
    assert result["status"] == "sufficient"
    assert result["focused_task_count"] == 1


@pytest.mark.asyncio
async def test_p3_preserves_verified_sources_from_the_single_search_pro_result(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits
    from audio_memory.prompts.beta8_pipeline_schema import stable_source_id

    pipeline = P1P5Pipeline(
        output_dir=tmp_path,
        transport=Scripted(),
        limits=WritingLimits(allow_paid=True, max_requests=10),
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
    )

    async def research(public_task, task, *, request_limit):
        return {
            "task_key": task["task_key"],
            "status": "partial",
            "answer": "已取得页面片段",
            "findings": [{
                "statement": "需预约",
                "source_keys": ["official"],
                "applicability": "当前行程",
                "limitations": "以官方页面为准",
            }],
            "sources": [{
                "local_source_key": "official",
                "source_id": stable_source_id("https://example.com/guide"),
                "title": "官方指南",
                "url": "https://example.com/guide",
                "quote_verified": True,
            }],
            "unresolved_questions": [],
        }

    pipeline._research_task = research
    public = {
        "task_key": "r",
        "question": "有哪些适合亲子的活动？",
        "purpose": "安排行程",
        "public_context": "亲子出行",
        "source_requirements": "官方页面",
        "jurisdiction": None,
        "as_of": "2026-09-10",
        "version_constraint": None,
        "stop_condition": "找到开放和预约条件",
        "source_limit": 5,
        "reusable_sources": [],
    }

    result = await pipeline._focused_research(public, {"task_key": "r"})

    assert len(result["sources"]) == 1
    assert len(result["findings"]) == 1
    assert {tuple(item["source_keys"]) for item in result["findings"]} == {
        (result["sources"][0]["local_source_key"],)
    }


@pytest.mark.asyncio
async def test_p3_verifies_search_pro_chunks_without_refetching_the_page(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStore

    class MustNotFetch:
        async def fetch(self, url):
            pytest.fail(f"Search Pro evidence must not be refetched: {url}")

    pipeline = P1P5Pipeline(
        output_dir=tmp_path, transport=Scripted(),
        limits=WritingLimits(allow_paid=True, max_requests=10),
        provider_id="deepseek", model_id="deepseek-v4-pro",
        source_fetcher=MustNotFetch(),
    )
    pipeline.store = WritingStore(tmp_path, {"test": "search-pro-proof"})
    candidate = {
        "task_key": "r1", "status": "partial", "answer": "已取得片段",
        "findings": [], "unresolved_questions": [],
        "sources": [{
            "local_source_key": "search_pro_1", "title": "官方指南",
            "url": "https://example.com/guide?utm_source=test",
            "publisher": "官方网站", "published_at": "2026-09-19", "version": None,
            "access_level": "original_text", "quote": "需提前预约。",
            "locator": None, "context_note": "开放提示",
            "retrieval_provenance": "kimi_search_pro",
            "retrieved_text": "每日 09:00 开放，需提前预约。儿童须由成人陪同。",
        }],
    }

    result = await pipeline._verify_research(candidate)

    source = result["sources"][0]
    assert source["quote_verified"] is True
    assert source["verified_quote"] == "需提前预约。"
    assert source["url"] == "https://example.com/guide"
    assert source["fetch"]["retrieval_method"] == "kimi_search_pro"
    assert source["fetch"]["content_sha256"]
    repeated = await pipeline._verify_research(candidate)
    assert repeated["sources"][0]["fetch"] == source["fetch"]


@pytest.mark.asyncio
async def test_p3_rejects_a_search_pro_quote_missing_from_returned_chunks(tmp_path):
    from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStore

    pipeline = P1P5Pipeline(
        output_dir=tmp_path, transport=Scripted(),
        limits=WritingLimits(allow_paid=True, max_requests=10),
        provider_id="deepseek", model_id="deepseek-v4-pro",
    )
    pipeline.store = WritingStore(tmp_path, {"test": "search-pro-mismatch"})
    candidate = {
        "task_key": "r1", "status": "partial", "answer": "已取得片段",
        "findings": [], "unresolved_questions": [],
        "sources": [{
            "local_source_key": "search_pro_1", "title": "官方指南",
            "url": "https://example.com/guide", "publisher": "官方网站",
            "published_at": None, "version": None,
            "access_level": "original_text", "quote": "需提前预约。",
            "locator": None, "context_note": "开放提示",
            "retrieval_provenance": "kimi_search_pro",
            "retrieved_text": "每日 09:00 开放。",
        }],
    }

    result = await pipeline._verify_research(candidate)

    assert result["status"] == "failed"
    assert result["sources"][0]["quote_verified"] is False
    assert "No verified source text" in result["unresolved_questions"][-1]
