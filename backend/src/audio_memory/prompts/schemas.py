from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardShell(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1_200)


class StrictTodoDraft(StrictModel):
    text: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=120)
    owner_type: Literal["user", "shared", "other", "unknown"]
    assignee_text: str | None
    due_at: datetime | None
    due_text: str | None
    intent_type: Literal["commitment", "assignment", "plan"]
    source_event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    source_context: str = Field(min_length=1, max_length=500)
    evidence_segment_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.3, le=1)


class SceneResultBase(StrictModel):
    should_generate: bool
    generation_reason: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_generation_state(self) -> SceneResultBase:
        cards = getattr(self, "cards", [])
        todos = getattr(self, "todos", [])
        if not self.should_generate and (cards or todos):
            raise ValueError("a non-generated scene must have empty cards and todos")
        scene_id = getattr(self, "scene_id", None)
        if self.should_generate and scene_id == "todo" and not todos:
            raise ValueError("a generated todo scene must include at least one todo")
        if self.should_generate and scene_id != "todo" and not cards:
            raise ValueError("a generated visible scene must include at least one card")
        return self


class MeetingParticipant(StrictModel):
    speaker_id: str = Field(min_length=1)
    display_name: str | None
    role: str | None
    evidence_segment_ids: list[str] = Field(min_length=1)


class EvidenceStatement(StrictModel):
    content: str = Field(min_length=1, max_length=1_200)
    evidence_segment_ids: list[str] = Field(min_length=1)


class MeetingDecision(EvidenceStatement):
    status: Literal["confirmed"]


class MeetingDetail(StrictModel):
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    topic: str = Field(min_length=1, max_length=300)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    background: str = Field(min_length=1, max_length=1_200)
    participants: list[MeetingParticipant]
    core_conclusions: list[EvidenceStatement]
    decisions: list[MeetingDecision]
    open_questions: list[EvidenceStatement]
    meeting_todos: list[StrictTodoDraft]
    discussion_topics: list[EvidenceStatement]

    @model_validator(mode="after")
    def validate_time_range(self) -> MeetingDetail:
        if self.end_ms <= self.start_ms:
            raise ValueError("meeting end_ms must be greater than start_ms")
        return self


class MeetingCard(StrictModel):
    event_ids: list[str] = Field(min_length=1, max_length=1)
    card: CardShell
    confidence: float = Field(ge=0, le=1)
    detail: MeetingDetail

    @model_validator(mode="after")
    def validate_event_binding(self) -> MeetingCard:
        if self.event_ids[0] != self.detail.event_id:
            raise ValueError("meeting card event_id must match its detail")
        if self.card.title.strip() in {
            "会议纪要",
            "会议总结",
            "产品会议纪要",
            "今日会议总结",
        }:
            raise ValueError("meeting card title must express the most important result")
        return self


class TodoSceneResult(SceneResultBase):
    scene_id: Literal["todo"]
    cards: list[CardShell] = Field(default_factory=list, max_length=0)
    todos: list[StrictTodoDraft]

    @model_validator(mode="after")
    def validate_global_todos(self) -> TodoSceneResult:
        if any(todo.owner_type not in {"user", "shared"} for todo in self.todos):
            raise ValueError("global todos must be owned by the user or shared")
        return self


class MeetingSceneResult(SceneResultBase):
    scene_id: Literal["meeting"]
    cards: list[MeetingCard]
    todos: list[StrictTodoDraft]

    @model_validator(mode="after")
    def validate_independent_meetings(self) -> MeetingSceneResult:
        event_ids = [card.event_ids[0] for card in self.cards]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("each meeting event may appear in only one meeting card")
        if any(todo.owner_type not in {"user", "shared"} for todo in self.todos):
            raise ValueError("global todos must be owned by the user or shared")
        return self


class ParentingFindingBase(StrictModel):
    finding_id: str = Field(pattern=r"^finding_parenting_event_[A-Za-z0-9_]+_[0-9]{2}$")
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    evidence_segment_ids: list[str] = Field(min_length=1)


class ChildDifficulty(ParentingFindingBase):
    content: str = Field(min_length=1, max_length=1_200)
    basis: str = Field(min_length=1, max_length=1_200)
    confidence: float = Field(ge=0.3, le=1)


class EmotionalSignal(ParentingFindingBase):
    signal: str = Field(min_length=1, max_length=1_200)
    possible_explanation: str = Field(min_length=1, max_length=1_200)
    confidence: float = Field(ge=0.3, le=1)


class ObservedParentAction(ParentingFindingBase):
    content: str = Field(min_length=1, max_length=1_200)
    effect: str = Field(min_length=1, max_length=1_200)


class ParentingIssue(ParentingFindingBase):
    content: str = Field(min_length=1, max_length=1_200)
    reasoning: str = Field(min_length=1, max_length=1_200)
    confidence: float = Field(ge=0.60, le=1)


class ParentingRecommendation(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    why_it_helps: str = Field(min_length=1, max_length=1_200)
    steps: list[str] = Field(min_length=1)
    suggested_language: str = Field(min_length=1, max_length=500)
    profile_basis: str | None
    basis_finding_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suggested_language_length(self) -> ParentingRecommendation:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"[。！？!?]+", self.suggested_language)
            if sentence.strip()
        ]
        if not 2 <= len(sentences) <= 3:
            raise ValueError("suggested_language must contain two or three sentences")
        return self


class ParentingInteraction(StrictModel):
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    title: str = Field(min_length=1, max_length=160)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    background: str = Field(min_length=1, max_length=1_200)
    child_difficulties: list[ChildDifficulty]
    emotional_signals: list[EmotionalSignal]
    observed_parent_actions: list[ObservedParentAction]
    possible_issues: list[ParentingIssue]
    recommendations: list[ParentingRecommendation]

    @model_validator(mode="after")
    def validate_findings_and_bases(self) -> ParentingInteraction:
        if self.end_ms <= self.start_ms:
            raise ValueError("parenting interaction end_ms must be greater than start_ms")
        findings = [
            *self.child_difficulties,
            *self.emotional_signals,
            *self.observed_parent_actions,
            *self.possible_issues,
        ]
        finding_ids = [finding.finding_id for finding in findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding_id values must be unique within an interaction")
        expected_prefix = f"finding_parenting_{self.event_id}_"
        if any(
            finding.event_id != self.event_id
            or not finding.finding_id.startswith(expected_prefix)
            for finding in findings
        ):
            raise ValueError("parenting findings must belong to their interaction event")
        known = set(finding_ids)
        if any(
            not set(recommendation.basis_finding_ids).issubset(known)
            for recommendation in self.recommendations
        ):
            raise ValueError("recommendations must reference findings in the same interaction")
        return self


class ParentingDetail(StrictModel):
    overall_observation: str = Field(min_length=1, max_length=2_000)
    interactions: list[ParentingInteraction] = Field(min_length=1)


class ParentingCard(StrictModel):
    event_ids: list[str] = Field(min_length=1)
    card: CardShell
    confidence: float = Field(ge=0, le=1)
    detail: ParentingDetail

    @model_validator(mode="after")
    def validate_grouped_events(self) -> ParentingCard:
        interaction_ids = [item.event_id for item in self.detail.interactions]
        if len(interaction_ids) != len(set(interaction_ids)):
            raise ValueError("parenting interactions must use distinct event IDs")
        if set(self.event_ids) != set(interaction_ids) or len(self.event_ids) != len(
            interaction_ids
        ):
            raise ValueError("card event_ids must match parenting interactions")
        if self.card.title.strip() in {"家庭教育", "家庭教育分析", "亲子互动"}:
            raise ValueError("parenting card title must express a concrete finding")
        return self


class ParentingSceneResult(SceneResultBase):
    scene_id: Literal["parenting"]
    cards: list[ParentingCard] = Field(max_length=1)
    todos: list[StrictTodoDraft]

    @model_validator(mode="after")
    def validate_global_todos(self) -> ParentingSceneResult:
        if any(todo.owner_type not in {"user", "shared"} for todo in self.todos):
            raise ValueError("global todos must be owned by the user or shared")
        return self


ContentType = Literal[
    "video",
    "live_stream",
    "launch_event",
    "podcast",
    "interview",
    "book",
    "course",
    "speech",
    "news",
    "program",
    "song",
    "other",
]


class ContentEvidence(StrictModel):
    content: str = Field(min_length=1, max_length=1_200)
    evidence_segment_ids: list[str] = Field(min_length=1)


class ConsumedItem(StrictModel):
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    content_type: ContentType
    platform: str | None
    source_title: str | None
    display_title: str = Field(min_length=1, max_length=200)
    title_source: Literal["explicit", "unknown"]
    inferred_title_hint: str | None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    introduction: str = Field(min_length=1, max_length=1_200)
    evidence_segment_ids: list[str] = Field(min_length=1)
    key_points: list[ContentEvidence]
    user_reactions: list[ContentEvidence]

    @model_validator(mode="after")
    def validate_title_provenance(self) -> ConsumedItem:
        if self.end_ms <= self.start_ms:
            raise ValueError("consumed item end_ms must be greater than start_ms")
        if self.title_source == "unknown" and self.source_title is not None:
            raise ValueError("unknown titles must not populate source_title")
        if self.title_source == "explicit":
            if self.source_title is None or not self.source_title.strip():
                raise ValueError("explicit titles require source_title")
            if self.inferred_title_hint is not None:
                raise ValueError("explicit titles must not carry an inferred title hint")
            vague_prefixes = (
                "那个",
                "这个",
                "那本",
                "这本",
                "一个",
                "一段",
                "某个",
                "某本",
            )
            vague_descriptions = (
                "最新的访谈",
                "最新访谈",
                "最新的视频",
                "最新视频",
            )
            if self.source_title.startswith(vague_prefixes) or any(
                phrase in self.source_title for phrase in vague_descriptions
            ):
                raise ValueError("descriptive references are not explicit source titles")
        return self


class CrossEventInsight(StrictModel):
    content: str = Field(min_length=1, max_length=1_200)
    supporting_event_ids: list[str] = Field(min_length=2)
    confidence: float = Field(ge=0.3, le=1)


class ContentRecommendation(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    content_type: ContentType
    creator: str | None
    introduction: str = Field(min_length=1, max_length=1_200)
    recommendation_reason: str = Field(min_length=1, max_length=1_200)
    related_event_ids: list[str] = Field(min_length=1)
    existence_confidence: float = Field(ge=0, le=1)
    search_query: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_uncertain_work_falls_back_to_search(self) -> ContentRecommendation:
        if self.existence_confidence < 0.90 and self.creator is not None:
            raise ValueError("uncertain works may only be represented by a search query")
        return self


class InterestSignal(StrictModel):
    dimension: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)
    evidence_mode: Literal["explicit_single_event", "multi_event_pattern"]
    supporting_event_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.3, le=1)

    @model_validator(mode="after")
    def validate_evidence_mode(self) -> InterestSignal:
        unique_events = set(self.supporting_event_ids)
        if len(unique_events) != len(self.supporting_event_ids):
            raise ValueError("interest supporting_event_ids must be unique")
        if self.evidence_mode == "explicit_single_event" and len(unique_events) != 1:
            raise ValueError("explicit_single_event must reference exactly one event")
        if self.evidence_mode == "multi_event_pattern" and len(unique_events) < 2:
            raise ValueError("multi_event_pattern must reference at least two events")
        if self.value.strip().lower() in {
            "不错",
            "挺好",
            "挺好的",
            "很好",
            "听了一下",
            "随便看看",
            "随便看了看",
        }:
            raise ValueError("lightweight reactions are not interest signals")
        return self


class ContentDetail(StrictModel):
    consumed_items: list[ConsumedItem] = Field(min_length=1)
    cross_event_insights: list[CrossEventInsight]
    recommendations: list[ContentRecommendation]
    internal_interest_signals: list[InterestSignal]


class ContentCard(StrictModel):
    event_ids: list[str] = Field(min_length=1)
    card: CardShell
    confidence: float = Field(ge=0, le=1)
    detail: ContentDetail

    @model_validator(mode="after")
    def validate_grouped_events(self) -> ContentCard:
        consumed_ids = [item.event_id for item in self.detail.consumed_items]
        if len(consumed_ids) != len(set(consumed_ids)):
            raise ValueError("consumed items must use distinct event IDs")
        if set(self.event_ids) != set(consumed_ids) or len(self.event_ids) != len(
            consumed_ids
        ):
            raise ValueError("card event_ids must match consumed items")
        known = set(self.event_ids)
        references = [
            *(insight.supporting_event_ids for insight in self.detail.cross_event_insights),
            *(item.related_event_ids for item in self.detail.recommendations),
            *(
                signal.supporting_event_ids
                for signal in self.detail.internal_interest_signals
            ),
        ]
        if any(not set(event_ids).issubset(known) for event_ids in references):
            raise ValueError("content detail may only reference events in its card")
        if self.card.title.strip() in {"内容推荐", "内容总结", "今日内容"}:
            raise ValueError("content card title must express the main focus")
        return self


class ContentSceneResult(SceneResultBase):
    scene_id: Literal["content"]
    cards: list[ContentCard] = Field(max_length=1)
    todos: list[StrictTodoDraft]

    @model_validator(mode="after")
    def validate_global_todos(self) -> ContentSceneResult:
        if any(todo.owner_type not in {"user", "shared"} for todo in self.todos):
            raise ValueError("global todos must be owned by the user or shared")
        return self

    def model_dump_for_frontend(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        for card in payload["cards"]:
            for item in card["detail"]["consumed_items"]:
                item.pop("inferred_title_hint", None)
        return payload


class GrowthCase(StrictModel):
    case_id: str = Field(pattern=r"^case_growth_[a-z][a-z0-9_]*_event_[A-Za-z0-9_]+_[0-9]{2}$")
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    title: str = Field(min_length=1, max_length=160)
    scene: str = Field(min_length=1, max_length=300)
    observed_behavior: str = Field(min_length=1, max_length=1_200)
    counterparty_response: str | None
    problem: str = Field(min_length=1, max_length=1_200)
    reasoning: str = Field(min_length=1, max_length=1_200)
    evidence_segment_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.3, le=1)


class GrowthRecommendation(StrictModel):
    goal: str = Field(min_length=1, max_length=500)
    method: str = Field(min_length=1, max_length=1_200)
    steps: list[str] = Field(min_length=1, max_length=3)
    suggested_language: str = Field(min_length=1, max_length=500)
    practice_task: str = Field(min_length=1, max_length=500)
    success_signal: str = Field(min_length=1, max_length=500)
    profile_basis: str | None
    basis_case_ids: list[str] = Field(min_length=1)


class LearningResource(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    creator: str | None
    resource_type: Literal["book", "podcast", "course", "video", "article", "other"]
    reason: str = Field(min_length=1, max_length=1_200)
    existence_confidence: float = Field(ge=0, le=1)
    search_query: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_uncertain_resource_falls_back_to_search(self) -> LearningResource:
        if self.existence_confidence < 0.90 and self.creator is not None:
            raise ValueError("uncertain resources may only be represented by a search query")
        return self


class GrowthDirection(StrictModel):
    direction_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=160)
    importance: str = Field(min_length=1, max_length=1_200)
    pattern_summary: str = Field(min_length=1, max_length=1_200)
    supporting_event_ids: list[str] = Field(min_length=1)
    cases: list[GrowthCase] = Field(min_length=1)
    recommendation: GrowthRecommendation
    resources: list[LearningResource]

    @model_validator(mode="after")
    def validate_cases_and_single_event_exception(self) -> GrowthDirection:
        supporting = set(self.supporting_event_ids)
        if len(supporting) != len(self.supporting_event_ids):
            raise ValueError("growth supporting_event_ids must be unique")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be unique within a direction")
        if any(case.event_id not in supporting for case in self.cases):
            raise ValueError("growth cases must belong to a supporting event")
        if any(
            not case.case_id.startswith(
                f"case_growth_{self.direction_id}_{case.event_id}_"
            )
            for case in self.cases
        ):
            raise ValueError("case_id must encode its direction and event")
        if not set(self.recommendation.basis_case_ids).issubset(set(case_ids)):
            raise ValueError("growth recommendation must reference cases in its direction")
        if len(supporting) == 1:
            if "不足以判断为长期模式" not in self.pattern_summary:
                raise ValueError("single-event observations must not claim a lasting pattern")
            if any(case.confidence < 0.80 for case in self.cases):
                raise ValueError("single-event growth cases require confidence >= 0.80")
            if any(
                case.counterparty_response is None
                or not case.counterparty_response.strip()
                for case in self.cases
            ):
                raise ValueError(
                    "single-event growth cases require observable negative feedback or result"
                )
        return self


class StrengthToKeep(StrictModel):
    content: str = Field(min_length=1, max_length=1_200)
    supporting_event_ids: list[str] = Field(min_length=1)
    evidence_segment_ids: list[str] = Field(min_length=1)


class GrowthDetail(StrictModel):
    overall_assessment: str = Field(min_length=1, max_length=2_000)
    directions: list[GrowthDirection] = Field(min_length=1)
    strengths_to_keep: list[StrengthToKeep]


class GrowthCard(StrictModel):
    event_ids: list[str] = Field(min_length=1)
    card: CardShell
    confidence: float = Field(ge=0, le=1)
    detail: GrowthDetail

    @model_validator(mode="after")
    def validate_grouped_events(self) -> GrowthCard:
        referenced = {
            event_id
            for direction in self.detail.directions
            for event_id in direction.supporting_event_ids
        }
        referenced.update(
            event_id
            for strength in self.detail.strengths_to_keep
            for event_id in strength.supporting_event_ids
        )
        if set(self.event_ids) != referenced or len(self.event_ids) != len(referenced):
            raise ValueError("card event_ids must match growth detail references")
        if self.card.title.strip() in {"成长建议", "成长建议总结", "成长分析"}:
            raise ValueError("growth card title must express a concrete direction")
        return self


class GrowthSceneResult(SceneResultBase):
    scene_id: Literal["growth"]
    cards: list[GrowthCard] = Field(max_length=1)
    todos: list[StrictTodoDraft]

    @model_validator(mode="after")
    def validate_single_event_reason_and_global_todos(self) -> GrowthSceneResult:
        has_single_event_exception = any(
            len(set(direction.supporting_event_ids)) == 1
            for card in self.cards
            for direction in card.detail.directions
        )
        if has_single_event_exception and "单事件例外" not in self.generation_reason:
            raise ValueError("generation_reason must mark a single-event exception")
        if any(todo.owner_type not in {"user", "shared"} for todo in self.todos):
            raise ValueError("global todos must be owned by the user or shared")
        return self


class InspirationNextStep(StrictModel):
    direction: str = Field(min_length=1, max_length=300)
    action: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_exploratory_language(self) -> InspirationNextStep:
        if any(token in self.action for token in ("需要", "必须", "应该", "截止")):
            raise ValueError("inspiration next steps must not create obligations or deadlines")
        if re.search(
            r"(?:周[一二三四五六日天]|今天|明天|后天|\d{1,2}月|\d{1,2}[日号])"
            r"[^，。！？]{0,12}前",
            self.action,
        ):
            raise ValueError("inspiration next steps must not include a concrete deadline")
        return self


class InspirationIdea(StrictModel):
    event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    title: str = Field(min_length=1, max_length=160)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    background: str = Field(min_length=1, max_length=1_200)
    conversation_summary: str = Field(min_length=1, max_length=1_200)
    core_idea: str = Field(min_length=1, max_length=1_200)
    why_valuable: str = Field(min_length=1, max_length=1_200)
    novelty_basis: str = Field(min_length=1, max_length=1_200)
    evidence_segment_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.3, le=1)
    next_steps: list[InspirationNextStep]

    @model_validator(mode="after")
    def reject_keyword_only_ideas(self) -> InspirationIdea:
        if self.end_ms <= self.start_ms:
            raise ValueError("inspiration idea end_ms must be greater than start_ms")
        evaluation_words = (
            "不错",
            "有价值",
            "可以",
            "好主意",
            "有意思",
            "值得思考",
        )
        core = self.core_idea.strip()
        novelty = self.novelty_basis.strip()
        if (
            core in evaluation_words
            or novelty in evaluation_words
            or (len(core) <= 12 and any(word in core for word in evaluation_words))
        ):
            raise ValueError("evaluation keywords alone do not constitute an inspiration")
        return self


class InspirationConnection(StrictModel):
    content: str = Field(min_length=1, max_length=1_200)
    related_event_ids: list[str] = Field(min_length=2)
    confidence: float = Field(ge=0.3, le=1)


class InspirationDetail(StrictModel):
    overall_value: str = Field(min_length=1, max_length=2_000)
    ideas: list[InspirationIdea] = Field(min_length=1)
    connections: list[InspirationConnection]


class InspirationCard(StrictModel):
    event_ids: list[str] = Field(min_length=1)
    card: CardShell
    confidence: float = Field(ge=0, le=1)
    detail: InspirationDetail

    @model_validator(mode="after")
    def validate_grouped_events(self) -> InspirationCard:
        idea_ids = [idea.event_id for idea in self.detail.ideas]
        if len(idea_ids) != len(set(idea_ids)):
            raise ValueError("inspiration ideas must use distinct event IDs")
        if set(self.event_ids) != set(idea_ids) or len(self.event_ids) != len(idea_ids):
            raise ValueError("card event_ids must match inspiration ideas")
        known = set(self.event_ids)
        if any(
            not set(connection.related_event_ids).issubset(known)
            for connection in self.detail.connections
        ):
            raise ValueError("connections may only reference events in the card")
        if self.card.title.strip() in {"今日灵感", "灵感", "闲聊灵感", "今日想法"}:
            raise ValueError("inspiration card title must express the core idea")
        return self


class InspirationSceneResult(SceneResultBase):
    scene_id: Literal["inspiration"]
    cards: list[InspirationCard] = Field(max_length=1)
    todos: list[StrictTodoDraft]

    @model_validator(mode="after")
    def validate_global_todos(self) -> InspirationSceneResult:
        if any(todo.owner_type not in {"user", "shared"} for todo in self.todos):
            raise ValueError("global todos must be owned by the user or shared")
        return self


StrictSceneResult: TypeAlias = Annotated[
    TodoSceneResult
    | MeetingSceneResult
    | ParentingSceneResult
    | ContentSceneResult
    | GrowthSceneResult
    | InspirationSceneResult,
    Field(discriminator="scene_id"),
]

SceneResultUnion: TypeAlias = StrictSceneResult


# Compatibility models for the phase-zero runner. Task 5 switches the runner to
# StrictSceneResult and can remove this block without changing the frozen models.
class GroupedItem(StrictModel):
    title: str
    items: list[str]


class DetailSection(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    kind: Literal["text", "list", "grouped_items"]
    text: str | None = None
    items: list[str] | None = None
    groups: list[GroupedItem] | None = None


class TodoDraft(StrictModel):
    text: str
    assignee: str | None = None
    due_at: str | None = None


class EvidenceRef(StrictModel):
    file_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class SceneResult(StrictModel):
    scene_id: Literal["todo", "meeting", "parenting", "content", "growth", "inspiration"]
    should_generate: bool
    card: CardShell | None = None
    detail_sections: list[DetailSection] = Field(default_factory=list)
    todos: list[TodoDraft] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
