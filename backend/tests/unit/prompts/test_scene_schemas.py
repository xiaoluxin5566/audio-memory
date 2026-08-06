from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from audio_memory.prompts.schemas import (
    CardShell,
    ConsumedItem,
    ContentCard,
    ContentDetail,
    ContentEvidence,
    ContentRecommendation,
    ContentSceneResult,
    EvidenceStatement,
    GrowthCard,
    GrowthCase,
    GrowthDetail,
    GrowthDirection,
    GrowthRecommendation,
    GrowthSceneResult,
    InterestSignal,
    LearningResource,
    InspirationCard,
    InspirationDetail,
    InspirationIdea,
    InspirationNextStep,
    InspirationSceneResult,
    MeetingCard,
    MeetingDetail,
    MeetingParticipant,
    MeetingSceneResult,
    ParentingCard,
    ParentingDetail,
    ParentingInteraction,
    ParentingIssue,
    ParentingRecommendation,
    ParentingSceneResult,
    StrictSceneResult,
    StrictTodoDraft,
    TodoSceneResult,
)


def todo(*, owner_type: str = "user", event_id: str = "event_001") -> StrictTodoDraft:
    return StrictTodoDraft(
        text="发送Q3预算表",
        action="发送Q3预算表",
        owner_type=owner_type,
        assignee_text="我",
        due_at="2026-08-06T15:00:00+08:00",
        due_text="明天下午三点前",
        intent_type="commitment",
        source_event_id=event_id,
        source_context="用户明确承诺发送预算表",
        evidence_segment_ids=["seg_001"],
        confidence=0.93,
    )


def meeting_card(event_id: str) -> MeetingCard:
    return MeetingCard(
        event_ids=[event_id],
        card=CardShell(title="一期范围确认", summary="团队确认第一期只支持上传已有音频。"),
        confidence=0.92,
        detail=MeetingDetail(
            event_id=event_id,
            topic="一期产品范围",
            start_ms=0,
            end_ms=12_000,
            background="围绕一期实现范围展开评审。",
            participants=[],
            core_conclusions=[],
            decisions=[],
            open_questions=[],
            meeting_todos=[],
            discussion_topics=[],
        ),
    )


def test_todo_scene_never_emits_cards_and_only_publishes_user_owned_todos() -> None:
    valid = TodoSceneResult(
        scene_id="todo",
        should_generate=True,
        generation_reason="event_001 中用户明确承诺，seg_001 提供直接证据。",
        cards=[],
        todos=[todo()],
        confidence=0.93,
    )

    assert valid.cards == []
    with pytest.raises(ValidationError):
        TodoSceneResult.model_validate(
            {
                **valid.model_dump(mode="json"),
                "cards": [
                    {
                        "event_ids": ["event_001"],
                        "card": {"title": "错误卡片", "summary": "待办不得生成卡片"},
                        "confidence": 0.9,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        TodoSceneResult(
            scene_id="todo",
            should_generate=True,
            generation_reason="event_001 的责任人是其他参与者。",
            cards=[],
            todos=[todo(owner_type="other")],
            confidence=0.9,
        )


def test_not_generated_scene_must_not_leak_cards_or_todos() -> None:
    with pytest.raises(ValidationError):
        TodoSceneResult(
            scene_id="todo",
            should_generate=False,
            generation_reason="责任归属不明。",
            cards=[],
            todos=[todo()],
            confidence=0.4,
        )


def test_meeting_allows_multiple_independent_cards_but_one_event_per_card() -> None:
    result = MeetingSceneResult(
        scene_id="meeting",
        should_generate=True,
        generation_reason="event_001 与 event_002 是两场独立会议。",
        cards=[meeting_card("event_001"), meeting_card("event_002")],
        todos=[],
        confidence=0.9,
    )

    assert [card.detail.event_id for card in result.cards] == ["event_001", "event_002"]
    invalid = meeting_card("event_001").model_dump(mode="json")
    invalid["event_ids"] = ["event_001", "event_002"]
    with pytest.raises(ValidationError):
        MeetingCard.model_validate(invalid)


def test_meeting_rejects_duplicate_event_cards_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MeetingSceneResult(
            scene_id="meeting",
            should_generate=True,
            generation_reason="event_001 被模型重复输出。",
            cards=[meeting_card("event_001"), meeting_card("event_001")],
            todos=[],
            confidence=0.9,
        )
    payload = meeting_card("event_001").model_dump(mode="json")
    payload["detail"]["invented"] = "not allowed"
    with pytest.raises(ValidationError):
        MeetingCard.model_validate(payload)


def parenting_interaction(event_id: str, sequence: int = 1) -> ParentingInteraction:
    finding_id = f"finding_parenting_{event_id}_{sequence:02d}"
    return ParentingInteraction(
        event_id=event_id,
        title="作业受挫后的沟通",
        start_ms=0,
        end_ms=8_000,
        background="孩子在解题失败后表达抗拒。",
        child_difficulties=[],
        emotional_signals=[],
        observed_parent_actions=[],
        possible_issues=[
            ParentingIssue(
                finding_id=finding_id,
                event_id=event_id,
                content="连续催促后孩子的抗拒加重",
                reasoning="催促后紧接着出现更强烈的拒绝回应",
                evidence_segment_ids=["seg_003"],
                confidence=0.60,
            )
        ],
        recommendations=[
            ParentingRecommendation(
                title="先确认困难点",
                why_it_helps="先区分不会做和不想做，避免继续升级冲突。",
                steps=["先问哪一步开始卡住", "让孩子复述已经理解的部分"],
                suggested_language="你是从哪一步开始觉得难？我们先只看这一小步。",
                profile_basis=None,
                basis_finding_ids=[finding_id],
            )
        ],
    )


def parenting_card(*event_ids: str) -> ParentingCard:
    return ParentingCard(
        event_ids=list(event_ids),
        card=CardShell(
            title="两次作业沟通中的不同困难",
            summary="分别关注解题受挫与开始任务时的抗拒，不合并推断原因。",
        ),
        confidence=0.84,
        detail=ParentingDetail(
            overall_observation="两段互动分别保留各自事实和建议。",
            interactions=[
                parenting_interaction(event_id, sequence)
                for sequence, event_id in enumerate(event_ids, start=1)
            ],
        ),
    )


def test_parenting_issue_below_point_six_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ParentingIssue(
            finding_id="finding_parenting_event_003_01",
            event_id="event_003",
            content="可能存在问题",
            reasoning="依据不足",
            evidence_segment_ids=["seg_003"],
            confidence=0.59,
        )


def test_parenting_aggregates_interactions_once_without_mixing_event_details() -> None:
    card = parenting_card("event_003", "event_004")
    result = ParentingSceneResult(
        scene_id="parenting",
        should_generate=True,
        generation_reason="event_003 与 event_004 是两段独立互动，分别有直接证据。",
        cards=[card],
        todos=[],
        confidence=0.84,
    )

    assert [item.event_id for item in result.cards[0].detail.interactions] == [
        "event_003",
        "event_004",
    ]
    with pytest.raises(ValidationError):
        ParentingSceneResult(
            scene_id="parenting",
            should_generate=True,
            generation_reason="错误地为同一上传生成两张家庭卡。",
            cards=[card, card],
            todos=[],
            confidence=0.84,
        )


def test_parenting_recommendation_must_reference_existing_finding_in_its_event() -> None:
    payload = parenting_interaction("event_003").model_dump(mode="json")
    payload["recommendations"][0]["basis_finding_ids"] = [
        "finding_parenting_event_999_01"
    ]
    with pytest.raises(ValidationError):
        ParentingInteraction.model_validate(payload)

    mismatched = parenting_interaction("event_003").model_dump(mode="json")
    mismatched["possible_issues"][0]["event_id"] = "event_004"
    with pytest.raises(ValidationError):
        ParentingInteraction.model_validate(mismatched)


def test_parenting_suggested_language_contains_two_or_three_sentences() -> None:
    payload = parenting_interaction("event_003").recommendations[0].model_dump(
        mode="json"
    )
    payload["suggested_language"] = "我们先看这一小步。"
    with pytest.raises(ValidationError):
        ParentingRecommendation.model_validate(payload)

    payload["suggested_language"] = "先告诉我哪里难。我们一起看。你先试一步。再全部重做。"
    with pytest.raises(ValidationError):
        ParentingRecommendation.model_validate(payload)


def consumed_item(
    event_id: str,
    *,
    source_title: str | None = None,
    title_source: str = "unknown",
    inferred_title_hint: str | None = "可能是某场端侧 AI 访谈",
) -> ConsumedItem:
    return ConsumedItem(
        event_id=event_id,
        content_type="video",
        platform=None,
        source_title=source_title,
        display_title="一段关于端侧 AI 产品体验的视频",
        title_source=title_source,
        inferred_title_hint=inferred_title_hint,
        start_ms=0,
        end_ms=10_000,
        introduction="视频讨论了端侧模型的响应体验。",
        evidence_segment_ids=["seg_010"],
        key_points=[
            ContentEvidence(
                content="端侧响应速度会直接影响交互连续性。",
                evidence_segment_ids=["seg_010"],
            )
        ],
        user_reactions=[],
    )


def test_content_title_source_keeps_explicit_unknown_and_inferred_hint_separate() -> None:
    unknown = consumed_item("event_010")
    explicit = consumed_item(
        "event_011",
        source_title="The Talk Show",
        title_source="explicit",
        inferred_title_hint=None,
    )

    assert unknown.source_title is None
    assert unknown.model_dump(mode="json")["inferred_title_hint"] == (
        "可能是某场端侧 AI 访谈"
    )
    assert explicit.source_title == "The Talk Show"
    with pytest.raises(ValidationError):
        consumed_item(
            "event_012",
            source_title="马斯克最新的访谈",
            title_source="explicit",
            inferred_title_hint=None,
        )
    with pytest.raises(ValidationError):
        consumed_item(
            "event_013",
            source_title="明确作品名",
            title_source="unknown",
        )


def test_content_item_round_trips_with_diagnostic_hint() -> None:
    item = consumed_item("event_010")

    restored = ConsumedItem.model_validate(item.model_dump(mode="json"))

    assert restored.inferred_title_hint == "可能是某场端侧 AI 访谈"


def test_content_frontend_dump_removes_inferred_title_hint_only_at_boundary() -> None:
    result = ContentSceneResult(
        scene_id="content",
        should_generate=True,
        generation_reason="event_010 中有可回顾的内容。",
        cards=[
            ContentCard(
                event_ids=["event_010"],
                card=CardShell(title="端侧体验视频", summary="记录可靠内容事实。"),
                confidence=0.8,
                detail=ContentDetail(
                    consumed_items=[consumed_item("event_010")],
                    cross_event_insights=[],
                    recommendations=[
                        ContentRecommendation(
                            title=None,
                            content_type="podcast",
                            creator=None,
                            introduction="通过主题搜索找可靠来源。",
                            recommendation_reason="与 event_010 直接相关。",
                            related_event_ids=["event_010"],
                            existence_confidence=0.50,
                            search_query="端侧 AI 产品体验 播客",
                        )
                    ],
                    internal_interest_signals=[],
                ),
            )
        ],
        todos=[],
        confidence=0.8,
    )

    assert "inferred_title_hint" in result.model_dump(mode="json")["cards"][0][
        "detail"
    ]["consumed_items"][0]
    frontend = result.model_dump_for_frontend()
    assert "inferred_title_hint" not in frontend["cards"][0]["detail"][
        "consumed_items"
    ][0]


def test_interest_modes_enforce_event_cardinality_and_reject_lightweight_reaction() -> None:
    with pytest.raises(ValidationError):
        InterestSignal(
            dimension="product_interest",
            value="端侧 AI",
            evidence_mode="multi_event_pattern",
            supporting_event_ids=["event_010"],
            confidence=0.8,
        )
    with pytest.raises(ValidationError):
        InterestSignal(
            dimension="product_interest",
            value="不错",
            evidence_mode="explicit_single_event",
            supporting_event_ids=["event_010"],
            confidence=0.8,
        )


def test_uncertain_work_recommendation_degrades_to_search_query() -> None:
    with pytest.raises(ValidationError):
        ContentRecommendation(
            title="某个可能存在的播客",
            content_type="podcast",
            creator="未知创作者",
            introduction="可能相关",
            recommendation_reason="与 event_010 讨论的端侧体验相关。",
            related_event_ids=["event_010"],
            existence_confidence=0.89,
            search_query="端侧 AI 产品体验 播客",
        )


def test_low_confidence_recommendation_rejects_specific_title_without_creator() -> None:
    with pytest.raises(ValidationError):
        ContentRecommendation(
            title="深夜端侧 AI 播客",
            content_type="podcast",
            creator=None,
            introduction="一档可能存在的具体播客。",
            recommendation_reason="与 event_010 的主题相关。",
            related_event_ids=["event_010"],
            existence_confidence=0.10,
            search_query="端侧 AI 产品体验 播客",
        )


def test_specific_work_recommendation_starts_at_point_nine() -> None:
    recommendation = ContentRecommendation(
        title="The Talk Show",
        content_type="podcast",
        creator="John Gruber",
        introduction="一档已确认存在的具体播客。",
        recommendation_reason="与 event_010 的主题直接相关。",
        related_event_ids=["event_010"],
        existence_confidence=0.90,
        search_query="The Talk Show John Gruber",
    )

    assert recommendation.title == "The Talk Show"


def test_search_only_recommendation_has_no_specific_title_or_creator() -> None:
    search_only = ContentRecommendation(
        title=None,
        content_type="podcast",
        creator=None,
        introduction="通过主题搜索寻找可靠来源。",
        recommendation_reason="与 event_010 讨论的端侧体验直接相关。",
        related_event_ids=["event_010"],
        existence_confidence=0.89,
        search_query="端侧 AI 产品体验 播客",
    )
    assert search_only.title is None
    assert search_only.search_query == "端侧 AI 产品体验 播客"


def test_low_confidence_learning_resource_rejects_specific_title() -> None:
    with pytest.raises(ValidationError):
        LearningResource(
            title="刻意练习",
            creator=None,
            resource_type="book",
            reason="与沟通复盘相关。",
            existence_confidence=0.89,
            search_query="沟通复盘 刻意练习 书",
        )


def test_search_only_learning_resource_has_no_specific_title_or_creator() -> None:
    resource = LearningResource(
        title=None,
        creator=None,
        resource_type="book",
        reason="通过通用主题搜索找可靠资源。",
        existence_confidence=0.89,
        search_query="沟通复盘 反馈技巧 书",
    )

    assert resource.title is None


def test_specific_learning_resource_starts_at_point_nine() -> None:
    resource = LearningResource(
        title="Crucial Conversations",
        creator="Joseph Grenny",
        resource_type="book",
        reason="该作品已确认存在且与沟通复盘相关。",
        existence_confidence=0.90,
        search_query="Crucial Conversations Joseph Grenny",
    )

    assert resource.title == "Crucial Conversations"


def test_content_aggregates_multiple_events_once_and_keeps_items_separate() -> None:
    card = ContentCard(
        event_ids=["event_010", "event_011"],
        card=CardShell(
            title="端侧产品视频与晚间发布会",
            summary="中午观看产品视频，晚间观看发布会，分别保留内容事实。",
        ),
        confidence=0.87,
        detail=ContentDetail(
            consumed_items=[consumed_item("event_010"), consumed_item("event_011")],
            cross_event_insights=[],
            recommendations=[],
            internal_interest_signals=[],
        ),
    )
    result = ContentSceneResult(
        scene_id="content",
        should_generate=True,
        generation_reason="event_010 与 event_011 是两个独立内容消费事件。",
        cards=[card],
        todos=[],
        confidence=0.87,
    )

    assert len(result.cards[0].detail.consumed_items) == 2
    with pytest.raises(ValidationError):
        ContentSceneResult(
            scene_id="content",
            should_generate=True,
            generation_reason="错误地输出两张内容卡。",
            cards=[card, card],
            todos=[],
            confidence=0.87,
        )


def growth_direction(
    *, confidence: float = 0.80, counterparty_response: str | None = "方案被要求重做"
) -> GrowthDirection:
    case_id = "case_growth_communication_event_006_01"
    return GrowthDirection(
        direction_id="communication",
        title="先明确约束再汇报方案",
        importance="减少高影响评审中的返工。",
        pattern_summary="本次场景中的观察，不足以判断为长期模式。",
        supporting_event_ids=["event_006"],
        cases=[
            GrowthCase(
                case_id=case_id,
                event_id="event_006",
                title="方案范围未先对齐",
                scene="产品评审",
                observed_behavior="用户直接展开方案细节，没有先确认范围约束。",
                counterparty_response=counterparty_response,
                problem="方案因范围不符被要求重做。",
                reasoning="评审方明确指出范围错误并要求重做。",
                evidence_segment_ids=["seg_006"],
                confidence=confidence,
            )
        ],
        recommendation=GrowthRecommendation(
            goal="下次汇报前先确认范围约束",
            method="用一句话复述目标和不做项，再进入方案。",
            steps=["复述目标", "确认不做项", "再展开方案"],
            suggested_language="我先确认一下，这次只解决上传音频，不涉及实时录制，对吗？",
            practice_task="本周为下一场评审写一条范围确认开场白。",
            success_signal="评审方在方案展开前明确确认范围。",
            profile_basis=None,
            basis_case_ids=[case_id],
        ),
        resources=[],
    )


def growth_card(direction: GrowthDirection | None = None) -> GrowthCard:
    selected = direction or growth_direction()
    return GrowthCard(
        event_ids=["event_006"],
        card=CardShell(
            title="汇报前先确认范围约束",
            summary="本次评审因范围未对齐产生返工，下次先复述目标与不做项。",
        ),
        confidence=0.84,
        detail=GrowthDetail(
            overall_assessment="仅依据本次高影响评审提出单事件改进，不判断长期模式。",
            directions=[selected],
            strengths_to_keep=[],
        ),
    )


def test_single_event_growth_requires_point_eight_and_observable_negative_result() -> None:
    with pytest.raises(ValidationError):
        growth_direction(confidence=0.79)
    with pytest.raises(ValidationError):
        growth_direction(counterparty_response=None)


def test_single_event_growth_requires_exception_reason_and_limited_pattern_claim() -> None:
    valid = GrowthSceneResult(
        scene_id="growth",
        should_generate=True,
        generation_reason="单事件例外：event_006 是高影响评审，seg_006 显示方案被要求重做。",
        cards=[growth_card()],
        todos=[],
        confidence=0.84,
    )
    assert valid.cards[0].detail.directions[0].cases[0].confidence == 0.80

    with pytest.raises(ValidationError):
        GrowthSceneResult(
            scene_id="growth",
            should_generate=True,
            generation_reason="event_006 有一条改进建议。",
            cards=[growth_card()],
            todos=[],
            confidence=0.84,
        )
    direction_payload = growth_direction().model_dump(mode="json")
    direction_payload["pattern_summary"] = "用户经常忽略范围约束。"
    with pytest.raises(ValidationError):
        GrowthDirection.model_validate(direction_payload)


def test_growth_recommendation_must_reference_case_in_same_direction() -> None:
    payload = growth_direction().model_dump(mode="json")
    payload["recommendation"]["basis_case_ids"] = [
        "case_growth_communication_event_999_01"
    ]
    with pytest.raises(ValidationError):
        GrowthDirection.model_validate(payload)


def test_growth_supporting_events_must_all_have_a_case() -> None:
    payload = growth_direction().model_dump(mode="json")
    payload["supporting_event_ids"] = ["event_006", "event_007"]

    with pytest.raises(ValidationError, match="case events|supporting events"):
        GrowthDirection.model_validate(payload)


def test_duplicate_growth_case_event_cannot_bypass_single_event_threshold() -> None:
    payload = growth_direction().model_dump(mode="json")
    first_case = payload["cases"][0]
    first_case["confidence"] = 0.79
    second_case = {**first_case, "case_id": "case_growth_communication_event_006_02"}
    payload["cases"] = [first_case, second_case]
    payload["supporting_event_ids"] = ["event_006", "event_007"]
    payload["recommendation"]["basis_case_ids"] = [
        first_case["case_id"],
        second_case["case_id"],
    ]

    with pytest.raises(ValidationError):
        GrowthDirection.model_validate(payload)


def test_multi_event_growth_requires_a_valid_case_for_each_event() -> None:
    payload = growth_direction().model_dump(mode="json")
    payload["cases"][0]["confidence"] = 0.50
    second_case = {
        **payload["cases"][0],
        "case_id": "case_growth_communication_event_007_02",
        "event_id": "event_007",
        "evidence_segment_ids": ["seg_007"],
    }
    payload["cases"].append(second_case)
    payload["supporting_event_ids"] = ["event_006", "event_007"]
    payload["recommendation"]["basis_case_ids"] = [
        "case_growth_communication_event_006_01",
        "case_growth_communication_event_007_02",
    ]

    result = GrowthDirection.model_validate(payload)

    assert {case.event_id for case in result.cases} == {"event_006", "event_007"}


def test_growth_scene_has_at_most_one_aggregate_card() -> None:
    card = growth_card()
    with pytest.raises(ValidationError):
        GrowthSceneResult(
            scene_id="growth",
            should_generate=True,
            generation_reason="单事件例外：event_006 被错误拆成两张成长卡。",
            cards=[card, card],
            todos=[],
            confidence=0.84,
        )


def inspiration_idea(event_id: str) -> InspirationIdea:
    return InspirationIdea(
        event_id=event_id,
        title="把回忆入口放在用户已有习惯里",
        start_ms=0,
        end_ms=6_000,
        background="讨论用户很少主动整理录音。",
        conversation_summary="用户将低回访率与入口脱离日常习惯联系起来。",
        core_idea="音频回忆入口应嵌入用户已经发生的工作流，而非要求额外整理。",
        why_valuable="这给产品入口选择提供了可验证的判断。",
        novelty_basis="不是复述外部观点，而是把回访问题与已有行为路径建立连接。",
        evidence_segment_ids=["seg_020"],
        confidence=0.82,
        next_steps=[
            InspirationNextStep(
                direction="验证入口假设",
                action="可以进一步访谈用户最常回看的工作记录入口。",
            )
        ],
    )


def test_lightweight_evaluation_is_not_an_inspiration() -> None:
    payload = inspiration_idea("event_020").model_dump(mode="json")
    payload["core_idea"] = "这个菜不错"
    payload["novelty_basis"] = "不错"
    with pytest.raises(ValidationError):
        InspirationIdea.model_validate(payload)


def test_inspiration_next_step_stays_exploratory_not_obligatory() -> None:
    with pytest.raises(ValidationError):
        InspirationNextStep(
            direction="继续研究",
            action="应该在周五前完成三次用户访谈。",
        )
    with pytest.raises(ValidationError):
        InspirationNextStep(
            direction="继续研究",
            action="可以在周五前完成三次用户访谈。",
        )


def test_inspiration_aggregates_ideas_once_and_keeps_event_groups() -> None:
    card = InspirationCard(
        event_ids=["event_020", "event_021"],
        card=CardShell(
            title="回忆入口假设与信息关联方式",
            summary="保留两个独立想法，并分别给出验证方向。",
        ),
        confidence=0.82,
        detail=InspirationDetail(
            overall_value="两个想法分别关联产品入口与信息整理。",
            ideas=[inspiration_idea("event_020"), inspiration_idea("event_021")],
            connections=[],
        ),
    )
    result = InspirationSceneResult(
        scene_id="inspiration",
        should_generate=True,
        generation_reason="event_020 与 event_021 分别包含可验证的新判断。",
        cards=[card],
        todos=[],
        confidence=0.82,
    )

    assert [idea.event_id for idea in result.cards[0].detail.ideas] == [
        "event_020",
        "event_021",
    ]
    with pytest.raises(ValidationError):
        InspirationSceneResult(
            scene_id="inspiration",
            should_generate=True,
            generation_reason="错误地生成两张灵感卡。",
            cards=[card, card],
            todos=[],
            confidence=0.82,
        )


def test_inspiration_rejects_uninformative_card_title() -> None:
    payload = InspirationCard(
        event_ids=["event_020"],
        card=CardShell(title="具体判断", summary="一条有证据的新判断。"),
        confidence=0.82,
        detail=InspirationDetail(
            overall_value="可继续验证。",
            ideas=[inspiration_idea("event_020")],
            connections=[],
        ),
    ).model_dump(mode="json")
    payload["card"]["title"] = "今日灵感"
    with pytest.raises(ValidationError):
        InspirationCard.model_validate(payload)


def frontend_todo_result() -> TodoSceneResult:
    return TodoSceneResult(
        scene_id="todo",
        should_generate=True,
        generation_reason="event_001 中用户明确承诺，seg_001 提供直接证据。",
        cards=[],
        todos=[todo()],
        confidence=0.93,
    )


def frontend_meeting_result() -> MeetingSceneResult:
    card = meeting_card("event_001")
    detail = card.detail.model_copy(
        update={
            "participants": [
                MeetingParticipant(
                    speaker_id="speaker_A",
                    display_name="用户",
                    role="汇报人",
                    evidence_segment_ids=["seg_001"],
                )
            ],
            "core_conclusions": [
                EvidenceStatement(
                    content="第一期只支持上传已有音频。",
                    evidence_segment_ids=["seg_001"],
                )
            ],
            "meeting_todos": [todo()],
        }
    )
    return MeetingSceneResult(
        scene_id="meeting",
        should_generate=True,
        generation_reason="event_001 形成明确结论。",
        cards=[card.model_copy(update={"detail": detail})],
        todos=[],
        confidence=0.92,
    )


def frontend_parenting_result() -> ParentingSceneResult:
    return ParentingSceneResult(
        scene_id="parenting",
        should_generate=True,
        generation_reason="event_003 中存在可复盘互动。",
        cards=[parenting_card("event_003")],
        todos=[],
        confidence=0.84,
    )


def frontend_content_result() -> ContentSceneResult:
    return ContentSceneResult(
        scene_id="content",
        should_generate=True,
        generation_reason="event_010 中有可回顾内容和可靠兴趣信号。",
        cards=[
            ContentCard(
                event_ids=["event_010"],
                card=CardShell(title="端侧体验视频", summary="记录可靠内容事实。"),
                confidence=0.8,
                detail=ContentDetail(
                    consumed_items=[consumed_item("event_010")],
                    cross_event_insights=[],
                    recommendations=[
                        ContentRecommendation(
                            title=None,
                            content_type="podcast",
                            creator=None,
                            introduction="通过主题搜索找可靠来源。",
                            recommendation_reason="与 event_010 直接相关。",
                            related_event_ids=["event_010"],
                            existence_confidence=0.50,
                            search_query="端侧 AI 产品体验 播客",
                        )
                    ],
                    internal_interest_signals=[
                        InterestSignal(
                            dimension="product_interest",
                            value="端侧 AI 产品体验",
                            evidence_mode="explicit_single_event",
                            supporting_event_ids=["event_010"],
                            confidence=0.8,
                        )
                    ],
                ),
            )
        ],
        todos=[],
        confidence=0.8,
    )


def frontend_growth_result() -> GrowthSceneResult:
    card = growth_card()
    direction = card.detail.directions[0].model_copy(
        update={
            "resources": [
                LearningResource(
                    title=None,
                    creator=None,
                    resource_type="book",
                    reason="通过主题搜索找可靠资源。",
                    existence_confidence=0.50,
                    search_query="沟通复盘 反馈技巧 书",
                )
            ]
        }
    )
    detail = card.detail.model_copy(update={"directions": [direction]})
    return GrowthSceneResult(
        scene_id="growth",
        should_generate=True,
        generation_reason="单事件例外：event_006 是高影响评审并出现明确返工。",
        cards=[card.model_copy(update={"detail": detail})],
        todos=[],
        confidence=0.84,
    )


def frontend_inspiration_result() -> InspirationSceneResult:
    return InspirationSceneResult(
        scene_id="inspiration",
        should_generate=True,
        generation_reason="event_020 中形成可验证的新判断。",
        cards=[
            InspirationCard(
                event_ids=["event_020"],
                card=CardShell(
                    title="入口应贴近已有习惯",
                    summary="可以继续验证入口假设。",
                ),
                confidence=0.82,
                detail=InspirationDetail(
                    overall_value="形成可验证的新连接。",
                    ideas=[inspiration_idea("event_020")],
                    connections=[],
                ),
            )
        ],
        todos=[],
        confidence=0.82,
    )


@pytest.mark.parametrize(
    "result_factory",
    [
        frontend_todo_result,
        frontend_meeting_result,
        frontend_parenting_result,
        frontend_content_result,
        frontend_growth_result,
        frontend_inspiration_result,
    ],
)
def test_generated_scene_confidence_starts_at_point_three(result_factory: type) -> None:
    payload = result_factory().model_dump(mode="json")
    payload["confidence"] = 0.29
    with pytest.raises(ValidationError):
        type(result_factory()).model_validate(payload)

    payload["confidence"] = 0.30
    assert type(result_factory()).model_validate(payload).confidence == 0.30


@pytest.mark.parametrize(
    "result_factory",
    [
        frontend_meeting_result,
        frontend_parenting_result,
        frontend_content_result,
        frontend_growth_result,
        frontend_inspiration_result,
    ],
)
def test_visible_card_confidence_starts_at_point_three(result_factory: type) -> None:
    payload = result_factory().model_dump(mode="json")
    payload["cards"][0]["confidence"] = 0.29
    with pytest.raises(ValidationError):
        type(result_factory()).model_validate(payload)

    payload["cards"][0]["confidence"] = 0.30
    assert type(result_factory()).model_validate(payload).cards[0].confidence == 0.30


def test_user_responsibility_confidence_starts_at_point_five() -> None:
    payload = todo().model_dump(mode="json")
    payload["confidence"] = 0.49
    with pytest.raises(ValidationError):
        StrictTodoDraft.model_validate(payload)

    payload["confidence"] = 0.50
    assert StrictTodoDraft.model_validate(payload).confidence == 0.50


def test_profile_signal_confidence_starts_at_point_five() -> None:
    payload = {
        "dimension": "product_interest",
        "value": "端侧 AI 产品体验",
        "evidence_mode": "explicit_single_event",
        "supporting_event_ids": ["event_010"],
        "confidence": 0.49,
    }
    with pytest.raises(ValidationError):
        InterestSignal.model_validate(payload)

    payload["confidence"] = 0.50
    assert InterestSignal.model_validate(payload).confidence == 0.50


def test_multi_event_personal_evaluation_confidence_starts_at_point_five() -> None:
    payload = growth_direction().cases[0].model_dump(mode="json")
    payload["confidence"] = 0.49
    with pytest.raises(ValidationError):
        GrowthCase.model_validate(payload)

    payload["confidence"] = 0.50
    assert GrowthCase.model_validate(payload).confidence == 0.50


def nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key
            for child in value.values()
            for nested_key in nested_keys(child)
        }
    if isinstance(value, list):
        return {
            nested_key
            for child in value
            for nested_key in nested_keys(child)
        }
    return set()


def test_frontend_dump_keeps_nested_visible_collections() -> None:
    meeting_payload = frontend_meeting_result().model_dump_for_frontend()
    content_payload = frontend_content_result().model_dump_for_frontend()
    growth_payload = frontend_growth_result().model_dump_for_frontend()

    assert "发送Q3预算表" in str(meeting_payload["cards"][0]["detail"]["meeting_todos"])
    assert "端侧 AI 产品体验 播客" in str(
        content_payload["cards"][0]["detail"]["recommendations"]
    )
    assert "沟通复盘 反馈技巧 书" in str(
        growth_payload["cards"][0]["detail"]["directions"][0]["resources"]
    )


@pytest.mark.parametrize(
    ("scene_id", "result_factory", "visible_value"),
    [
        ("todo", frontend_todo_result, "发送Q3预算表"),
        ("meeting", frontend_meeting_result, "一期产品范围"),
        ("parenting", frontend_parenting_result, "连续催促后孩子的抗拒加重"),
        ("content", frontend_content_result, "一段关于端侧 AI 产品体验的视频"),
        ("growth", frontend_growth_result, "用户直接展开方案细节"),
        ("inspiration", frontend_inspiration_result, "音频回忆入口应嵌入"),
    ],
)
def test_frontend_dump_uses_six_scene_recursive_allowlists(
    scene_id: str,
    result_factory: type,
    visible_value: str,
) -> None:
    payload = result_factory().model_dump_for_frontend()

    assert set(payload) == {"scene_id", "should_generate", "cards", "todos"}
    assert payload["scene_id"] == scene_id
    assert visible_value in str(payload)
    assert nested_keys(payload).isdisjoint(
        {
            "generation_reason",
            "internal_interest_signals",
            "inferred_title_hint",
            "confidence",
            "event_id",
            "event_ids",
            "source_event_id",
            "evidence_segment_ids",
            "finding_id",
            "case_id",
            "direction_id",
            "basis_finding_ids",
            "basis_case_ids",
            "supporting_event_ids",
            "related_event_ids",
            "speaker_id",
            "profile_basis",
            "existence_confidence",
        }
    )


@pytest.mark.parametrize(
    ("scene_id", "expected_type"),
    [
        ("todo", TodoSceneResult),
        ("meeting", MeetingSceneResult),
        ("parenting", ParentingSceneResult),
        ("content", ContentSceneResult),
        ("growth", GrowthSceneResult),
        ("inspiration", InspirationSceneResult),
    ],
)
def test_scene_result_union_dispatches_by_scene_id(scene_id: str, expected_type: type) -> None:
    adapter = TypeAdapter(StrictSceneResult)

    result = adapter.validate_python(
        {
            "scene_id": scene_id,
            "should_generate": False,
            "generation_reason": "当前场景证据不足。",
            "cards": [],
            "todos": [],
            "confidence": 0.4,
        }
    )

    assert isinstance(result, expected_type)
    schema = adapter.json_schema()
    assert schema["discriminator"]["propertyName"] == "scene_id"
    assert len(schema["oneOf"]) == 6


@pytest.mark.parametrize(
    ("result_type", "scene_id"),
    [
        (TodoSceneResult, "todo"),
        (MeetingSceneResult, "meeting"),
        (ParentingSceneResult, "parenting"),
        (ContentSceneResult, "content"),
        (GrowthSceneResult, "growth"),
        (InspirationSceneResult, "inspiration"),
    ],
)
def test_generated_scene_requires_its_publishable_payload(
    result_type: type, scene_id: str
) -> None:
    with pytest.raises(ValidationError):
        result_type(
            scene_id=scene_id,
            should_generate=True,
            generation_reason="错误地声明生成但没有任何可发布内容。",
            cards=[],
            todos=[],
            confidence=0.8,
        )


def test_meeting_card_event_id_matches_its_detail() -> None:
    payload = meeting_card("event_001").model_dump(mode="json")
    payload["detail"]["event_id"] = "event_002"

    with pytest.raises(ValidationError):
        MeetingCard.model_validate(payload)


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (MeetingDetail, meeting_card("event_001").detail.model_dump(mode="json")),
        (
            ParentingInteraction,
            parenting_interaction("event_003").model_dump(mode="json"),
        ),
        (ConsumedItem, consumed_item("event_010").model_dump(mode="json")),
        (InspirationIdea, inspiration_idea("event_020").model_dump(mode="json")),
    ],
)
def test_scene_event_details_reject_inverted_time_ranges(
    model_type: type, payload: dict[str, object]
) -> None:
    payload["start_ms"] = 500
    payload["end_ms"] = 500

    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


@pytest.mark.parametrize(
    ("model_type", "payload", "generic_title"),
    [
        (MeetingCard, meeting_card("event_001").model_dump(mode="json"), "会议纪要"),
        (
            ParentingCard,
            parenting_card("event_003").model_dump(mode="json"),
            "家庭教育",
        ),
        (
            ContentCard,
            ContentCard(
                event_ids=["event_010"],
                card=CardShell(title="具体内容", summary="具体摘要"),
                confidence=0.8,
                detail=ContentDetail(
                    consumed_items=[consumed_item("event_010")],
                    cross_event_insights=[],
                    recommendations=[],
                    internal_interest_signals=[],
                ),
            ).model_dump(mode="json"),
            "内容推荐",
        ),
        (GrowthCard, growth_card().model_dump(mode="json"), "成长建议"),
    ],
)
def test_visible_card_title_cannot_be_only_the_scene_name(
    model_type: type, payload: dict[str, object], generic_title: str
) -> None:
    payload["card"]["title"] = generic_title

    with pytest.raises(ValidationError):
        model_type.model_validate(payload)
