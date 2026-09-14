from __future__ import annotations

from copy import deepcopy

import pytest

from audio_memory.analysis.beta8_writing_evidence import EvidenceCatalog, EvidenceError


def segments(count: int = 6, *, source_file: str = "day.md") -> list[dict[str, object]]:
    return [
        {
            "segment_id": f"s{number:03d}",
            "source_file": source_file,
            "text": f"第 {number} 段原文，关键词-{number}。",
            "speaker": f"speaker-{number % 2}",
            "start_ms": number * 1_000,
            "custom_metadata": {"ordinal": number},
        }
        for number in range(1, count + 1)
    ]


def p1_result(
    window: dict[str, object],
    *,
    activity_key: str = "activity",
    topic_key: str = "topic",
    activity_start: str | None = None,
    activity_end: str | None = None,
    topic_start: str | None = None,
    topic_end: str | None = None,
    subject: str = "同名讨论",
    continuation_note: str | None = None,
) -> dict[str, object]:
    core = window["core_segments"]
    assert isinstance(core, list)
    start = activity_start or str(core[0]["segment_id"])
    end = activity_end or str(core[-1]["segment_id"])
    topic_first = topic_start or str(core[0]["segment_id"])
    topic_last = topic_end or str(core[-1]["segment_id"])
    return {
        "status": "complete",
        "window_id": window["window_id"],
        "activities": [{
            "local_key": activity_key,
            "start_segment_id": start,
            "end_segment_id": end,
            "activity_kind": "conversation",
            "participation": "live",
            "purpose": "work",
            "subject": subject,
            "continuation_note": continuation_note,
        }],
        "topics": [{
            "local_key": topic_key,
            "parent_activity_key": activity_key,
            "ranges": [{
                "source_file": "day.md",
                "start_segment_id": topic_first,
                "end_segment_id": topic_last,
            }],
            "provenance": "现场互动",
            "understanding": "讨论了可验证的交付约束。",
            "open_questions": [],
            "evidence_anchors": [{
                "segment_id": topic_first,
                "quote": f"关键词-{int(topic_first[1:])}",
            }],
        }],
        "commitments": [],
        "context_requests": [],
    }


def registry_for_plan() -> dict[str, object]:
    rows = segments(4)
    catalog = EvidenceCatalog(rows)
    windows = catalog.windows(10_000)
    result = p1_result(windows[0])
    result["commitments"] = [{
        "local_key": "todo",
        "text": "发送方案",
        "actor": "speaker-1",
        "acceptance_state": "accepted",
        "time_text": "明天",
        "evidence_segment_id": "s002",
    }]
    return catalog.normalize([result], windows)


def test_id_only_p1_anchor_copies_source_verbatim_and_preserves_raw_response():
    rows = segments(4)
    rows[0]["text"] = "招的人，还不能就是玩玩而已而已。"
    catalog = EvidenceCatalog(rows)
    windows = catalog.windows(10_000)
    result = p1_result(windows[0])
    result["topics"][0]["evidence_anchors"] = [{"segment_id": "s001"}]
    original = deepcopy(result)
    assert catalog.validate_window(result, windows[0]) == original
    registry = catalog.normalize([result], windows)
    assert registry["registered_topics"][0]["evidence_anchors"] == [
        {"segment_id": "s001", "quote": rows[0]["text"]},
    ]
    assert result == original


def test_id_only_p1_anchor_still_rejects_wrong_topic_and_legacy_misquote():
    rows = segments(4)
    catalog = EvidenceCatalog(rows)
    windows = catalog.windows(10_000)
    result = p1_result(windows[0], topic_end="s002")
    result["topics"][0]["evidence_anchors"] = [{"segment_id": "s003"}]
    with pytest.raises(EvidenceError, match="outside topic ranges"):
        catalog.validate_window(result, windows[0])
    result["topics"][0]["evidence_anchors"] = [{"segment_id": "s001", "quote": "原文没有的内容"}]
    with pytest.raises(EvidenceError, match="not exact"):
        catalog.validate_window(result, windows[0])


def valid_plan() -> dict[str, object]:
    return {
        "status": "complete",
        "cards": [{
            "draft_key": "draft-a",
            "scene_id": "work_communication",
            "source_activity_ids": ["activity_001"],
            "topic_ids": ["topic_001"],
            "related_topic_ids": [],
            "reader_need": "理解约束并推进",
            "must_answer": ["哪个约束会改变方案？"],
            "must_keep": ["已有决定"],
            "useful_deliverable": "给出依赖顺序",
            "research_decision": {
                "decision": "search",
                "reason": "公开版本信息会改变建议",
                "task_keys": ["research-a"],
            },
        }],
        "topic_dispositions": [{
            "topic_id": "topic_001",
            "state": "assigned",
            "card_keys": ["draft-a"],
            "reason": "这是本卡主问题",
        }],
        "research_tasks": [{
            "task_key": "research-a",
            "question": "当前公开版本支持什么？",
            "purpose": "判断方案是否可行",
            "public_context": "产品名称与公开版本",
            "target_card_keys": ["draft-a"],
            "source_requirements": "官方文档",
            "jurisdiction": None,
            "as_of": "2026-09-09",
            "version_constraint": "当前稳定版",
        }],
        "accepted_todo_keys": ["commitment_001"],
        "context_requests": [],
        "budget_gaps": [],
    }


def test_constructor_rejects_missing_fields_and_globally_duplicate_segment_ids() -> None:
    missing_text = segments(2)
    del missing_text[1]["text"]
    with pytest.raises(EvidenceError, match="text"):
        EvidenceCatalog(missing_text)

    duplicated = segments(2)
    duplicated[1]["segment_id"] = "s001"
    duplicated[1]["source_file"] = "other.md"
    with pytest.raises(EvidenceError, match="duplicate.*s001"):
        EvidenceCatalog(duplicated)


def test_windows_own_every_core_once_keep_metadata_and_do_not_truncate_oversize_segment() -> None:
    rows = segments(4)
    rows[1]["text"] = "超" * 30
    catalog = EvidenceCatalog(rows)

    windows = catalog.windows(core_budget_bytes=20, boundary_segments=1)

    assert [
        segment["segment_id"]
        for window in windows
        for segment in window["core_segments"]
    ] == ["s001", "s002", "s003", "s004"]
    oversize = next(
        window for window in windows if window["core_segments"][0]["segment_id"] == "s002"
    )
    assert len(oversize["core_segments"]) == 1
    assert oversize["core_segments"][0]["text"] == "超" * 30
    assert oversize["core_segments"][0]["custom_metadata"] == {"ordinal": 2}
    assert [row["segment_id"] for row in oversize["boundary_context"]["before"]] == [
        "s001"
    ]
    assert [row["segment_id"] for row in oversize["boundary_context"]["after"]] == [
        "s003"
    ]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("s999", "s002", "unknown"),
        ("s003", "s002", "reversed"),
    ],
)
def test_normalize_rejects_unknown_or_reversed_activity_ranges(
    start: str, end: str, message: str
) -> None:
    catalog = EvidenceCatalog(segments(3))
    windows = catalog.windows(10_000)
    result = p1_result(windows[0], activity_start=start, activity_end=end)

    with pytest.raises(EvidenceError, match=message):
        catalog.normalize([result], windows)


def test_normalize_rejects_cross_file_range_even_when_segment_ids_exist() -> None:
    rows = segments(2)
    rows.extend(segments(2, source_file="other.md"))
    rows[2]["segment_id"] = "o001"
    rows[3]["segment_id"] = "o002"
    catalog = EvidenceCatalog(rows)
    windows = catalog.windows(10_000)
    result = p1_result(
        windows[0], activity_start="s001", activity_end="o001"
    )
    second = p1_result(windows[1])
    second["topics"][0]["ranges"][0]["source_file"] = "other.md"

    with pytest.raises(EvidenceError, match="cross-file"):
        catalog.normalize([result, second], windows)


def test_normalize_assigns_stable_global_ids_and_links_only_confirmed_overlap() -> None:
    catalog = EvidenceCatalog(segments(6))
    windows = catalog.windows(core_budget_bytes=50, boundary_segments=1)
    assert len(windows) >= 2
    left, right = windows[0], windows[1]
    left_last = str(left["core_segments"][-1]["segment_id"])
    right_first = str(right["core_segments"][0]["segment_id"])
    left_result = p1_result(
        left,
        activity_key="left",
        topic_key="left-topic",
        activity_end=right_first,
        topic_end=left_last,
        continuation_note="活动继续到下一窗口",
    )
    right_result = p1_result(
        right,
        activity_key="right",
        topic_key="right-topic",
        activity_start=left_last,
        continuation_note="承接上一窗口",
    )
    remaining = [p1_result(window) for window in windows[2:]]

    registry = catalog.normalize([left_result, right_result, *remaining], windows)

    first = registry["normalized_activities"][0]
    assert first["activity_id"] == "activity_001"
    assert [part["local_key"] for part in first["window_parts"]] == ["left", "right"]
    assert first["source_ranges"][0]["start_segment_id"] == "s001"
    assert first["source_ranges"][0]["end_segment_id"] == str(
        right["core_segments"][-1]["segment_id"]
    )
    assert registry["registered_topics"][0]["topic_id"] == "topic_001"
    assert registry["registered_topics"][1]["topic_id"] == "topic_002"
    assert registry["boundary_evidence"][0]["state"] == "linked"


def test_normalize_keeps_reciprocal_boundary_overlap_ambiguous_when_activity_facts_conflict() -> None:
    catalog = EvidenceCatalog(segments(4))
    windows = catalog.windows(core_budget_bytes=35, boundary_segments=1)
    left_last = str(windows[0]["core_segments"][-1]["segment_id"])
    right_first = str(windows[1]["core_segments"][0]["segment_id"])
    left = p1_result(
        windows[0], activity_end=right_first, continuation_note="可能继续"
    )
    right = p1_result(
        windows[1], activity_start=left_last, continuation_note="可能承接"
    )
    right["activities"][0]["activity_kind"] = "media_playback"
    remaining = [p1_result(window) for window in windows[2:]]

    registry = catalog.normalize([left, right, *remaining], windows)

    assert len(registry["normalized_activities"]) == len(windows)
    assert registry["boundary_evidence"][0]["state"] == "ambiguous"


def test_normalize_never_merges_same_title_without_range_evidence() -> None:
    catalog = EvidenceCatalog(segments(4))
    windows = catalog.windows(core_budget_bytes=35, boundary_segments=1)
    results = [
        p1_result(
            window,
            activity_key=f"a-{number}",
            topic_key=f"t-{number}",
            subject="完全相同的标题",
            continuation_note="可能是同一段讨论",
        )
        for number, window in enumerate(windows)
    ]

    registry = catalog.normalize(results, windows)

    assert len(registry["normalized_activities"]) == len(windows)
    assert [row["activity_id"] for row in registry["normalized_activities"]] == [
        f"activity_{number:03d}" for number in range(1, len(windows) + 1)
    ]
    assert all(item["state"] == "ambiguous" for item in registry["boundary_evidence"])


def test_validate_window_can_reject_or_accept_one_window_before_the_rest_run() -> None:
    catalog = EvidenceCatalog(segments(4))
    windows = catalog.windows(core_budget_bytes=35, boundary_segments=1)

    assert catalog.validate_window(p1_result(windows[0]), windows[0])["status"] == "complete"

    malformed = p1_result(windows[0])
    malformed["topics"][0]["ranges"][0]["end_segment_id"] = "s999"
    with pytest.raises(EvidenceError, match="unknown"):
        catalog.validate_window(malformed, windows[0])


def test_normalize_rejects_duplicate_local_keys_and_invented_quotes() -> None:
    catalog = EvidenceCatalog(segments(3))
    windows = catalog.windows(10_000)
    duplicate = p1_result(windows[0], activity_key="same", topic_key="same")
    with pytest.raises(EvidenceError, match="duplicate local_key"):
        catalog.normalize([duplicate], windows)

    invented = p1_result(windows[0])
    invented["topics"][0]["evidence_anchors"][0]["quote"] = "原文没有这句话"
    with pytest.raises(EvidenceError, match="quote"):
        catalog.normalize([invented], windows)


def test_validate_plan_requires_complete_dispositions_and_symmetric_research_links() -> None:
    catalog = EvidenceCatalog(segments(4))
    registry = registry_for_plan()
    plan = valid_plan()

    assert catalog.validate_plan(plan, registry) == plan

    missing_disposition = deepcopy(plan)
    missing_disposition["topic_dispositions"] = []
    with pytest.raises(EvidenceError, match="disposition"):
        catalog.validate_plan(missing_disposition, registry)

    asymmetric = deepcopy(plan)
    asymmetric["cards"][0]["research_decision"] = {
        "decision": "no_search",
        "reason": "原文已经足够",
        "task_keys": [],
    }
    with pytest.raises(EvidenceError, match="symmetric"):
        catalog.validate_plan(asymmetric, registry)


def test_validate_plan_requires_explicit_search_or_no_search_per_card() -> None:
    catalog = EvidenceCatalog(segments(4))
    registry = registry_for_plan()
    for decision in (None, "maybe"):
        plan = valid_plan()
        if decision is None:
            del plan["cards"][0]["research_decision"]
        else:
            plan["cards"][0]["research_decision"]["decision"] = decision
        with pytest.raises(EvidenceError, match="research_decision"):
            catalog.validate_plan(plan, registry)


def test_validate_plan_complete_rejects_context_requests_and_needs_context_topics() -> None:
    catalog = EvidenceCatalog(segments(4))
    registry = registry_for_plan()
    plan = valid_plan()
    plan["context_requests"] = [{"question": "缺少边界"}]
    with pytest.raises(EvidenceError, match="complete plan"):
        catalog.validate_plan(plan, registry)

    plan = valid_plan()
    plan["cards"][0]["topic_ids"] = []
    plan["topic_dispositions"][0].update(
        {"state": "needs_context", "card_keys": [], "reason": "缺少主体身份"}
    )
    with pytest.raises(EvidenceError, match="complete plan"):
        catalog.validate_plan(plan, registry)


def test_validate_plan_does_not_merge_two_canonical_work_conversations_into_one_card() -> None:
    catalog = EvidenceCatalog(segments(4))
    registry = registry_for_plan()
    second_activity = deepcopy(registry["normalized_activities"][0])
    second_activity["activity_id"] = "activity_002"
    second_topic = deepcopy(registry["registered_topics"][0])
    second_topic.update({"topic_id": "topic_002", "parent_activity_id": "activity_002"})
    registry["normalized_activities"].append(second_activity)
    registry["registered_topics"].append(second_topic)
    plan = valid_plan()
    plan["cards"][0]["source_activity_ids"].append("activity_002")
    plan["cards"][0]["topic_ids"].append("topic_002")
    plan["topic_dispositions"].append({
        "topic_id": "topic_002",
        "state": "assigned",
        "card_keys": ["draft-a"],
        "reason": "另一场工作沟通",
    })

    with pytest.raises(EvidenceError, match="one primary card"):
        catalog.validate_plan(plan, registry)


def related_topic_plan() -> tuple[EvidenceCatalog, dict[str, object], dict[str, object]]:
    catalog = EvidenceCatalog(segments(4))
    registry = registry_for_plan()
    registry["normalized_activities"][0].update(
        {"activity_kind": "media_playback", "purpose": "content"}
    )
    second_topic = deepcopy(registry["registered_topics"][0])
    second_topic["topic_id"] = "topic_002"
    registry["registered_topics"].append(second_topic)
    plan = valid_plan()
    plan["cards"][0]["scene_id"] = "content_consumption"
    plan["cards"][0]["related_topic_ids"] = ["topic_002"]
    plan["cards"].append({
        **deepcopy(plan["cards"][0]),
        "draft_key": "draft-b",
        "topic_ids": ["topic_002"],
        "related_topic_ids": [],
        "research_decision": {
            "decision": "no_search",
            "reason": "原文足够",
            "task_keys": [],
        },
    })
    plan["topic_dispositions"].append({
        "topic_id": "topic_002",
        "state": "assigned",
        "card_keys": ["draft-b"],
        "reason": "在另一张卡主写",
    })
    return catalog, registry, plan


def test_related_topic_is_context_without_becoming_a_second_primary_assignment() -> None:
    catalog, registry, plan = related_topic_plan()

    assert catalog.validate_plan(plan, registry) == plan

    packet = catalog.assemble("card-001", plan["cards"][0], registry, [])
    result = complete_card_result()
    assert catalog.validate_card(result, packet) == result


def test_assemble_includes_every_segment_of_long_primary_and_related_parent_activities() -> None:
    rows = segments(72)
    catalog = EvidenceCatalog(rows)
    windows = catalog.windows(100_000)
    result = p1_result(windows[0], topic_end="s070")
    result["activities"] = [
        result["activities"][0],
        {
            "local_key": "related-activity",
            "start_segment_id": "s071",
            "end_segment_id": "s072",
            "activity_kind": "reflection",
            "participation": "solo",
            "purpose": "context",
            "subject": "关联背景",
            "continuation_note": None,
        },
    ]
    result["activities"][0]["end_segment_id"] = "s070"
    result["topics"].append({
        "local_key": "related-topic",
        "parent_activity_key": "related-activity",
        "ranges": [{
            "source_file": "day.md",
            "start_segment_id": "s071",
            "end_segment_id": "s072",
        }],
        "provenance": "用户自述",
        "understanding": "这是完整相关背景。",
        "open_questions": [],
        "evidence_anchors": [{"segment_id": "s071", "quote": "关键词-71"}],
    })
    registry = catalog.normalize([result], windows)
    brief = {
        "draft_key": "draft-long",
        "scene_id": "work_communication",
        "source_activity_ids": ["activity_001"],
        "topic_ids": ["topic_001"],
        "related_topic_ids": ["topic_002"],
        "reader_need": "完整理解长会话",
        "must_answer": [],
        "must_keep": [],
        "useful_deliverable": "完整说明",
        "research_decision": {
            "decision": "no_search",
            "reason": "原文已经足够",
            "task_keys": [],
        },
    }

    packet = catalog.assemble("card-001", brief, registry, [{"text": "主体修正"}])

    assert len(packet["complete_transcript"]) == 70
    assert packet["complete_transcript"][0]["custom_metadata"] == {"ordinal": 1}
    assert [row["segment_id"] for row in packet["related_context"][0]["transcript"]] == [
        "s071",
        "s072",
    ]
    assert packet["context_scope"]["complete_transcript_segment_ids"] == [
        f"s{number:03d}" for number in range(1, 71)
    ]
    assert packet["user_corrections"] == [{"text": "主体修正"}]


def complete_card_result() -> dict[str, object]:
    return {
        "status": "complete",
        "card_id": "card-001",
        "markdown": "# 交付判断\n\n约束已经明确，可以按顺序推进。\n\n## 依据\n\n关键词-1。",
        "topic_dispositions": [{
            "topic_id": "topic_001",
            "state": "included",
            "body_locator": "## 依据",
            "reason": None,
        }],
        "evidence_anchors": [{
            "body_locator": "## 依据",
            "segment_id": "s001",
            "quote": "关键词-1",
        }],
        "new_topics": [],
        "new_commitments": [],
        "requests": [],
    }


def short_packet() -> tuple[EvidenceCatalog, dict[str, object]]:
    catalog = EvidenceCatalog(segments(4))
    registry = registry_for_plan()
    brief = valid_plan()["cards"][0]
    packet = catalog.assemble("card-001", brief, registry, [])
    return catalog, packet


def test_validate_card_rejects_mismatched_card_id_and_invented_quote() -> None:
    catalog, packet = short_packet()
    wrong_card = complete_card_result()
    wrong_card["card_id"] = "card-999"
    with pytest.raises(EvidenceError, match="card_id"):
        catalog.validate_card(wrong_card, packet)

    invented = complete_card_result()
    invented["evidence_anchors"][0]["quote"] = "并不存在的连续原话"
    with pytest.raises(EvidenceError, match="quote"):
        catalog.validate_card(invented, packet)


def test_validate_card_checks_actual_body_locators_and_complete_status_consistency() -> None:
    catalog, packet = short_packet()
    valid = complete_card_result()
    assert catalog.validate_card(valid, packet) == valid

    missing_locator = deepcopy(valid)
    missing_locator["topic_dispositions"][0]["body_locator"] = "## 正文没有此节"
    with pytest.raises(EvidenceError, match="body_locator"):
        catalog.validate_card(missing_locator, packet)

    unresolved = deepcopy(valid)
    unresolved["requests"] = [{
        "kind": "context",
        "question": "谁作出了决定？",
        "purpose": "影响核心判断",
        "known_source_ranges": None,
        "public_context": None,
    }]
    with pytest.raises(EvidenceError, match="complete.*requests"):
        catalog.validate_card(unresolved, packet)


def test_validate_card_checks_new_topic_ranges_and_commitment_evidence() -> None:
    catalog, packet = short_packet()
    result = complete_card_result()
    result["new_topics"] = [{
        "local_key": "new-topic",
        "source_ranges": [{
            "source_file": "day.md",
            "start_segment_id": "s002",
            "end_segment_id": "s001",
        }],
        "description": "新发现的真实内容",
        "body_locator": "## 依据",
        "suggested_scene": "work_communication",
    }]
    with pytest.raises(EvidenceError, match="reversed"):
        catalog.validate_card(result, packet)

    result = complete_card_result()
    result["new_commitments"] = [{
        "text": "发送材料",
        "actor": "speaker-1",
        "acceptance_state": "accepted",
        "time_text": None,
        "evidence_segment_id": "s999",
    }]
    with pytest.raises(EvidenceError, match="supplied material"):
        catalog.validate_card(result, packet)


@pytest.mark.parametrize(
    "markdown",
    [
        "# 判断\n\n参考[虚构来源](https://invented.example/report)。\n\n## 依据\n\n关键词-1。",
        "# 判断\n\n参考 https://invented.example/report 。\n\n## 依据\n\n关键词-1。",
    ],
)
def test_validate_card_rejects_links_absent_from_verified_sources(markdown: str) -> None:
    catalog, packet = short_packet()
    result = complete_card_result()
    result["markdown"] = markdown

    with pytest.raises(EvidenceError, match="verified_sources"):
        catalog.validate_card(result, packet)


def test_p1_string_anchors_are_losslessly_normalized_without_mutating_raw():
    rows = segments(4)
    catalog = EvidenceCatalog(rows)
    window = catalog.windows(10_000)[0]
    raw = p1_result(window)
    raw["topics"][0]["evidence_anchors"] = ["s001", "s002"]
    original = deepcopy(raw)
    assert catalog.validate_window(raw, window) == original
    value = catalog.normalize([raw], [window])
    assert value["registered_topics"][0]["evidence_anchors"] == [
        {"segment_id": row["segment_id"], "quote": row["text"]} for row in rows[:2]
    ]
    assert raw == original
    raw["topics"][0]["evidence_anchors"] = ["unknown"]
    with pytest.raises(EvidenceError, match="outside topic ranges"):
        catalog.validate_window(raw, window)


@pytest.mark.parametrize("questions, expected", [(None, []), ("负责人未定", ["负责人未定"]), (["何时执行"], ["何时执行"])])
def test_p1_optional_question_shapes_preserve_every_question(questions, expected):
    catalog = EvidenceCatalog(segments(4))
    window = catalog.windows(10_000)[0]
    raw = p1_result(window)
    raw["topics"][0]["open_questions"] = questions
    before = deepcopy(raw)
    assert catalog.validate_window(raw, window) == before
    assert catalog.normalize([raw], [window])["registered_topics"][0]["open_questions"] == expected
    assert raw == before
