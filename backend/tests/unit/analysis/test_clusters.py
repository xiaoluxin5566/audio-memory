from __future__ import annotations

from audio_memory.analysis.clusters import (
    build_transcript_clusters,
    event_hints_for_cluster,
)
from audio_memory.prompts.event_schema import EventMap


def segment(
    segment_id: str,
    file_id: str,
    start_ms: int,
    end_ms: int,
    *,
    text: str | None = None,
    reliability_weight: float = 1.0,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "file_id": file_id,
        "file_name": f"{file_id}.mp3",
        "recording_started_at": None,
        "local_date": None,
        "timezone": None,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker_id": "unknown",
        "text": text or f"synthetic {segment_id}",
        "reliability_weight": reliability_weight,
    }


def event_map() -> EventMap:
    return EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": None,
                "confidence": 0,
                "reasoning": "synthetic unknown identity",
                "evidence_segment_ids": [],
            },
            "events": [
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
                    "factual_summary": "A bounded synthetic discussion.",
                    "topics": ["synthetic"],
                    "candidate_scenes": ["meeting"],
                    "evidence_segment_ids": ["seg_0_0"],
                    "boundary_confidence": 0.9,
                    "local_date": None,
                    "timezone": None,
                }
            ],
            "unassigned_segment_ids": ["seg_0_1", "seg_0_2"],
        }
    )


def test_clusters_include_event_unassigned_segments_exactly_once() -> None:
    transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 2_000, 3_000),
        segment("seg_0_2", "file-a", 50_000, 51_000),
    ]

    clusters = build_transcript_clusters(transcript)

    assert [
        item["segment_id"] for cluster in clusters for item in cluster.segments
    ] == ["seg_0_0", "seg_0_1", "seg_0_2"]
    assert [len(cluster.segments) for cluster in clusters] == [2, 1]


def test_cluster_ids_are_stable_for_the_same_transcript_identity() -> None:
    early = segment("seg_0_0", "file-a", 0, 1_000)
    late = segment("seg_0_1", "file-a", 2_000, 3_000)
    changed_metadata = {
        **late,
        "text": "updated model spelling",
        "reliability_weight": 0.6,
    }

    ordered = build_transcript_clusters([early, late])
    shuffled = build_transcript_clusters([changed_metadata, early])
    changed_time = build_transcript_clusters(
        [early, segment("seg_0_1", "file-a", 2_001, 3_001)]
    )

    assert ordered[0].cluster_id == shuffled[0].cluster_id
    assert ordered[0].cluster_id != changed_time[0].cluster_id
    assert ordered[0].cluster_id.startswith("cluster_")


def test_cluster_event_hints_use_evidence_and_omit_compatibility_data() -> None:
    transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 2_000, 3_000),
        segment("seg_0_2", "file-a", 50_000, 51_000),
    ]
    clusters = build_transcript_clusters(transcript)
    lookup = {str(item["segment_id"]): item for item in transcript}

    hints = event_hints_for_cluster(clusters[0], event_map(), lookup)

    assert hints == [
        {
            "event_id": "event_001",
            "event_type": "discussion",
            "title": "Synthetic discussion",
            "factual_summary": "A bounded synthetic discussion.",
            "start_ms": 0,
            "end_ms": 1_000,
            "candidate_scenes": ["meeting"],
        }
    ]
    assert "unassigned_segment_ids" not in repr(hints)
