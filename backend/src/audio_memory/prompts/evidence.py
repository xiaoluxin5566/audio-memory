from __future__ import annotations

from collections.abc import Iterable

from audio_memory.prompts.event_schema import Event, EventMap
from audio_memory.prompts.schemas import (
    ContentSceneResult,
    GrowthSceneResult,
    InspirationSceneResult,
    MeetingSceneResult,
    ParentingSceneResult,
    SceneResultBase,
    StrictTodoDraft,
)


class EvidenceIntegrityError(ValueError):
    pass


_MEDIA_EVENT_TYPES = {
    "media",
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
}


def validate_evidence_integrity(
    result: SceneResultBase,
    event_map: EventMap,
    segment_ids: set[str],
) -> None:
    events = {event.event_id: event for event in event_map.events}
    event_map_segments = {
        segment_id
        for event in event_map.events
        for segment_id in event.evidence_segment_ids
    }
    event_map_segments.update(event_map.unassigned_segment_ids)
    referenced_map_segments = event_map_segments | set(
        event_map.user_speaker.evidence_segment_ids
    )
    unknown_map_segments = referenced_map_segments - segment_ids
    if unknown_map_segments:
        raise EvidenceIntegrityError(
            f"event map references unknown segment IDs: {sorted(unknown_map_segments)}"
        )
    missing_segments = segment_ids - event_map_segments
    if missing_segments:
        raise EvidenceIntegrityError(
            "event map must list transcript segments in an event or unassigned_segment_ids: "
            f"{sorted(missing_segments)}"
        )

    todos: list[StrictTodoDraft] = list(getattr(result, "todos", []))
    requires_user_identity = bool(todos)
    if isinstance(result, ParentingSceneResult):
        requires_user_identity = requires_user_identity or any(
            interaction.observed_parent_actions
            or interaction.possible_issues
            or interaction.recommendations
            for card in result.cards
            for interaction in card.detail.interactions
        )
    if isinstance(result, ContentSceneResult):
        requires_user_identity = requires_user_identity or any(
            card.detail.internal_interest_signals
            or any(item.user_reactions for item in card.detail.consumed_items)
            for card in result.cards
        )
    if isinstance(result, GrowthSceneResult):
        requires_user_identity = requires_user_identity or bool(result.cards)
    if requires_user_identity and not event_map.user_speaker.is_reliable:
        raise EvidenceIntegrityError(
            "user identity must have a speaker_id and confidence >= 0.70"
        )
    for todo in todos:
        _validate_todo(todo, events, segment_ids)

    if isinstance(result, MeetingSceneResult):
        for card in result.cards:
            event = _require_event(card.detail.event_id, events)
            evidence_items = [
                *card.detail.participants,
                *card.detail.core_conclusions,
                *card.detail.decisions,
                *card.detail.open_questions,
                *card.detail.discussion_topics,
            ]
            for item in evidence_items:
                _validate_event_evidence(
                    event.event_id,
                    item.evidence_segment_ids,
                    event,
                    segment_ids,
                )
            for meeting_todo in card.detail.meeting_todos:
                _validate_todo(meeting_todo, events, segment_ids)

    if isinstance(result, ParentingSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            card_finding_ids: list[str] = []
            for interaction in card.detail.interactions:
                event = _require_event(interaction.event_id, events)
                findings = [
                    *interaction.child_difficulties,
                    *interaction.emotional_signals,
                    *interaction.observed_parent_actions,
                    *interaction.possible_issues,
                ]
                finding_ids = {finding.finding_id for finding in findings}
                card_finding_ids.extend(finding.finding_id for finding in findings)
                for recommendation in interaction.recommendations:
                    if not set(recommendation.basis_finding_ids).issubset(finding_ids):
                        raise EvidenceIntegrityError(
                            "basis_finding_ids must reference findings in the same interaction"
                        )
                for finding in findings:
                    _validate_event_evidence(
                        interaction.event_id,
                        finding.evidence_segment_ids,
                        event,
                        segment_ids,
                    )
            if len(card_finding_ids) != len(set(card_finding_ids)):
                raise EvidenceIntegrityError(
                    "finding_id values must be unique within a parenting card"
                )

    if isinstance(result, ContentSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            for item in card.detail.consumed_items:
                event = _require_event(item.event_id, events)
                _validate_event_evidence(
                    item.event_id,
                    item.evidence_segment_ids,
                    event,
                    segment_ids,
                )
                for evidence_item in [*item.key_points, *item.user_reactions]:
                    _validate_event_evidence(
                        item.event_id,
                        evidence_item.evidence_segment_ids,
                        event,
                        segment_ids,
                    )
            for insight in card.detail.cross_event_insights:
                _require_events(insight.supporting_event_ids, events)
            for recommendation in card.detail.recommendations:
                _require_events(recommendation.related_event_ids, events)
            for signal in card.detail.internal_interest_signals:
                _require_events(signal.supporting_event_ids, events)

    if isinstance(result, GrowthSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            card_case_ids: list[str] = []
            for direction in card.detail.directions:
                _require_events(direction.supporting_event_ids, events)
                direction_case_ids = {case.case_id for case in direction.cases}
                card_case_ids.extend(case.case_id for case in direction.cases)
                if not set(direction.recommendation.basis_case_ids).issubset(
                    direction_case_ids
                ):
                    raise EvidenceIntegrityError(
                        "basis_case_ids must reference cases in the same growth direction"
                    )
                for case in direction.cases:
                    event = _require_event(case.event_id, events)
                    _validate_event_evidence(
                        case.event_id,
                        case.evidence_segment_ids,
                        event,
                        segment_ids,
                    )
            if len(card_case_ids) != len(set(card_case_ids)):
                raise EvidenceIntegrityError(
                    "case_id values must be unique within a growth card"
                )
            for strength in card.detail.strengths_to_keep:
                _validate_multi_event_evidence(
                    strength.supporting_event_ids,
                    strength.evidence_segment_ids,
                    events,
                    segment_ids,
                )

    if isinstance(result, InspirationSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            for idea in card.detail.ideas:
                event = _require_event(idea.event_id, events)
                _validate_event_evidence(
                    idea.event_id,
                    idea.evidence_segment_ids,
                    event,
                    segment_ids,
                )
            for connection in card.detail.connections:
                _require_events(connection.related_event_ids, events)


def _validate_todo(
    todo: StrictTodoDraft,
    events: dict[str, Event],
    segment_ids: set[str],
) -> None:
    event = _require_event(todo.source_event_id, events)
    _validate_event_evidence(
        todo.source_event_id,
        todo.evidence_segment_ids,
        event,
        segment_ids,
    )
    if event.event_type.strip().lower() in _MEDIA_EVENT_TYPES:
        raise EvidenceIntegrityError(
            f"a media event cannot be classified as a user todo: {event.event_id}"
        )


def _require_event(event_id: str, events: dict[str, Event]) -> Event:
    event = events.get(event_id)
    if event is None:
        raise EvidenceIntegrityError(f"result references unknown event ID: {event_id}")
    return event


def _require_events(event_ids: Iterable[str], events: dict[str, Event]) -> list[Event]:
    return [_require_event(event_id, events) for event_id in event_ids]


def _validate_event_evidence(
    event_id: str,
    evidence_segment_ids: Iterable[str],
    event: Event,
    segment_ids: set[str],
) -> None:
    evidence = set(evidence_segment_ids)
    unknown = evidence - segment_ids
    if unknown:
        raise EvidenceIntegrityError(
            f"result references unknown segment IDs: {sorted(unknown)}"
        )
    outside = evidence - set(event.evidence_segment_ids)
    if outside:
        raise EvidenceIntegrityError(
            f"evidence segment IDs {sorted(outside)} are outside {event_id}"
        )


def _validate_multi_event_evidence(
    event_ids: Iterable[str],
    evidence_segment_ids: Iterable[str],
    events: dict[str, Event],
    segment_ids: set[str],
) -> None:
    referenced_events = _require_events(event_ids, events)
    evidence = set(evidence_segment_ids)
    unknown = evidence - segment_ids
    if unknown:
        raise EvidenceIntegrityError(
            f"result references unknown segment IDs: {sorted(unknown)}"
        )
    allowed = {
        segment_id
        for event in referenced_events
        for segment_id in event.evidence_segment_ids
    }
    outside = evidence - allowed
    if outside:
        raise EvidenceIntegrityError(
            f"evidence segment IDs {sorted(outside)} are outside referenced events"
        )
