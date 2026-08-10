from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from audio_memory.analysis.dossiers import SceneDossier
from audio_memory.prompts.event_schema import Event, EventMap
from audio_memory.prompts.schemas import (
    ContentSceneResult,
    GrowthSceneResult,
    InspirationSceneResult,
    MeetingSceneResult,
    ParentingSceneResult,
    SceneResultBase,
    StrictSceneResult,
    StrictTodoDraft,
)


class EvidenceIntegrityError(ValueError):
    pass


_MEDIA_EVENT_TYPES = {
    "media",
    "video",
    "youtube_video",
    "tiktok",
    "douyin",
    "live_stream",
    "livestream",
    "launch_event",
    "podcast",
    "music",
    "audiobook",
    "interview",
    "book",
    "course",
    "speech",
    "news",
    "program",
    "song",
}

_TODO_CAPABLE_EVENT_TYPES = {
    "conversation",
    "casual_chat",
    "meeting",
    "work_meeting",
    "parenting",
    "family_interaction",
    "commitment",
    "monologue",
    "phone_call",
    "discussion",
    "work_session",
}

_STRICT_SCENE_RESULT_ADAPTER = TypeAdapter(StrictSceneResult)


@dataclass(frozen=True, slots=True)
class _DossierScope:
    dossiers: tuple[SceneDossier, ...]
    segment_lookup: dict[str, dict[str, object]]


def validate_evidence_integrity(
    result: SceneResultBase,
    event_map: EventMap,
    segment_ids: set[str],
    *,
    dossiers: list[SceneDossier] | None = None,
    segment_lookup: dict[str, dict[str, object]] | None = None,
) -> None:
    try:
        event_map = EventMap.model_validate(event_map.model_dump(mode="python"))
        result = _STRICT_SCENE_RESULT_ADAPTER.validate_python(
            result.model_dump(mode="python")
        )
    except ValidationError as exc:
        raise EvidenceIntegrityError(
            f"strict schema revalidation failed: {exc}"
        ) from exc

    events = {event.event_id: event for event in event_map.events}
    scope: _DossierScope | None = None
    if dossiers is not None:
        if segment_lookup is None:
            raise EvidenceIntegrityError(
                "dossier evidence validation requires transcript segment lookup"
            )
        routed = tuple(
            SceneDossier.model_validate(dossier.model_dump(mode="python"))
            for dossier in dossiers
            if result.scene_id in dossier.candidate_scenes
        )
        if not routed:
            raise EvidenceIntegrityError(
                "scene evidence requires at least one routed dossier"
            )
        scope = _DossierScope(routed, segment_lookup)
    event_map_segments = {
        segment_id
        for event in event_map.events
        for segment_id in event.evidence_segment_ids
    }
    event_map_segments.update(event_map.unassigned_segment_ids)
    referenced_map_segments = event_map_segments | set(
        event_map.user_speaker.evidence_segment_ids
    )
    if event_map.user_speaker.confidence >= 0.85:
        _require_nonempty_unique_ids(
            event_map.user_speaker.evidence_segment_ids,
            "reliable user speaker evidence_segment_ids",
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
    requires_user_identity = any(
        todo.owner_type in {"user", "shared"} for todo in todos
    )
    if isinstance(result, MeetingSceneResult):
        requires_user_identity = requires_user_identity or any(
            todo.owner_type in {"user", "shared"}
            for card in result.cards
            for todo in card.detail.meeting_todos
        )
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
            "user identity must have a speaker_id and confidence >= 0.85"
        )
    for todo in todos:
        _validate_todo(todo, events, segment_ids, scope)

    if isinstance(result, MeetingSceneResult):
        for card in result.cards:
            event = _require_event(card.detail.event_id, events)
            _require_dossier_events([event.event_id], scope)
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
                    scope,
                )
            for meeting_todo in card.detail.meeting_todos:
                _validate_todo(meeting_todo, events, segment_ids, scope)

    if isinstance(result, ParentingSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            _require_dossier_events(card.event_ids, scope)
            card_finding_ids: list[str] = []
            for interaction in card.detail.interactions:
                event = _require_event(interaction.event_id, events)
                _require_dossier_events([interaction.event_id], scope)
                findings = [
                    *interaction.child_difficulties,
                    *interaction.emotional_signals,
                    *interaction.observed_parent_actions,
                    *interaction.possible_issues,
                ]
                finding_ids = {finding.finding_id for finding in findings}
                card_finding_ids.extend(finding.finding_id for finding in findings)
                for recommendation in interaction.recommendations:
                    basis_finding_ids = _require_nonempty_unique_ids(
                        recommendation.basis_finding_ids,
                        "basis_finding_ids",
                    )
                    if not set(basis_finding_ids).issubset(finding_ids):
                        raise EvidenceIntegrityError(
                            "basis_finding_ids must reference findings in the same interaction"
                        )
                for finding in findings:
                    _validate_event_evidence(
                        interaction.event_id,
                        finding.evidence_segment_ids,
                        event,
                        segment_ids,
                        scope,
                    )
            if len(card_finding_ids) != len(set(card_finding_ids)):
                raise EvidenceIntegrityError(
                    "finding_id values must be unique within a parenting card"
                )

    if isinstance(result, ContentSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            _require_dossier_events(card.event_ids, scope)
            for item in card.detail.consumed_items:
                event = _require_event(item.event_id, events)
                _validate_event_evidence(
                    item.event_id,
                    item.evidence_segment_ids,
                    event,
                    segment_ids,
                    scope,
                )
                for evidence_item in [*item.key_points, *item.user_reactions]:
                    _validate_event_evidence(
                        item.event_id,
                        evidence_item.evidence_segment_ids,
                        event,
                        segment_ids,
                        scope,
                    )
            for insight in card.detail.cross_event_insights:
                _require_events(insight.supporting_event_ids, events)
                _require_dossier_events(insight.supporting_event_ids, scope)
            for recommendation in card.detail.recommendations:
                _require_events(recommendation.related_event_ids, events)
                _require_dossier_events(recommendation.related_event_ids, scope)
            for signal in card.detail.internal_interest_signals:
                _require_events(signal.supporting_event_ids, events)
                _require_dossier_events(signal.supporting_event_ids, scope)

    if isinstance(result, GrowthSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            _require_dossier_events(card.event_ids, scope)
            card_case_ids: list[str] = []
            for direction in card.detail.directions:
                _require_events(direction.supporting_event_ids, events)
                _require_dossier_events(direction.supporting_event_ids, scope)
                direction_case_ids = {case.case_id for case in direction.cases}
                card_case_ids.extend(case.case_id for case in direction.cases)
                basis_case_ids = _require_nonempty_unique_ids(
                    direction.recommendation.basis_case_ids,
                    "basis_case_ids",
                )
                if not set(basis_case_ids).issubset(
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
                        scope,
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
                    scope,
                )

    if isinstance(result, InspirationSceneResult):
        for card in result.cards:
            for event_id in card.event_ids:
                _require_event(event_id, events)
            _require_dossier_events(card.event_ids, scope)
            for idea in card.detail.ideas:
                event = _require_event(idea.event_id, events)
                _validate_event_evidence(
                    idea.event_id,
                    idea.evidence_segment_ids,
                    event,
                    segment_ids,
                    scope,
                )
            for connection in card.detail.connections:
                _require_events(connection.related_event_ids, events)
                _require_dossier_events(connection.related_event_ids, scope)


def _validate_todo(
    todo: StrictTodoDraft,
    events: dict[str, Event],
    segment_ids: set[str],
    scope: _DossierScope | None,
) -> None:
    event = _require_event(todo.source_event_id, events)
    _validate_event_evidence(
        todo.source_event_id,
        todo.evidence_segment_ids,
        event,
        segment_ids,
        scope,
    )
    if event.event_type.strip().lower() in _MEDIA_EVENT_TYPES:
        raise EvidenceIntegrityError(
            f"a media event cannot be classified as a user todo: {event.event_id}"
        )
    if event.event_type.strip().lower() not in _TODO_CAPABLE_EVENT_TYPES:
        raise EvidenceIntegrityError(
            f"event type {event.event_type!r} cannot support a user todo: "
            f"{event.event_id}"
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
    scope: _DossierScope | None,
) -> None:
    evidence = set(
        _require_nonempty_unique_ids(evidence_segment_ids, "evidence_segment_ids")
    )
    unknown = evidence - segment_ids
    if unknown:
        raise EvidenceIntegrityError(
            f"result references unknown segment IDs: {sorted(unknown)}"
        )
    if scope is not None:
        _require_dossier_scope([event_id], evidence, scope)
    else:
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
    scope: _DossierScope | None,
) -> None:
    identifiers = list(event_ids)
    referenced_events = _require_events(identifiers, events)
    evidence = set(
        _require_nonempty_unique_ids(evidence_segment_ids, "evidence_segment_ids")
    )
    unknown = evidence - segment_ids
    if unknown:
        raise EvidenceIntegrityError(
            f"result references unknown segment IDs: {sorted(unknown)}"
        )
    if scope is not None:
        _require_dossier_scope(identifiers, evidence, scope)
    else:
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


def _require_dossier_scope(
    event_ids: Iterable[str],
    evidence_segment_ids: set[str],
    scope: _DossierScope,
) -> SceneDossier:
    event_id_set = set(event_ids)
    event_scopes = [
        dossier
        for dossier in scope.dossiers
        if event_id_set.issubset(
            {dossier.primary_event_id, *dossier.source_event_ids}
        )
    ]
    if not event_scopes:
        raise EvidenceIntegrityError(
            "dossier does not authorize event references"
        )
    membership_scopes = [
        dossier
        for dossier in event_scopes
        if evidence_segment_ids.issubset(dossier.allowed_segment_ids)
    ]
    if not membership_scopes:
        raise EvidenceIntegrityError(
            "evidence segment IDs are outside dossier scope"
        )
    for dossier in membership_scopes:
        aligned = True
        for segment_id in evidence_segment_ids:
            item = scope.segment_lookup.get(segment_id)
            if item is None:
                raise EvidenceIntegrityError(
                    f"result references unknown segment IDs: {[segment_id]}"
                )
            file_id = str(item.get("file_id", ""))
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if file_id not in dossier.file_ids:
                aligned = False
                break
            if not isinstance(start_ms, int) or not isinstance(end_ms, int):
                raise EvidenceIntegrityError(
                    "dossier evidence segment time range is invalid"
                )
            if start_ms < dossier.start_ms or end_ms > dossier.end_ms:
                aligned = False
                break
        if aligned:
            return dossier
    if any(
        str(scope.segment_lookup[segment_id].get("file_id", ""))
        not in dossier.file_ids
        for dossier in membership_scopes
        for segment_id in evidence_segment_ids
    ):
        raise EvidenceIntegrityError("dossier evidence crosses file boundary")
    raise EvidenceIntegrityError("dossier evidence exceeds recorded time range")


def _require_dossier_events(
    event_ids: Iterable[str], scope: _DossierScope | None
) -> None:
    if scope is None:
        return
    event_id_set = set(event_ids)
    if any(
        event_id_set.issubset(
            {dossier.primary_event_id, *dossier.source_event_ids}
        )
        for dossier in scope.dossiers
    ):
        return
    raise EvidenceIntegrityError("dossier does not authorize event references")


def _require_nonempty_unique_ids(
    values: Iterable[str],
    label: str,
) -> list[str]:
    identifiers = list(values)
    if not identifiers:
        raise EvidenceIntegrityError(f"{label} must not be empty")
    if len(identifiers) != len(set(identifiers)):
        raise EvidenceIntegrityError(f"{label} must be unique; duplicate IDs are invalid")
    return identifiers
