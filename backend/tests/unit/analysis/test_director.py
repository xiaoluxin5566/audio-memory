from __future__ import annotations

import pytest

from audio_memory.analysis.clusters import build_transcript_clusters
from audio_memory.analysis.director import (
    DirectorSelectionError,
    attach_event_anchors,
    normalize_director_results,
)
from audio_memory.prompts.director_schema import DirectorResult
from audio_memory.prompts.event_schema import EventMap


def segment(
    segment_id: str,
    file_id: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "file_id": file_id,
        "file_name": f"{file_id}.mp3",
        "recording_started_at": None,
        "local_date": "2026-08-10",
        "timezone": "Asia/Shanghai",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker_id": "unknown",
        "text": f"synthetic {segment_id}",
        "reliability_weight": 1.0,
    }


def transcript() -> list[dict[str, object]]:
    return [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 2_000, 3_000),
        segment("seg_0_2", "file-a", 50_000, 51_000),
        segment("seg_1_0", "file-b", 0, 1_000),
    ]


def base_event_map(*, assigned: bool = True) -> EventMap:
    events: list[dict[str, object]] = []
    if assigned:
        events.append(
            {
                "event_id": "event_001",
                "parent_event_id": None,
                "event_type": "discussion",
                "title": "Synthetic discussion",
                "start_ms": 0,
                "end_ms": 1_000,
                "speaker_ids": ["unknown"],
                "user_role": None,
                "user_role_confidence": 0,
                "factual_summary": "A synthetic discussion occurred.",
                "topics": ["synthetic"],
                "candidate_scenes": ["meeting"],
                "evidence_segment_ids": ["seg_0_0"],
                "boundary_confidence": 0.9,
                "local_date": "2026-08-10",
                "timezone": "Asia/Shanghai",
            }
        )
    assigned_ids = {
        segment_id
        for event in events
        for segment_id in event["evidence_segment_ids"]
    }
    return EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": None,
                "confidence": 0,
                "reasoning": "synthetic unknown identity",
                "evidence_segment_ids": [],
            },
            "events": events,
            "unassigned_segment_ids": sorted(
                {str(item["segment_id"]) for item in transcript()} - assigned_ids
            ),
        }
    )


def result(
    cluster_id: str,
    *,
    selection_id: str = "selection_001",
    source_event_ids: list[str] | None = None,
    candidate_scenes: list[str] | None = None,
    priority: str = "high",
) -> DirectorResult:
    return DirectorResult.model_validate(
        {
            "selections": [
                {
                    "selection_id": selection_id,
                    "cluster_ids": [cluster_id],
                    "source_event_ids": source_event_ids or [],
                    "candidate_scenes": candidate_scenes or ["meeting"],
                    "title": "Synthetic work discussion",
                    "selection_reason": "Contains a bounded work decision.",
                    "value_signals": ["explicit_decision"],
                    "priority": priority,
                    "context_before_clusters": 0,
                    "context_after_clusters": 1,
                }
            ]
        }
    )


def test_normalization_rejects_unknown_or_cross_batch_references() -> None:
    clusters = build_transcript_clusters(transcript())

    with pytest.raises(DirectorSelectionError, match="unknown cluster"):
        normalize_director_results(
            clusters=clusters,
            event_map=base_event_map(),
            results=[
                (
                    clusters[0].cluster_id,
                    result("cluster_ffffffffffffffffffff"),
                )
            ],
        )
    with pytest.raises(DirectorSelectionError, match="outside its batch"):
        normalize_director_results(
            clusters=clusters,
            event_map=base_event_map(),
            results=[
                (clusters[0].cluster_id, result(clusters[1].cluster_id))
            ],
        )
    with pytest.raises(DirectorSelectionError, match="unknown event"):
        normalize_director_results(
            clusters=clusters,
            event_map=base_event_map(),
            results=[
                (
                    clusters[0].cluster_id,
                    result(
                        clusters[0].cluster_id,
                        source_event_ids=["event_missing"],
                    ),
                )
            ],
        )


def test_normalization_requires_every_cluster_batch_exactly_once() -> None:
    clusters = build_transcript_clusters(transcript())

    with pytest.raises(DirectorSelectionError, match="exactly once"):
        normalize_director_results(
            clusters=clusters,
            event_map=base_event_map(),
            results=[
                (clusters[0].cluster_id, DirectorResult(selections=[])),
                (clusters[0].cluster_id, DirectorResult(selections=[])),
            ],
        )


def test_normalization_deduplicates_by_cluster_and_scene_with_stable_ids() -> None:
    clusters = build_transcript_clusters(transcript())
    duplicate = DirectorResult.model_validate(
        {
            "selections": [
                result(clusters[0].cluster_id).selections[0].model_dump(),
                result(
                    clusters[0].cluster_id,
                    selection_id="selection_002",
                ).selections[0].model_dump(),
            ]
        }
    )
    results = [
        (
            cluster.cluster_id,
            duplicate if index == 0 else DirectorResult(selections=[]),
        )
        for index, cluster in enumerate(clusters)
    ]

    first = normalize_director_results(
        clusters=clusters, event_map=base_event_map(), results=results
    )
    second = normalize_director_results(
        clusters=clusters, event_map=base_event_map(), results=results
    )

    assert len(first) == 1
    assert first[0].selection_id == second[0].selection_id
    assert first[0].selection_id.startswith("selection_")


def test_attach_event_anchors_uses_existing_evidence_linked_event() -> None:
    source = transcript()
    clusters = build_transcript_clusters(source)
    normalized = normalize_director_results(
        clusters=clusters,
        event_map=base_event_map(),
        results=[
            (
                cluster.cluster_id,
                result(cluster.cluster_id) if index == 0 else DirectorResult(selections=[]),
            )
            for index, cluster in enumerate(clusters)
        ],
    )

    updated, anchored = attach_event_anchors(
        selections=normalized,
        clusters=clusters,
        event_map=base_event_map(),
        segment_lookup={str(item["segment_id"]): item for item in source},
    )

    assert [event.event_id for event in updated.events] == ["event_001"]
    assert anchored[0].primary_event_id == "event_001"
    assert anchored[0].source_event_ids == ("event_001",)


def test_attach_event_anchors_creates_stable_minimal_supplemental_event() -> None:
    source = transcript()
    clusters = build_transcript_clusters(source)
    results = [
        (
            cluster.cluster_id,
            result(cluster.cluster_id) if index == 1 else DirectorResult(selections=[]),
        )
        for index, cluster in enumerate(clusters)
    ]
    normalized = normalize_director_results(
        clusters=clusters,
        event_map=base_event_map(),
        results=results,
    )
    lookup = {str(item["segment_id"]): item for item in source}

    first_map, first = attach_event_anchors(
        selections=normalized,
        clusters=clusters,
        event_map=base_event_map(),
        segment_lookup=lookup,
    )
    second_map, second = attach_event_anchors(
        selections=normalized,
        clusters=clusters,
        event_map=base_event_map(),
        segment_lookup=lookup,
    )

    anchor = first_map.events[-1]
    assert anchor.event_id.startswith("event_context_")
    assert anchor.event_id == second_map.events[-1].event_id
    assert first[0].primary_event_id == second[0].primary_event_id == anchor.event_id
    assert anchor.evidence_segment_ids == ["seg_0_2"]
    assert "seg_0_1" in first_map.unassigned_segment_ids
    assert set(first_map.unassigned_segment_ids) | {
        segment_id
        for event in first_map.events
        for segment_id in event.evidence_segment_ids
    } == set(lookup)


def test_attach_event_anchors_rejects_cross_file_source_event() -> None:
    source = transcript()
    clusters = build_transcript_clusters(source)
    normalized = normalize_director_results(
        clusters=clusters,
        event_map=base_event_map(),
        results=[
            (
                cluster.cluster_id,
                result(
                    cluster.cluster_id,
                    source_event_ids=["event_001"],
                )
                if cluster.file_id == "file-b"
                else DirectorResult(selections=[]),
            )
            for cluster in clusters
        ],
    )

    with pytest.raises(DirectorSelectionError, match="does not overlap"):
        attach_event_anchors(
            selections=normalized,
            clusters=clusters,
            event_map=base_event_map(),
            segment_lookup={str(item["segment_id"]): item for item in source},
        )
