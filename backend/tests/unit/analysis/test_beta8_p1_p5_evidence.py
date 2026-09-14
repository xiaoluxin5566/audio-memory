from __future__ import annotations

from copy import deepcopy

import pytest

from audio_memory.analysis.beta8_p1_p5_evidence import P1P5EvidenceCatalog, EvidenceError


def rows():
    return [
        {"segment_id": f"s{i}", "source_file": "day", "text": text}
        for i, text in enumerate(["开始讨论预算。", "预算只有三万元。", "决定比较两个方案。", "下午确认结果。"])
    ]


def p1(window):
    return {
        "status": "complete", "window_id": window["window_id"],
        "activities": [{"local_key": "a", "start_segment_id": "s0", "end_segment_id": "s3", "activity_kind": "conversation", "participation": "live", "purpose": "work", "subject": "方案讨论", "continuation_note": None}],
        "topics": [{"local_key": "t", "understanding": "预算限制下比较两个方案", "open_questions": [], "evidence_anchor_ids": ["s1", "s2"]}],
        "attention_signals": [{"local_key": "sig", "topic_keys": ["t"], "type": "decision", "description": "决定比较两个方案", "evidence_anchor_ids": ["s2"]}],
        "commitments": [{"local_key": "c", "topic_keys": ["t"], "text": "下午确认结果", "actor": None, "acceptance_state": "explicit", "time_text": "下午", "completed": False, "evidence_anchor_ids": ["s3"]}],
        "context_requests": [],
    }


def plan():
    return {
        "status": "complete",
        "cards": [{
            "draft_key": "card-a", "scene_id": "work_communication", "core_question": "如何在三万元预算内比较方案？",
            "source_activity_ids": ["activity_001"],
            "topic_assignments": [{"topic_id": "topic_001", "role": "core"}],
            "reader_need": "形成可执行比较", "must_answer": ["比较依据是什么"],
            "must_cover": [{"description": "三万元限制", "activity_id": "activity_001", "evidence_anchor_ids": ["s1"]}],
            "useful_deliverable": "比较框架", "research_decision": {"decision": "no_search", "reason": "内部约束足够", "task_keys": []},
        }],
        "topic_dispositions": [{"topic_id": "topic_001", "state": "standalone", "card_keys": ["card-a"], "reason": "有独立问题和帮助价值"}],
        "research_tasks": [], "context_requests": [], "budget_gaps": [],
    }


def test_p1_topics_have_no_ranges_and_program_copies_anchor_text_from_activity():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    result = p1(window)
    original = deepcopy(result)
    registry = catalog.normalize([result], [window])
    topic = registry["registered_topics"][0]
    assert "ranges" not in topic and "source_ranges" not in topic
    assert topic["evidence_anchors"] == [
        {"segment_id": "s1", "quote": "预算只有三万元。"},
        {"segment_id": "s2", "quote": "决定比较两个方案。"},
    ]
    assert registry["attention_signals"][0]["type"] == "decision"
    assert registry["registered_topics"][0]["activity_ids"] == ["activity_001"]
    assert registry["attention_signals"][0]["activity_ids"] == ["activity_001"]
    assert registry["commitments"][0]["activity_ids"] == ["activity_001"]
    assert registry["commitments"][0]["completed"] is False
    assert result == original


def test_p1_program_derives_all_activity_ids_when_topic_anchors_cross_boundaries():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    result = p1(window)
    result["activities"][0]["end_segment_id"] = "s2"
    result["activities"].append({"local_key": "b", "start_segment_id": "s3", "end_segment_id": "s3", "activity_kind": "follow_up", "participation": "live", "purpose": "work", "subject": "下午确认", "continuation_note": None})
    result["topics"][0]["evidence_anchor_ids"] = ["s1", "s3"]
    result["commitments"] = []
    validated = catalog.validate_window(result, window)
    registry = catalog.normalize([validated], [window])
    assert registry["registered_topics"][0]["activity_ids"] == [
        "activity_001", "activity_002",
    ]


def test_p1_normalizes_only_an_unambiguous_missing_segment_id_separator():
    source = [
        {"segment_id": "seg_3_818", "source_file": "day", "text": "直播销售开始"},
        {"segment_id": "seg_3_819", "source_file": "day", "text": "直播销售结束"},
        {"segment_id": "seg_3_820", "source_file": "day", "text": "游戏开始"},
    ]
    catalog = P1P5EvidenceCatalog(source)
    window = catalog.windows(10_000)[0]
    result = {
        "status": "complete", "window_id": window["window_id"],
        "activities": [
            {"local_key": "a", "start_segment_id": "seg_3_818", "end_segment_id": "seg_3819", "activity_kind": "media", "participation": "watching", "purpose": "entertainment", "subject": "直播销售", "continuation_note": None},
            {"local_key": "b", "start_segment_id": "seg_3_820", "end_segment_id": "seg_3_820", "activity_kind": "media", "participation": "watching", "purpose": "entertainment", "subject": "游戏", "continuation_note": None},
        ],
        "topics": [
            {"local_key": "t1", "understanding": "直播销售", "open_questions": [], "evidence_anchor_ids": ["seg_3819"]},
            {"local_key": "t2", "understanding": "游戏开始", "open_questions": [], "evidence_anchor_ids": ["seg_3_820"]},
        ],
        "attention_signals": [], "commitments": [], "context_requests": [],
    }
    original = deepcopy(result)

    corrected = catalog.validate_window(result, window)

    assert corrected["activities"][0]["end_segment_id"] == "seg_3_819"
    assert corrected["topics"][0]["evidence_anchor_ids"] == ["seg_3_819"]
    assert result == original


def test_signal_and_commitment_activity_ids_are_derived_from_anchors_without_mutating_raw():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    result = p1(window)
    result["activities"] = [
        {**result["activities"][0], "end_segment_id": "s2"},
        {"local_key": "b", "start_segment_id": "s3", "end_segment_id": "s3", "activity_kind": "follow_up", "participation": "live", "purpose": "work", "subject": "下午确认", "continuation_note": None},
    ]
    result["commitments"][0]["evidence_anchor_ids"] = ["s3"]
    original = deepcopy(result)
    registry = catalog.normalize([result], [window])
    commitment = registry["commitments"][0]
    assert commitment["activity_ids"] == ["activity_002"]
    assert result == original


def test_p2_receives_each_complete_activity_transcript_and_absolute_dispositions():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    registry = catalog.normalize([p1(window)], [window])
    p2_input = catalog.planning_input(registry)
    assert [r["segment_id"] for r in p2_input["activity_transcripts"][0]["complete_transcript"]] == ["s0", "s1", "s2", "s3"]
    assert catalog.validate_plan(plan(), registry) == plan()
    bad = plan()
    bad["topic_dispositions"][0]["state"] = "important"
    with pytest.raises(EvidenceError, match="disposition"):
        catalog.validate_plan(bad, registry)


def test_p2_program_derives_card_sources_and_splits_cross_activity_must_cover():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    result = p1(window)
    result["activities"] = [
        {**result["activities"][0], "end_segment_id": "s2"},
        {"local_key": "b", "start_segment_id": "s3", "end_segment_id": "s3", "activity_kind": "follow_up", "participation": "live", "purpose": "work", "subject": "下午确认", "continuation_note": None},
    ]
    result["topics"][0]["evidence_anchor_ids"] = ["s1", "s3"]
    registry = catalog.normalize([result], [window])
    candidate = plan()
    candidate["cards"][0]["must_cover"] = [{
        "description": "预算与下午确认",
        "activity_id": "activity_001",
        "evidence_anchor_ids": ["s1", "s3"],
    }]

    validated = catalog.validate_plan(candidate, registry)

    assert validated["cards"][0]["source_activity_ids"] == [
        "activity_001", "activity_002",
    ]
    assert validated["cards"][0]["must_cover"] == [
        {"description": "预算与下午确认", "activity_id": "activity_001", "evidence_anchor_ids": ["s1"]},
        {"description": "预算与下午确认", "activity_id": "activity_002", "evidence_anchor_ids": ["s3"]},
    ]


def test_p2_program_materializes_a_missing_merged_topic_assignment():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    registry = catalog.normalize([p1(window)], [window])
    candidate = plan()
    candidate["cards"][0]["topic_assignments"] = []
    candidate["topic_dispositions"][0]["state"] = "merged"

    validated = catalog.validate_plan(candidate, registry)

    assert validated["cards"][0]["topic_assignments"] == [
        {"topic_id": "topic_001", "role": "context"},
    ]


def test_p4_packet_has_full_activity_and_writer_cannot_add_topics_or_searches():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    registry = catalog.normalize([p1(window)], [window])
    packet = catalog.assemble("card-1", plan()["cards"][0], registry, [])
    assert [r["segment_id"] for r in packet["complete_transcript"]] == ["s0", "s1", "s2", "s3"]
    card = {
        "status": "written", "card_id": "card-1", "title": "三万元预算先比较必要能力",
        "markdown": "# 三万元预算先比较必要能力\n\n预算只有三万元，因此先比较必要能力。",
        "evidence_refs": [{"body_locator": "预算只有三万元", "anchor_ids": ["s1"]}],
        "research_refs": [], "uncertainties": [],
    }
    assert catalog.validate_card(card, packet) == card
    card["new_topics"] = []
    with pytest.raises(EvidenceError, match="fields"):
        catalog.validate_card(card, packet)


def test_p4_keeps_descriptive_evidence_locator_when_anchor_is_in_activity():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    registry = catalog.normalize([p1(window)], [window])
    packet = catalog.assemble("card-1", plan()["cards"][0], registry, [])
    card = {
        "status": "written", "card_id": "card-1", "title": "三万元预算先比较必要能力",
        "markdown": "# 三万元预算先比较必要能力\n\n预算只有三万元，因此先比较必要能力。",
        "evidence_refs": [{"body_locator": "预算与必要能力", "anchor_ids": ["s1"]}],
        "research_refs": [], "uncertainties": [],
    }

    assert catalog.validate_card(card, packet) == card


def test_p4_structural_validation_leaves_missing_must_cover_judgment_to_p5():
    catalog = P1P5EvidenceCatalog(rows())
    window = catalog.windows(10_000)[0]
    registry = catalog.normalize([p1(window)], [window])
    packet = catalog.assemble("card-1", plan()["cards"][0], registry, [])
    card = {
        "status": "written", "card_id": "card-1", "title": "预算决策",
        "markdown": "只写了无关背景。",
        "evidence_refs": [{"body_locator": "无关背景", "anchor_ids": ["s2"]}],
        "research_refs": [], "uncertainties": [],
    }

    assert catalog.validate_card(card, packet) == card
