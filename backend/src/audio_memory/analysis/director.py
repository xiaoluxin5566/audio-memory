from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from audio_memory.analysis.clusters import TranscriptCluster
from audio_memory.prompts.director_schema import DirectorResult, DirectorSelection
from audio_memory.prompts.event_schema import Event, EventMap


class DirectorSelectionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "director_selection_invalid",
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AnchoredSelection:
    selection: DirectorSelection
    primary_event_id: str
    source_event_ids: tuple[str, ...]


def normalize_director_results(
    *,
    clusters: list[TranscriptCluster],
    event_map: EventMap,
    results: list[tuple[str, DirectorResult]],
) -> list[DirectorSelection]:
    cluster_ids = [cluster.cluster_id for cluster in clusters]
    cluster_indexes = {
        cluster_id: index for index, cluster_id in enumerate(cluster_ids)
    }
    known_events = {event.event_id for event in event_map.events}
    seen_batches: list[str] = []
    selections: list[DirectorSelection] = []

    for batch_cluster_id, result in results:
        if batch_cluster_id not in cluster_indexes:
            raise DirectorSelectionError("director result references unknown cluster batch")
        seen_batches.append(batch_cluster_id)
        validated = DirectorResult.model_validate(result.model_dump(mode="python"))
        for selection in validated.selections:
            for cluster_id in selection.cluster_ids:
                if cluster_id not in cluster_indexes:
                    raise DirectorSelectionError(
                        "director selection references unknown cluster"
                    )
                if cluster_id != batch_cluster_id:
                    raise DirectorSelectionError(
                        "director selection references a cluster outside its batch"
                    )
            if set(selection.source_event_ids) - known_events:
                raise DirectorSelectionError(
                    "director selection references unknown event"
                )
            selections.append(selection)

    if len(seen_batches) != len(cluster_ids) or set(seen_batches) != set(cluster_ids):
        raise DirectorSelectionError(
            "every transcript cluster must have exactly once director coverage"
        )
    if len(seen_batches) != len(set(seen_batches)):
        raise DirectorSelectionError(
            "every transcript cluster must have exactly once director coverage"
        )

    deduplicated: dict[tuple[tuple[str, ...], tuple[str, ...]], DirectorSelection] = {}
    for selection in selections:
        key = (
            tuple(selection.cluster_ids),
            tuple(sorted(selection.candidate_scenes)),
        )
        if key in deduplicated:
            continue
        stable_id = _stable_id("selection", {"clusters": key[0], "scenes": key[1]})
        deduplicated[key] = selection.model_copy(
            update={"selection_id": stable_id}
        )

    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        deduplicated.values(),
        key=lambda item: (
            min(cluster_indexes[cluster_id] for cluster_id in item.cluster_ids),
            priority_order[item.priority],
            item.selection_id,
        ),
    )


def attach_event_anchors(
    *,
    selections: list[DirectorSelection],
    clusters: list[TranscriptCluster],
    event_map: EventMap,
    segment_lookup: dict[str, dict[str, object]],
) -> tuple[EventMap, list[AnchoredSelection]]:
    clusters_by_id = {cluster.cluster_id: cluster for cluster in clusters}
    events = list(event_map.events)
    events_by_id = {event.event_id: event for event in events}
    anchored: list[AnchoredSelection] = []
    assigned_ids = {
        segment_id
        for event in events
        for segment_id in event.evidence_segment_ids
    }

    for selection in selections:
        try:
            selected_clusters = [
                clusters_by_id[cluster_id] for cluster_id in selection.cluster_ids
            ]
        except KeyError as exc:
            raise DirectorSelectionError(
                "director selection references unknown cluster"
            ) from exc
        if len({cluster.file_id for cluster in selected_clusters}) != 1:
            raise DirectorSelectionError(
                "director selection cannot combine clusters across files"
            )

        ranked_events = _rank_related_events(
            events=events,
            selected_clusters=selected_clusters,
            segment_lookup=segment_lookup,
        )
        related_by_id = {event.event_id: event for _, event in ranked_events}
        if selection.source_event_ids:
            if any(
                event_id not in related_by_id
                for event_id in selection.source_event_ids
            ):
                raise DirectorSelectionError(
                    "director source event does not overlap selected clusters"
                )
            source_event_ids = tuple(selection.source_event_ids)
            primary_event_id = min(
                source_event_ids,
                key=lambda event_id: next(
                    score
                    for score, event in ranked_events
                    if event.event_id == event_id
                ),
            )
        elif ranked_events:
            source_event_ids = tuple(event.event_id for _, event in ranked_events)
            primary_event_id = source_event_ids[0]
        else:
            anchor_id = _stable_id(
                "event_context",
                {
                    "clusters": selection.cluster_ids,
                    "scenes": sorted(selection.candidate_scenes),
                },
            )
            existing = events_by_id.get(anchor_id)
            if existing is None:
                core_segments = [
                    item
                    for cluster in selected_clusters
                    for item in cluster.segments
                ]
                anchor_evidence = next(
                    (
                        str(item["segment_id"])
                        for item in core_segments
                        if str(item["segment_id"]) not in assigned_ids
                    ),
                    None,
                )
                if anchor_evidence is None:
                    raise DirectorSelectionError(
                        "supplemental event anchor has no unassigned core evidence"
                    )
                first = core_segments[0]
                existing = Event(
                    event_id=anchor_id,
                    parent_event_id=None,
                    event_type=_anchor_event_type(selection),
                    title=selection.title,
                    start_ms=min(cluster.start_ms for cluster in selected_clusters),
                    end_ms=max(cluster.end_ms for cluster in selected_clusters),
                    speaker_ids=list(
                        dict.fromkeys(
                            str(item["speaker_id"]) for item in core_segments
                        )
                    ),
                    user_role=None,
                    user_role_confidence=0,
                    factual_summary=selection.selection_reason,
                    topics=list(selection.value_signals),
                    candidate_scenes=list(selection.candidate_scenes),
                    evidence_segment_ids=[anchor_evidence],
                    boundary_confidence=0.5,
                    local_date=first.get("local_date"),
                    timezone=(
                        str(first["timezone"])
                        if first.get("timezone") is not None
                        else None
                    ),
                )
                events.append(existing)
                events_by_id[existing.event_id] = existing
                assigned_ids.add(anchor_evidence)
            source_event_ids = (existing.event_id,)
            primary_event_id = existing.event_id

        anchored.append(
            AnchoredSelection(
                selection=selection,
                primary_event_id=primary_event_id,
                source_event_ids=source_event_ids,
            )
        )

    known_ids = set(segment_lookup)
    final_assigned_ids = {
        segment_id
        for event in events
        for segment_id in event.evidence_segment_ids
    }
    if final_assigned_ids - known_ids:
        raise DirectorSelectionError(
            "event anchor references unknown transcript evidence"
        )
    updated_map = EventMap.model_validate(
        {
            "user_speaker": event_map.user_speaker.model_dump(mode="python"),
            "events": [event.model_dump(mode="python") for event in events],
            "unassigned_segment_ids": sorted(known_ids - final_assigned_ids),
        }
    )
    return updated_map, anchored


def _rank_related_events(
    *,
    events: list[Event],
    selected_clusters: list[TranscriptCluster],
    segment_lookup: dict[str, dict[str, object]],
) -> list[tuple[tuple[int, int, str], Event]]:
    selected_segment_ids = {
        str(item["segment_id"])
        for cluster in selected_clusters
        for item in cluster.segments
    }
    selected_file_id = selected_clusters[0].file_id
    selected_start = min(cluster.start_ms for cluster in selected_clusters)
    selected_end = max(cluster.end_ms for cluster in selected_clusters)
    ranked: list[tuple[tuple[int, int, str], Event]] = []
    for event in events:
        evidence = set(event.evidence_segment_ids)
        event_file_ids = {
            str(segment_lookup[segment_id]["file_id"])
            for segment_id in evidence
            if segment_id in segment_lookup
        }
        intersection = len(selected_segment_ids & evidence)
        time_overlap = (
            selected_file_id in event_file_ids
            and event.start_ms < selected_end
            and event.end_ms > selected_start
        )
        if not intersection and not time_overlap:
            continue
        score = (-intersection, 0 if time_overlap else 1, event.event_id)
        ranked.append((score, event))
    return sorted(ranked, key=lambda item: item[0])


def _anchor_event_type(selection: DirectorSelection) -> str:
    scenes = set(selection.candidate_scenes)
    if "todo" in scenes:
        return "commitment"
    if "meeting" in scenes:
        return "meeting"
    if "parenting" in scenes:
        return "parenting"
    if "content" in scenes:
        return "media"
    return "discussion"


def _stable_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{prefix}_{sha256(canonical.encode('utf-8')).hexdigest()[:20]}"
