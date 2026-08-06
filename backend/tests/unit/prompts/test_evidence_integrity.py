from __future__ import annotations

import pytest

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
    MeetingDetail,
    MeetingSceneResult,
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


def test_rejects_user_attribution_below_point_seven() -> None:
    with pytest.raises(EvidenceIntegrityError, match="user identity"):
        validate_evidence_integrity(
            todo_result(),
            event_map(user_confidence=0.69),
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
                    event_id="event_001",
                    topic="一期范围",
                    start_ms=0,
                    end_ms=1_000,
                    background="产品范围评审。",
                    participants=[],
                    core_conclusions=[
                        EvidenceStatement(
                            content="第一期只支持上传已有音频。",
                            evidence_segment_ids=["seg_002"],
                        )
                    ],
                    decisions=[],
                    open_questions=[],
                    meeting_todos=[],
                    discussion_topics=[],
                ),
            )
        ],
        todos=[],
        confidence=0.9,
    )


def meeting_with_user_owned_detail_todo() -> MeetingSceneResult:
    payload = meeting_with_cross_event_conclusion().model_dump(mode="json")
    payload["cards"][0]["detail"]["core_conclusions"][0][
        "evidence_segment_ids"
    ] = ["seg_001"]
    payload["cards"][0]["detail"]["meeting_todos"] = [
        todo_result().todos[0].model_dump(mode="json")
    ]
    return MeetingSceneResult.model_validate(payload)


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
            event_map(user_confidence=0.69),
            {"seg_001", "seg_002"},
        )


def test_low_identity_still_allows_objective_content_consumption_record() -> None:
    payload = content_with_cross_event_key_point().model_dump(mode="json")
    payload["cards"][0]["detail"]["consumed_items"][0]["key_points"][0][
        "evidence_segment_ids"
    ] = ["seg_001"]

    validate_evidence_integrity(
        ContentSceneResult.model_validate(payload),
        event_map(user_confidence=0.69),
        {"seg_001", "seg_002"},
    )


def test_meeting_detail_user_todo_uses_point_seven_identity_boundary() -> None:
    result = meeting_with_user_owned_detail_todo()

    with pytest.raises(EvidenceIntegrityError, match="user identity"):
        validate_evidence_integrity(
            result,
            event_map(user_confidence=0.69),
            {"seg_001", "seg_002"},
        )

    validate_evidence_integrity(
        result,
        event_map(user_confidence=0.70),
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
    valid_map = event_map(user_confidence=0.70)
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
    valid_map = event_map(user_confidence=0.70)
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
