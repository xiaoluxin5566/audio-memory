from __future__ import annotations

import pytest

from audio_memory.analysis.dossiers import SceneDossier
from audio_memory.prompts.event_schema import Event, EventMap, UserSpeaker
from audio_memory.prompts.evidence import (
    EvidenceIntegrityError,
    validate_evidence_integrity,
)
from audio_memory.prompts.schemas import (
    CardShell,
    ConsumedItem,
    ContentCard,
    ContentDetail,
    ContentEvidence,
    ContentSceneResult,
    EvidenceStatement,
    GrowthCard,
    GrowthCase,
    GrowthDetail,
    GrowthDirection,
    GrowthRecommendation,
    GrowthSceneResult,
    InspirationCard,
    InspirationDetail,
    InspirationIdea,
    InspirationSceneResult,
    InterestSignal,
    MeetingCard,
    MeetingAdaptiveSection,
    MeetingArgument,
    MeetingDetail,
    MeetingKeyFact,
    MeetingParticipant,
    MeetingQuoteAnalysis,
    MeetingRecommendation,
    MeetingSceneResult,
    MeetingUncertainty,
    ParentingCard,
    ParentingDetail,
    ParentingInteraction,
    ParentingIssue,
    ParentingRecommendation,
    ParentingSceneResult,
    StrictTodoDraft,
    TodoSceneResult,
)


def event(event_id: str, segment_id: str, *, event_type: str = "conversation") -> Event:
    return Event(
        event_id=event_id,
        parent_event_id=None,
        event_type=event_type,
        title="客观事件",
        start_ms=0,
        end_ms=1_000,
        speaker_ids=["speaker_A"],
        user_role="参与者",
        user_role_confidence=0.9,
        factual_summary="事件事实摘要。",
        topics=["范围"],
        candidate_scenes=["todo"],
        evidence_segment_ids=[segment_id],
        boundary_confidence=0.9,
        local_date="2026-08-05",
        timezone="Asia/Shanghai",
    )


def event_map(*, user_confidence: float = 0.9) -> EventMap:
    return EventMap(
        user_speaker=UserSpeaker(
            speaker_id="speaker_A",
            confidence=user_confidence,
            reasoning="存在第一人称责任锚点。",
            evidence_segment_ids=["seg_001"],
        ),
        events=[
            event("event_001", "seg_001"),
            event("event_002", "seg_002", event_type="video"),
        ],
        unassigned_segment_ids=[],
    )


def todo_result(
    *, event_id: str = "event_001", evidence_segment_ids: list[str] | None = None
) -> TodoSceneResult:
    return TodoSceneResult(
        scene_id="todo",
        should_generate=True,
        generation_reason=f"{event_id} 中用户明确承诺执行。",
        cards=[],
        todos=[
            StrictTodoDraft(
                text="发送Q3预算表",
                action="发送Q3预算表",
                owner_type="user",
                assignee_text="我",
                due_at=None,
                due_text=None,
                intent_type="commitment",
                source_event_id=event_id,
                source_context="用户明确承诺发送预算表。",
                evidence_segment_ids=evidence_segment_ids or ["seg_001"],
                confidence=0.9,
            )
        ],
        confidence=0.9,
    )


def test_rejects_unknown_segment_and_unknown_event_references() -> None:
    with pytest.raises(EvidenceIntegrityError, match="unknown segment"):
        validate_evidence_integrity(
            todo_result(evidence_segment_ids=["seg_999"]),
            event_map(),
            {"seg_001", "seg_002"},
        )
    with pytest.raises(EvidenceIntegrityError, match="unknown event"):
        validate_evidence_integrity(
            todo_result(event_id="event_999", evidence_segment_ids=["seg_001"]),
            event_map(),
            {"seg_001", "seg_002"},
        )


def test_rejects_cross_event_evidence_even_when_segment_exists() -> None:
    with pytest.raises(EvidenceIntegrityError, match="outside event_001"):
        validate_evidence_integrity(
            todo_result(evidence_segment_ids=["seg_002"]),
            event_map(),
            {"seg_001", "seg_002"},
        )


def dossier_event_map() -> EventMap:
    base = event_map()
    return base.model_copy(update={"unassigned_segment_ids": ["seg_003"]})


def dossier(
    *,
    allowed_segment_ids: tuple[str, ...] = ("seg_001", "seg_003"),
    file_ids: tuple[str, ...] = ("file-a",),
    start_ms: int = 0,
    end_ms: int = 3_000,
    source_event_ids: tuple[str, ...] = ("event_001",),
) -> SceneDossier:
    return SceneDossier(
        dossier_id="dossier_1234567890abcdefabcd",
        primary_event_id="event_001",
        source_event_ids=source_event_ids,
        candidate_scenes=("todo", "meeting"),
        selected_cluster_ids=("cluster_1234567890abcdefabcd",),
        expanded_cluster_ids=("cluster_1234567890abcdefabcd",),
        allowed_segment_ids=allowed_segment_ids,
        file_ids=file_ids,
        start_ms=start_ms,
        end_ms=end_ms,
        title="完整工作讨论",
        selection_reason="包含 Event 未分配的完整上下文。",
        priority="high",
    )


def dossier_segment_lookup() -> dict[str, dict[str, object]]:
    return {
        "seg_001": {
            "segment_id": "seg_001",
            "file_id": "file-a",
            "start_ms": 0,
            "end_ms": 1_000,
        },
        "seg_002": {
            "segment_id": "seg_002",
            "file_id": "file-a",
            "start_ms": 1_000,
            "end_ms": 2_000,
        },
        "seg_003": {
            "segment_id": "seg_003",
            "file_id": "file-a",
            "start_ms": 2_000,
            "end_ms": 3_000,
        },
    }


def test_dossier_allows_event_unassigned_todo_evidence() -> None:
    validate_evidence_integrity(
        todo_result(evidence_segment_ids=["seg_003"]),
        dossier_event_map(),
        {"seg_001", "seg_002", "seg_003"},
        dossiers=[dossier()],
        segment_lookup=dossier_segment_lookup(),
    )


@pytest.mark.parametrize(
    ("result", "scope", "lookup", "match"),
    [
        (
            todo_result(evidence_segment_ids=["seg_999"]),
            dossier(),
            dossier_segment_lookup(),
            "unknown segment",
        ),
        (
            todo_result(evidence_segment_ids=["seg_002"]),
            dossier(),
            dossier_segment_lookup(),
            "outside dossier",
        ),
        (
            todo_result(event_id="event_002", evidence_segment_ids=["seg_003"]),
            dossier(),
            dossier_segment_lookup(),
            "authorize event",
        ),
        (
            todo_result(evidence_segment_ids=["seg_003"]),
            dossier(),
            {
                **dossier_segment_lookup(),
                "seg_003": {
                    "segment_id": "seg_003",
                    "file_id": "file-b",
                    "start_ms": 2_000,
                    "end_ms": 3_000,
                },
            },
            "file",
        ),
        (
            todo_result(evidence_segment_ids=["seg_003"]),
            dossier(end_ms=2_500),
            dossier_segment_lookup(),
            "time",
        ),
    ],
)
def test_dossier_rejects_unknown_outside_or_misaligned_evidence(
    result: TodoSceneResult,
    scope: SceneDossier,
    lookup: dict[str, dict[str, object]],
    match: str,
) -> None:
    with pytest.raises(EvidenceIntegrityError, match=match):
        validate_evidence_integrity(
            result,
            dossier_event_map(),
            {"seg_001", "seg_002", "seg_003"},
            dossiers=[scope],
            segment_lookup=lookup,
        )


def test_dossier_allows_meeting_conclusion_outside_event_membership() -> None:
    result = meeting_with_cross_event_conclusion()
    result.cards[0].detail.key_facts[0].evidence_segment_ids = ["seg_003"]

    validate_evidence_integrity(
        result,
        dossier_event_map(),
        {"seg_001", "seg_002", "seg_003"},
        dossiers=[dossier()],
        segment_lookup=dossier_segment_lookup(),
    )


def adaptive_meeting_with_every_evidence_field() -> MeetingSceneResult:
    evidence = {"event_ids": ["event_001"], "evidence_segment_ids": ["seg_001"]}
    return MeetingSceneResult(
        scene_id="meeting",
        should_generate=True,
        generation_reason="同一主题包含原句、论点、分析和建议。",
        cards=[
            MeetingCard(
                event_ids=["event_001"],
                card=CardShell(title="首版范围取舍", summary="分析范围收缩的依据和风险。"),
                confidence=0.9,
                detail=MeetingDetail(
                    event_ids=["event_001"],
                    analysis_angle="首版范围如何取舍",
                    context_summary="团队讨论首版范围。",
                    participants=[
                        MeetingParticipant(
                            **evidence,
                            speaker_id="speaker_A",
                            display_name=None,
                            role="负责人",
                        )
                    ],
                    key_facts=[
                        MeetingKeyFact(
                            **evidence,
                            fact="首版聚焦已有音频。",
                            interpretation="主动收缩范围。",
                        )
                    ],
                    quote_analyses=[
                        MeetingQuoteAnalysis(
                            **evidence,
                            speaker="负责人",
                            quote="先把已有音频跑通。",
                            context="讨论首版范围。",
                            surface_meaning="首版先支持已有音频。",
                            deeper_analysis="优先验证核心价值。",
                            interaction_effect="讨论转向验收边界。",
                        )
                    ],
                    arguments=[
                        MeetingArgument(
                            **evidence,
                            speaker="负责人",
                            position="首版应聚焦。",
                            reasoning="先验证核心价值。",
                            supporting_facts=["已有音频链路可用"],
                            assumptions=["用户接受首版入口"],
                            response_from_others=None,
                            counterpoints=[],
                            assessment="方向清楚但需用户验证。",
                        )
                    ],
                    recommendations=[
                        MeetingRecommendation(
                            **evidence,
                            target="产品负责人",
                            observed_issue="成功口径不清晰。",
                            evidence_basis="只确认了功能范围。",
                            why_it_matters="无法判断首版价值。",
                            recommendation="补充质量验收标准。",
                            actions=["定义验收指标"],
                            suggested_language=None,
                            expected_result=None,
                            caveat=None,
                        )
                    ],
                    sections=[
                        MeetingAdaptiveSection(
                            **evidence,
                            section_type="tradeoff",
                            title="范围取舍",
                            narrative="团队延后实时采集以降低首版复杂度。",
                            key_points=["先验证价值"],
                        )
                    ],
                    uncertainties=[
                        MeetingUncertainty(
                            **evidence,
                            question="用户是否接受该入口？",
                            why_uncertain="没有用户验证证据。",
                        )
                    ],
                ),
            )
        ],
        todos=[],
        confidence=0.9,
    )


@pytest.mark.parametrize(
    "collection",
    [
        "participants",
        "key_facts",
        "quote_analyses",
        "arguments",
        "recommendations",
        "sections",
        "uncertainties",
    ],
)
def test_every_adaptive_meeting_item_is_checked_against_its_dossier(
    collection: str,
) -> None:
    result = adaptive_meeting_with_every_evidence_field()
    getattr(result.cards[0].detail, collection)[0].evidence_segment_ids = ["seg_002"]

    with pytest.raises(EvidenceIntegrityError, match="outside dossier"):
        validate_evidence_integrity(
            result,
            dossier_event_map(),
            {"seg_001", "seg_002", "seg_003"},
            dossiers=[dossier()],
            segment_lookup=dossier_segment_lookup(),
        )


def test_dossier_rejects_card_event_without_authorized_scope() -> None:
    result = MeetingSceneResult(
        scene_id="meeting",
        should_generate=True,
        generation_reason="event_002 被错误路由为会议。",
        cards=[
            MeetingCard(
                event_ids=["event_002"],
                card=CardShell(title="错误事件", summary="没有档案授权。"),
                confidence=0.9,
                detail=MeetingDetail(
                    event_ids=["event_002"],
                    analysis_angle="错误事件为何没有档案授权",
                    context_summary="没有档案授权。",
                    participants=[],
                    key_facts=[
                        MeetingKeyFact(
                            event_ids=["event_002"],
                            evidence_segment_ids=["seg_002"],
                            fact="该事件没有会议档案授权。",
                            interpretation=None,
                        )
                    ],
                    quote_analyses=[],
                    arguments=[],
                    recommendations=[],
                    sections=[],
                    uncertainties=[],
                ),
            )
        ],
        todos=[],
        confidence=0.9,
    )

    with pytest.raises(EvidenceIntegrityError, match="authorize event"):
        validate_evidence_integrity(
            result,
            dossier_event_map(),
            {"seg_001", "seg_002", "seg_003"},
            dossiers=[dossier()],
            segment_lookup=dossier_segment_lookup(),
        )


def test_rejects_user_attribution_below_point_eight_five() -> None:
    with pytest.raises(EvidenceIntegrityError, match="user identity"):
        validate_evidence_integrity(
            todo_result(),
            event_map(user_confidence=0.84),
            {"seg_001", "seg_002"},
        )


def test_rejects_media_action_call_misclassified_as_user_todo() -> None:
    with pytest.raises(EvidenceIntegrityError, match="media event"):
        validate_evidence_integrity(
            todo_result(event_id="event_002", evidence_segment_ids=["seg_002"]),
            event_map(),
            {"seg_001", "seg_002"},
        )


@pytest.mark.parametrize(
    "media_alias",
    [
        "media",
        "video",
        "interview",
        "podcast",
        "music",
        "audiobook",
        "livestream",
        "youtube_video",
        "tiktok",
        "douyin",
    ],
)
def test_media_alias_cannot_bypass_user_todo_guard(media_alias: str) -> None:
    alias_map = EventMap(
        user_speaker=event_map().user_speaker,
        events=[
            event("event_001", "seg_001"),
            event("event_002", "seg_002", event_type=media_alias),
        ],
        unassigned_segment_ids=[],
    )

    with pytest.raises(EvidenceIntegrityError, match="media event"):
        validate_evidence_integrity(
            todo_result(event_id="event_002", evidence_segment_ids=["seg_002"]),
            alias_map,
            {"seg_001", "seg_002"},
        )


def test_other_event_kind_cannot_default_to_user_commitment() -> None:
    other_map = EventMap(
        user_speaker=event_map().user_speaker,
        events=[
            event("event_001", "seg_001"),
            event("event_002", "seg_002", event_type="other"),
        ],
        unassigned_segment_ids=[],
    )

    with pytest.raises(EvidenceIntegrityError, match="cannot support a user todo"):
        validate_evidence_integrity(
            todo_result(event_id="event_002", evidence_segment_ids=["seg_002"]),
            other_map,
            {"seg_001", "seg_002"},
        )


def test_meeting_routed_interview_allows_objective_recommendation_with_unknown_identity() -> None:
    routed_map = dossier_event_map().model_copy(
        update={
            "user_speaker": UserSpeaker(
                speaker_id=None,
                confidence=0,
                reasoning="身份未知。",
                evidence_segment_ids=[],
            ),
            "events": [
                event("event_001", "seg_001", event_type="interview"),
                event("event_002", "seg_002", event_type="video"),
            ],
        }
    )
    result = meeting_with_cross_event_conclusion()
    result.cards[0].detail.key_facts[0].evidence_segment_ids = ["seg_001"]
    result.cards[0].detail.recommendations = [
        MeetingRecommendation(
            event_ids=["event_001"],
            target="候选人",
            observed_issue="作品集证据需要补充。",
            evidence_basis="面试中出现补充作品集的讨论。",
            why_it_matters="这会影响项目能力判断。",
            recommendation="补充能够证明个人职责的作品集材料。",
            actions=["整理项目材料"],
            suggested_language=None,
            expected_result=None,
            caveat="无法确认候选人是否为用户本人。",
            evidence_segment_ids=["seg_001"],
        )
    ]

    validate_evidence_integrity(
        result,
        routed_map,
        {"seg_001", "seg_002", "seg_003"},
        dossiers=[dossier()],
        segment_lookup=dossier_segment_lookup(),
    )


def test_event_map_must_account_for_every_transcript_segment() -> None:
    with pytest.raises(EvidenceIntegrityError, match="unassigned"):
        validate_evidence_integrity(
            todo_result(),
            event_map(),
            {"seg_001", "seg_002", "seg_003"},
        )

    validate_evidence_integrity(
        todo_result(),
        event_map(),
        {"seg_001", "seg_002"},
    )


def meeting_with_cross_event_conclusion() -> MeetingSceneResult:
    return MeetingSceneResult(
        scene_id="meeting",
        should_generate=True,
        generation_reason="event_001 形成结论。",
        cards=[
            MeetingCard(
                event_ids=["event_001"],
                card=CardShell(title="确认一期范围", summary="只支持上传已有音频。"),
                confidence=0.9,
                detail=MeetingDetail(
                    event_ids=["event_001"],
                    analysis_angle="一期范围如何取舍",
                    context_summary="产品范围评审。",
                    participants=[],
                    key_facts=[
                        MeetingKeyFact(
                            event_ids=["event_001"],
                            fact="第一期只支持上传已有音频。",
                            interpretation="团队主动收缩范围。",
                            evidence_segment_ids=["seg_002"],
                        )
                    ],
                    quote_analyses=[],
                    arguments=[],
                    recommendations=[],
                    sections=[],
                    uncertainties=[],
                ),
            )
        ],
        todos=[],
        confidence=0.9,
    )


def parenting_with_cross_event_finding() -> ParentingSceneResult:
    finding_id = "finding_parenting_event_001_01"
    return ParentingSceneResult(
        scene_id="parenting",
        should_generate=True,
        generation_reason="event_001 中有可复盘互动。",
        cards=[
            ParentingCard(
                event_ids=["event_001"],
                card=CardShell(title="先确认孩子的困难", summary="催促后抗拒加重。"),
                confidence=0.8,
                detail=ParentingDetail(
                    overall_observation="仅分析本次互动。",
                    interactions=[
                        ParentingInteraction(
                            event_id="event_001",
                            title="作业沟通",
                            start_ms=0,
                            end_ms=1_000,
                            background="孩子解题受挫。",
                            child_difficulties=[],
                            emotional_signals=[],
                            observed_parent_actions=[],
                            possible_issues=[
                                ParentingIssue(
                                    finding_id=finding_id,
                                    event_id="event_001",
                                    content="催促后抗拒加重",
                                    reasoning="出现连续回应",
                                    evidence_segment_ids=["seg_002"],
                                    confidence=0.7,
                                )
                            ],
                            recommendations=[],
                        )
                    ],
                ),
            )
        ],
        todos=[],
        confidence=0.8,
    )


def content_with_cross_event_key_point() -> ContentSceneResult:
    return ContentSceneResult(
        scene_id="content",
        should_generate=True,
        generation_reason="event_001 中消费了有价值内容。",
        cards=[
            ContentCard(
                event_ids=["event_001"],
                card=CardShell(title="端侧体验视频", summary="讨论交互响应速度。"),
                confidence=0.8,
                detail=ContentDetail(
                    consumed_items=[
                        ConsumedItem(
                            event_id="event_001",
                            content_type="video",
                            platform=None,
                            source_title=None,
                            display_title="一段端侧体验视频",
                            title_source="unknown",
                            inferred_title_hint=None,
                            start_ms=0,
                            end_ms=1_000,
                            introduction="讨论交互响应速度。",
                            evidence_segment_ids=["seg_001"],
                            key_points=[
                                ContentEvidence(
                                    content="响应速度影响连续性。",
                                    evidence_segment_ids=["seg_002"],
                                )
                            ],
                            user_reactions=[],
                        )
                    ],
                    cross_event_insights=[],
                    recommendations=[],
                    internal_interest_signals=[],
                ),
            )
        ],
        todos=[],
        confidence=0.8,
    )


def growth_with_cross_event_case() -> GrowthSceneResult:
    case_id = "case_growth_communication_event_001_01"
    return GrowthSceneResult(
        scene_id="growth",
        should_generate=True,
        generation_reason="单事件例外：event_001 是高影响评审，方案被要求重做。",
        cards=[
            GrowthCard(
                event_ids=["event_001"],
                card=CardShell(title="先确认范围", summary="本次评审出现明确返工。"),
                confidence=0.8,
                detail=GrowthDetail(
                    overall_assessment="只评价本次行为。",
                    directions=[
                        GrowthDirection(
                            direction_id="communication",
                            title="先确认范围",
                            importance="减少返工。",
                            pattern_summary="本次场景中的观察，不足以判断为长期模式。",
                            supporting_event_ids=["event_001"],
                            cases=[
                                GrowthCase(
                                    case_id=case_id,
                                    event_id="event_001",
                                    title="范围未对齐",
                                    scene="产品评审",
                                    observed_behavior="直接展开方案。",
                                    counterparty_response="方案被要求重做。",
                                    problem="范围错误。",
                                    reasoning="评审方明确指出错误。",
                                    evidence_segment_ids=["seg_002"],
                                    confidence=0.8,
                                )
                            ],
                            recommendation=GrowthRecommendation(
                                goal="先确认范围",
                                method="复述目标与不做项。",
                                steps=["复述目标"],
                                suggested_language="我先确认一下范围。",
                                practice_task="写一句开场白。",
                                success_signal="对方明确确认范围。",
                                profile_basis=None,
                                basis_case_ids=[case_id],
                            ),
                            resources=[],
                        )
                    ],
                    strengths_to_keep=[],
                ),
            )
        ],
        todos=[],
        confidence=0.8,
    )


def inspiration_with_cross_event_evidence() -> InspirationSceneResult:
    return InspirationSceneResult(
        scene_id="inspiration",
        should_generate=True,
        generation_reason="event_001 中形成新判断。",
        cards=[
            InspirationCard(
                event_ids=["event_001"],
                card=CardShell(title="入口应贴近已有习惯", summary="可继续验证入口假设。"),
                confidence=0.8,
                detail=InspirationDetail(
                    overall_value="形成可验证的新连接。",
                    ideas=[
                        InspirationIdea(
                            event_id="event_001",
                            title="入口贴近已有习惯",
                            start_ms=0,
                            end_ms=1_000,
                            background="讨论回访率。",
                            conversation_summary="将低回访与入口路径联系起来。",
                            core_idea="把入口放进已有工作流。",
                            why_valuable="提供入口假设。",
                            novelty_basis="建立了新的因果假设。",
                            evidence_segment_ids=["seg_002"],
                            confidence=0.8,
                            next_steps=[],
                        )
                    ],
                    connections=[],
                ),
            )
        ],
        todos=[],
        confidence=0.8,
    )


@pytest.mark.parametrize(
    "result_factory",
    [
        meeting_with_cross_event_conclusion,
        parenting_with_cross_event_finding,
        content_with_cross_event_key_point,
        growth_with_cross_event_case,
        inspiration_with_cross_event_evidence,
    ],
)
def test_nested_scene_evidence_cannot_cross_event_boundaries(result_factory) -> None:
    with pytest.raises(EvidenceIntegrityError, match="outside event_001"):
        validate_evidence_integrity(
            result_factory(),
            event_map(),
            {"seg_001", "seg_002"},
        )


def valid_parenting_result() -> ParentingSceneResult:
    payload = parenting_with_cross_event_finding().model_dump(mode="json")
    payload["cards"][0]["detail"]["interactions"][0]["possible_issues"][0][
        "evidence_segment_ids"
    ] = ["seg_001"]
    return ParentingSceneResult.model_validate(payload)


def valid_growth_result() -> GrowthSceneResult:
    payload = growth_with_cross_event_case().model_dump(mode="json")
    payload["cards"][0]["detail"]["directions"][0]["cases"][0][
        "evidence_segment_ids"
    ] = ["seg_001"]
    return GrowthSceneResult.model_validate(payload)


def content_with_interest_signal() -> ContentSceneResult:
    payload = content_with_cross_event_key_point().model_dump(mode="json")
    payload["cards"][0]["detail"]["consumed_items"][0]["key_points"][0][
        "evidence_segment_ids"
    ] = ["seg_001"]
    payload["cards"][0]["detail"]["internal_interest_signals"] = [
        InterestSignal(
            dimension="product_interest",
            value="端侧 AI 产品体验",
            evidence_mode="explicit_single_event",
            supporting_event_ids=["event_001"],
            confidence=0.8,
        ).model_dump(mode="json")
    ]
    return ContentSceneResult.model_validate(payload)


@pytest.mark.parametrize(
    "result_factory",
    [valid_parenting_result, valid_growth_result, content_with_interest_signal],
)
def test_low_identity_rejects_personal_evaluation_or_profile_signal(result_factory) -> None:
    with pytest.raises(EvidenceIntegrityError, match="user identity"):
        validate_evidence_integrity(
            result_factory(),
            event_map(user_confidence=0.84),
            {"seg_001", "seg_002"},
        )


def test_low_identity_still_allows_objective_content_consumption_record() -> None:
    payload = content_with_cross_event_key_point().model_dump(mode="json")
    payload["cards"][0]["detail"]["consumed_items"][0]["key_points"][0][
        "evidence_segment_ids"
    ] = ["seg_001"]

    validate_evidence_integrity(
        ContentSceneResult.model_validate(payload),
        event_map(user_confidence=0.84),
        {"seg_001", "seg_002"},
    )


def test_integrity_check_rejects_nonexistent_parenting_basis_even_if_model_was_bypassed() -> None:
    result = valid_parenting_result()
    card = result.cards[0]
    interaction = card.detail.interactions[0]
    invalid_recommendation = ParentingRecommendation(
        title="错误建议",
        why_it_helps="没有实际 finding 支持。",
        steps=["先确认依据"],
        suggested_language="我们先确认发生了什么。再一起看下一步。",
        profile_basis=None,
        basis_finding_ids=["finding_parenting_event_001_99"],
    )
    invalid_interaction = interaction.model_copy(
        update={"recommendations": [invalid_recommendation]}
    )
    invalid_detail = card.detail.model_copy(update={"interactions": [invalid_interaction]})
    invalid_card = card.model_copy(update={"detail": invalid_detail})
    invalid_result = result.model_copy(update={"cards": [invalid_card]})

    with pytest.raises(EvidenceIntegrityError, match="basis_finding_ids"):
        validate_evidence_integrity(
            invalid_result,
            event_map(),
            {"seg_001", "seg_002"},
        )


def test_integrity_check_rejects_nonexistent_growth_basis_even_if_model_was_bypassed() -> None:
    result = valid_growth_result()
    card = result.cards[0]
    direction = card.detail.directions[0]
    invalid_recommendation = direction.recommendation.model_copy(
        update={"basis_case_ids": ["case_growth_communication_event_001_99"]}
    )
    invalid_direction = direction.model_copy(update={"recommendation": invalid_recommendation})
    invalid_detail = card.detail.model_copy(update={"directions": [invalid_direction]})
    invalid_card = card.model_copy(update={"detail": invalid_detail})
    invalid_result = result.model_copy(update={"cards": [invalid_card]})

    with pytest.raises(EvidenceIntegrityError, match="basis_case_ids"):
        validate_evidence_integrity(
            invalid_result,
            event_map(),
            {"seg_001", "seg_002"},
        )


def test_integrity_revalidates_model_construct_with_empty_evidence() -> None:
    result = todo_result()
    todo_payload = result.todos[0].model_dump(mode="python")
    todo_payload["evidence_segment_ids"] = []
    invalid_todo = StrictTodoDraft.model_construct(**todo_payload)
    invalid_result = result.model_copy(update={"todos": [invalid_todo]})

    with pytest.raises(EvidenceIntegrityError, match="strict schema|evidence"):
        validate_evidence_integrity(
            invalid_result,
            event_map(),
            {"seg_001", "seg_002"},
        )


def test_integrity_rejects_duplicate_evidence_after_model_copy_bypass() -> None:
    result = todo_result()
    invalid_todo = result.todos[0].model_copy(
        update={"evidence_segment_ids": ["seg_001", "seg_001"]}
    )
    invalid_result = result.model_copy(update={"todos": [invalid_todo]})

    with pytest.raises(EvidenceIntegrityError, match="unique|duplicate"):
        validate_evidence_integrity(
            invalid_result,
            event_map(),
            {"seg_001", "seg_002"},
        )


def test_integrity_rejects_empty_parenting_basis_after_model_copy_bypass() -> None:
    result = valid_parenting_result()
    card = result.cards[0]
    interaction = card.detail.interactions[0]
    recommendation = ParentingRecommendation(
        title="先确认困难点",
        why_it_helps="建议必须有本次互动 finding 支持。",
        steps=["先确认事实"],
        suggested_language="我们先确认发生了什么。再一起看下一步。",
        profile_basis=None,
        basis_finding_ids=["finding_parenting_event_001_01"],
    ).model_copy(update={"basis_finding_ids": []})
    invalid_interaction = interaction.model_copy(
        update={"recommendations": [recommendation]}
    )
    invalid_card = card.model_copy(
        update={
            "detail": card.detail.model_copy(
                update={"interactions": [invalid_interaction]}
            )
        }
    )
    invalid_result = result.model_copy(update={"cards": [invalid_card]})

    with pytest.raises(EvidenceIntegrityError, match="basis_finding_ids|strict schema"):
        validate_evidence_integrity(
            invalid_result,
            event_map(),
            {"seg_001", "seg_002"},
        )


def test_integrity_revalidates_reliable_user_evidence_after_model_copy_bypass() -> None:
    valid_map = event_map(user_confidence=0.85)
    invalid_speaker = valid_map.user_speaker.model_copy(
        update={"evidence_segment_ids": []}
    )
    invalid_map = valid_map.model_copy(update={"user_speaker": invalid_speaker})

    with pytest.raises(EvidenceIntegrityError, match="strict schema|evidence"):
        validate_evidence_integrity(
            todo_result(),
            invalid_map,
            {"seg_001", "seg_002"},
        )


def test_reliable_user_speaker_evidence_must_reference_a_transcript_segment() -> None:
    valid_map = event_map(user_confidence=0.85)
    invalid_speaker = valid_map.user_speaker.model_copy(
        update={"evidence_segment_ids": ["seg_999"]}
    )
    invalid_map = valid_map.model_copy(update={"user_speaker": invalid_speaker})

    with pytest.raises(EvidenceIntegrityError, match="unknown segment"):
        validate_evidence_integrity(
            todo_result(),
            invalid_map,
            {"seg_001", "seg_002"},
        )
