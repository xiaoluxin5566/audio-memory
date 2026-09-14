"""Offline contracts; these do not measure model topic recall."""
import json

import pytest

from audio_memory.analysis.beta8_event_index import normalize_event_index_draft
from audio_memory.prompts.beta8_composer import Beta8PromptComposer
from audio_memory.prompts.beta8_event_index_schema import Beta8EventIndexDraft


@pytest.mark.parametrize("obligation", [
    "登记成立必须同时满足两点",
    "同一议题在中断后再次出现",
    "不能只截取开头或最后一次出现",
    "先按原文顺序逐段回查每个复杂会话",
    "不能把范围内所有内容都视为已被该标题登记",
])
def test_beta8_composed_request_delivers_topic_navigation_obligations(obligation):
    # Removing a navigation obligation from the actual request must fail.
    # This tests instruction delivery, not whether a paid model obeys it.
    transcript = '<segment id="a">普通交流</segment>'
    request = Beta8PromptComposer().compose_event_index(transcript_markdown=transcript)
    assert obligation in request.instructions
    assert obligation not in request.user_data
    body = request.user_data.split(">", 1)[1].rsplit("</", 1)[0]
    assert json.loads(body)["transcript_markdown"] == transcript
    assert request.max_tokens == 32_000
    assert request.thinking_enabled is True


def test_beta8_recurrent_topics_reach_v1_without_splitting_session_or_filling_gaps():
    # A normalizer/composer that collapses disjoint topic ranges or filters
    # short topics would lose these hand-authored facts at the V1 boundary.
    rows = [
        {"file_id": "a", "segment_id": "z", "text": "演示时看不到边缘"},
        {"file_id": "a", "segment_id": "b", "text": "检索很慢，需要比较两种方案"},
        {"file_id": "a", "segment_id": "q", "text": "先安排下周的培训"},
        {"file_id": "b", "segment_id": "x", "text": "培训安排说完，回到检索"},
        {"file_id": "b", "segment_id": "c", "text": "新增缓存会增加存储，尚未决定"},
    ]
    def span(file, start, end):
        return {"source_file": file, "start_segment_id": start, "end_segment_id": end}
    payload = {
        "input_complete": True,
        "primary_sessions": [{
            "session_id": "meeting", "activity_kind": "conversation",
            "communication_purpose": "work",
            "participation_mode": "user_present_live_interaction",
            "start_boundary": "input_start", "start_boundary_evidence_segment_ids": ["z"],
            "end_boundary": "input_end", "end_boundary_evidence_segment_ids": ["c"],
            "subject": "同一次讨论", "description": "讨论演示、检索和培训。",
        }],
        "file_timelines": [
            {"source_file": "a", "blocks": [{"start_segment_id": "z", "end_segment_id": "q", "disposition": "session", "session_id": "meeting"}]},
            {"source_file": "b", "blocks": [{"start_segment_id": "x", "end_segment_id": "c", "disposition": "session", "session_id": "meeting"}]},
        ],
        "embedded_events": [
            {"event_id": "brief", "parent_session_id": "meeting", "event_kind": "discussion_topic",
             "expression_mode": "user_experience", "subject": "演示视野", "description": "讨论演示时的视野限制。",
             "evidence_ranges": [span("a", "z", "z")]},
            {"event_id": "recurring", "parent_session_id": "meeting", "event_kind": "discussion_topic",
             "expression_mode": "user_experience", "subject": "检索速度与存储取舍", "description": "比较方案并提出缓存成本，尚未决定。",
             "evidence_ranges": [span("a", "b", "b"), span("b", "x", "c")]},
            {"event_id": "training", "parent_session_id": "meeting", "event_kind": "discussion_topic",
             "expression_mode": "user_experience", "subject": "培训安排", "description": "安排下周培训。",
             "evidence_ranges": [span("a", "q", "q")]},
        ],
    }
    normalized = normalize_event_index_draft(Beta8EventIndexDraft.model_validate(payload), rows)
    transcript = "\n".join(f'<segment id="{r["segment_id"]}">{r["text"]}</segment>' for r in rows)
    request = Beta8PromptComposer().compose_all_scenes_v1(transcript_markdown=transcript, event_index=normalized)
    data = json.loads(request.user_data.split(">", 1)[1].rsplit("</", 1)[0])
    assert data["transcript_markdown"] == transcript
    index = data["event_index"]
    assert len(index["primary_sessions"]) == 1
    assert index["primary_sessions"][0]["session_id"] == "meeting"
    assert [(r["start_segment_id"], r["end_segment_id"]) for r in index["embedded_events"][1]["evidence_ranges"]] == [("b", "b"), ("x", "c")]
    assert [e["event_id"] for e in index["embedded_events"]] == ["brief", "recurring", "training"]
    assert [r["session_id"] for r in index["coverage_ranges"]] == ["meeting", "meeting"]
