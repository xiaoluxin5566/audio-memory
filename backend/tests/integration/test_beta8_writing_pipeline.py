from __future__ import annotations

import json

import pytest


SEGMENTS = [{"segment_id": f"s{i}", "source_file": "meeting", "text": text} for i, text in enumerate([
    "先把预算限制写清楚。", "预算只有三万元。", "先比较两个可行方案。"
])]


class ScriptedTransport:
    def __init__(self, *, search=False, invalid=False, partial=False):
        self.calls = []
        self.search = search
        self.invalid = invalid
        self.partial = partial

    async def complete(self, **kwargs):
        stage = kwargs["stage"]
        data = json.loads(kwargs["user"].split(">", 1)[1].rsplit("</task_data>", 1)[0])
        self.calls.append((stage, data))
        for _ in range(2 if stage == "P3" else 1):
            await kwargs["before_request"]({"messages": [{"role": "system", "content": kwargs["system"]}, {"role": "user", "content": kwargs["user"]}]})
            await kwargs["after_response"]({"usage": {"prompt_tokens": 50, "completion_tokens": 10}, "choices": [{"message": {"content": "response"}, "finish_reason": "stop"}]})
        if self.invalid:
            return "{broken"
        if stage == "P1":
            value = {"status": "complete", "window_id": data["window_id"], "activities": [{"local_key": "a", "start_segment_id": "s0", "end_segment_id": "s2", "activity_kind": "conversation", "participation": "user_present_live_interaction", "purpose": "work", "subject": "方案沟通", "continuation_note": None}], "topics": [{"local_key": "t", "parent_activity_key": "a", "ranges": [{"source_file": "meeting", "start_segment_id": "s0", "end_segment_id": "s2"}], "provenance": "现场互动", "understanding": "预算三万元，要比较两个方案。", "open_questions": ["哪种方案适合预算"], "evidence_anchors": [{"segment_id": "s1", "quote": "预算只有三万元。"}]}], "commitments": [], "context_requests": []}
        elif stage == "P2":
            activity = data["normalized_activities"][0]["activity_id"]
            topic = data["registered_topics"][0]["topic_id"]
            value = {"status": "complete", "cards": [{"draft_key": "meeting", "scene_id": "work_communication", "source_activity_ids": [activity], "topic_ids": [topic], "related_topic_ids": [], "reader_need": "选出预算内的方案", "must_answer": ["如何比较两个方案"], "must_keep": ["三万元上限"], "useful_deliverable": "比较依据", "research_decision": {"decision": "search" if self.search else "no_search", "reason": "补充公开方法" if self.search else "问题只依赖内部预算", "task_keys": ["r1"] if self.search else []}}], "topic_dispositions": [{"topic_id": topic, "state": "assigned", "card_keys": ["meeting"], "reason": "工作会话"}], "research_tasks": [{"task_key": "r1", "question": "公开方案比较方法", "purpose": "建立比较依据", "public_context": "比较采购方案", "target_card_keys": ["meeting"], "source_requirements": "官方文档", "jurisdiction": None, "as_of": None, "version_constraint": None}] if self.search else [], "accepted_todo_keys": [], "context_requests": [], "budget_gaps": []}
        elif stage == "P3":
            value = {"task_key": data["task_key"], "status": "failed", "findings": [], "sources": [], "unresolved": ["没有适用资料"]}
        elif stage == "P4":
            topic = data["card_brief"]["topic_ids"][0]
            value = {"status": "needs_context" if self.partial else "complete", "card_id": data["card_id"], "markdown": "# 先用三万元预算筛选方案\n\n先比较两个方案的必要能力与总成本。预算只有三万元。", "topic_dispositions": [{"topic_id": topic, "state": "included", "body_locator": "预算只有三万元。", "reason": None}], "evidence_anchors": [{"body_locator": "预算只有三万元。", "segment_id": "s1", "quote": "预算只有三万元。"}], "new_topics": [], "new_commitments": [], "requests": [{"kind": "context", "question": "缺少报价", "purpose": "比较", "known_source_ranges": [], "public_context": None}] if self.partial else []}
        else:
            pytest.fail("Forbidden audit or revision stage dispatched")
        return json.dumps(value, ensure_ascii=False)


def make_pipeline(tmp_path, transport, *, allow=True, limit=20):
    from audio_memory.analysis.beta8_writing_pipeline import WritingPipeline
    from audio_memory.analysis.beta8_writing_store import WritingLimits
    return WritingPipeline(output_dir=tmp_path, transport=transport, limits=WritingLimits(allow_paid=allow, max_requests=limit), provider_id="deepseek", model_id="deepseek-v4-pro", search_provider_id="kimi", search_model_id="kimi-k2.6", credential_generation=1)


@pytest.mark.asyncio
async def test_first_writing_stops_with_raw_cards_and_pending_separate_scores(tmp_path):
    transport = ScriptedTransport(search=True)
    result = await make_pipeline(tmp_path, transport).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "draft_ready"
    assert [x[0] for x in transport.calls] == ["P1", "P2", "P3", "P4"]
    assert result["metrics"]["model_request_count"] == 5
    assert result["metrics"]["search_model_request_count"] == 2
    assert result["audit_performed"] is False
    assert result["published"] is False
    assert result["cards"][0]["markdown"].startswith("# 先用三万元")
    packet = json.loads((tmp_path / "scoring-packet.json").read_text())
    assert packet["cards"][0]["score"]["raw_total"] is None
    assert len(packet["cards"][0]["input"]["complete_transcript"]) == 3
    assert packet["cards"][0]["input"]["research_packets"][0]["status"] == "failed"
    before = (tmp_path / "完整初稿.md").read_bytes()
    cached = await make_pipeline(tmp_path, ScriptedTransport(invalid=True), allow=False).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert cached["status"] == "draft_ready"
    assert cached["new_request_count"] == 0
    assert (tmp_path / "完整初稿.md").read_bytes() == before


@pytest.mark.asyncio
async def test_default_execution_has_no_network_and_no_fabricated_score(tmp_path):
    transport = ScriptedTransport()
    result = await make_pipeline(tmp_path, transport, allow=False).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "stopped"
    assert transport.calls == []
    assert result["cards"] == []
    assert result["metrics"]["model_request_count"] == 0


@pytest.mark.asyncio
async def test_malformed_output_is_saved_without_repair_or_resend(tmp_path):
    transport = ScriptedTransport(invalid=True)
    result = await make_pipeline(tmp_path, transport).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "stopped"
    assert result["metrics"]["model_request_count"] == 1
    saved = list((tmp_path / "stages").glob("*.json"))
    assert json.loads(saved[0].read_text())["raw"] == "{broken"
    retry = ScriptedTransport()
    resumed = await make_pipeline(tmp_path, retry).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert resumed["status"] == "stopped"
    assert retry.calls == []


@pytest.mark.asyncio
async def test_incomplete_card_is_preserved_without_second_writing_call(tmp_path):
    transport = ScriptedTransport(partial=True)
    result = await make_pipeline(tmp_path, transport).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "draft_incomplete"
    assert result["cards"][0]["status"] == "needs_context"
    assert [x[0] for x in transport.calls].count("P4") == 1


@pytest.mark.asyncio
async def test_known_input_capacity_gap_stops_before_any_provider_invocation(tmp_path):
    from dataclasses import replace
    transport = ScriptedTransport()
    pipeline = make_pipeline(tmp_path, transport)
    pipeline.limits = replace(pipeline.limits, max_input_tokens=10)
    result = await pipeline.run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "stopped"
    assert transport.calls == []
    assert result["metrics"]["model_request_count"] == 0


@pytest.mark.asyncio
async def test_failed_resume_preserves_existing_complete_drafts_and_scoring_inputs(tmp_path):
    from dataclasses import replace
    first = await make_pipeline(tmp_path, ScriptedTransport()).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert first["status"] == "draft_ready"
    draft = (tmp_path / "完整初稿.md").read_bytes()
    score_input = (tmp_path / "scoring-packet.json").read_bytes()
    transport = ScriptedTransport()
    resumed = make_pipeline(tmp_path, transport, allow=False)
    resumed.limits = replace(resumed.limits, max_input_tokens=10)
    stopped = await resumed.run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert stopped["status"] == "stopped"
    assert transport.calls == []
    assert stopped["cards"] == first["cards"]
    assert (tmp_path / "完整初稿.md").read_bytes() == draft
    assert (tmp_path / "scoring-packet.json").read_bytes() == score_input


@pytest.mark.asyncio
async def test_empty_successful_research_is_not_reported_as_verified_success(tmp_path):
    from audio_memory.analysis.beta8_writing_store import WritingStore
    pipeline = make_pipeline(tmp_path, ScriptedTransport())
    pipeline.store = WritingStore(tmp_path, {"fixture": True})
    result = await pipeline._verify_research({"task_key": "r1", "status": "success", "findings": [], "sources": [], "unresolved": []})
    assert result["status"] == "failed"
    assert result["candidate_status"] == "success"
    assert result["unresolved"]


class FailingSearchTransport(ScriptedTransport):
    def __init__(self, failure):
        super().__init__(search=True)
        self.failure = failure

    async def complete(self, **kwargs):
        if kwargs["stage"] != "P3":
            return await super().complete(**kwargs)
        from audio_memory.analysis.errors import ProviderAnalysisError
        self.calls.append(("P3", {}))
        await kwargs["before_request"]({"messages": [{"role": "user", "content": kwargs["user"]}]})
        if self.failure in {"provider_error", "pause_batch"}:
            error = ProviderAnalysisError("Search unavailable", code="provider_unavailable", partial_response="upstream failed", pause_batch=self.failure == "pause_batch")
            error.http_status_code = 503
            raise error
        raw = {
            "malformed": "{broken",
            "invalid_protocol": '{"status":"success"}',
            "invalid_nested_field": '{"task_key":"r1","status":"success","findings":[null],"sources":[],"unresolved":[]}',
        }[self.failure]
        await kwargs["after_response"]({"usage": {"prompt_tokens": 30, "completion_tokens": 4}, "choices": [{"message": {"content": raw}, "finish_reason": "stop"}]})
        return raw


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider_error", "malformed", "invalid_protocol", "invalid_nested_field"])
async def test_search_failure_is_preserved_and_given_to_writer_without_retry(tmp_path, failure):
    transport = FailingSearchTransport(failure)
    result = await make_pipeline(tmp_path, transport).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "draft_ready"
    assert [stage for stage, _ in transport.calls] == ["P1", "P2", "P3", "P4"]
    assert result["metrics"]["model_request_count"] == 4
    assert result["metrics"]["search_model_request_count"] == 1
    packet = json.loads((tmp_path / "scoring-packet.json").read_text())
    research = packet["cards"][0]["input"]["research_packets"][0]
    assert research["status"] == "failed"
    assert research["unresolved"]
    assert packet["cards"][0]["input"]["verified_sources"] == []
    raw_stage = json.loads(next((tmp_path / "stages").glob("P3-*.json")).read_text())
    assert raw_stage["raw"] == {
        "provider_error": "upstream failed",
        "malformed": "{broken",
        "invalid_protocol": '{"status":"success"}',
        "invalid_nested_field": '{"task_key":"r1","status":"success","findings":[null],"sources":[],"unresolved":[]}',
    }[failure]
    first_draft = (tmp_path / "完整初稿.md").read_bytes()
    resumed_transport = ScriptedTransport(invalid=True)
    resumed = await make_pipeline(tmp_path, resumed_transport, allow=False).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert resumed["status"] == "draft_ready"
    assert resumed["new_request_count"] == 0
    assert resumed_transport.calls == []
    assert (tmp_path / "完整初稿.md").read_bytes() == first_draft


@pytest.mark.asyncio
async def test_search_budget_or_explicit_batch_pause_still_stops_writing(tmp_path):
    for failure, limit in [("provider_error", 2), ("pause_batch", 20)]:
        transport = FailingSearchTransport(failure)
        result = await make_pipeline(tmp_path / failure, transport, limit=limit).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
        assert result["status"] == "stopped"
        assert not any(stage == "P4" for stage, _ in transport.calls)


@pytest.mark.asyncio
async def test_explicit_p1_retry_preserves_original_and_cannot_retry_twice(tmp_path):
    first = ScriptedTransport(invalid=True)
    await make_pipeline(tmp_path, first).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    original_path = next((tmp_path / "stages").glob("*.json"))
    original = original_path.read_bytes()
    second = ScriptedTransport(invalid=True)
    pipeline = make_pipeline(tmp_path, second)
    pipeline.retry_invalid_p1_windows = {"window_001"}
    result = await pipeline.run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["metrics"]["model_request_count"] == 2
    assert len(second.calls) == 1
    assert original_path.read_bytes() == original
    assert len(list((tmp_path / "stages").glob("*-retry-1.json"))) == 1
    third = ScriptedTransport()
    pipeline = make_pipeline(tmp_path, third)
    pipeline.retry_invalid_p1_windows = {"window_001"}
    result = await pipeline.run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "stopped"
    assert third.calls == []


@pytest.mark.asyncio
async def test_successful_explicit_retry_is_reused_with_same_prompt_and_input(tmp_path):
    first = ScriptedTransport(invalid=True)
    await make_pipeline(tmp_path, first).run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    second = ScriptedTransport()
    pipeline = make_pipeline(tmp_path, second)
    pipeline.retry_invalid_p1_windows = {"window_001"}
    result = await pipeline.run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "draft_ready"
    assert second.calls[0] == first.calls[0]
    third = ScriptedTransport(invalid=True)
    pipeline = make_pipeline(tmp_path, third)
    pipeline.retry_invalid_p1_windows = {"window_001"}
    result = await pipeline.run(SEGMENTS, user_corrections=[], report_period="2026-09-09")
    assert result["status"] == "draft_ready"
    assert third.calls == []
