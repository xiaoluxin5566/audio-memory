from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from audio_memory.analysis.windows import build_analysis_windows
from audio_memory.prompts.event_schema import EventMap


@dataclass(frozen=True, slots=True)
class TranscriptCluster:
    cluster_id: str
    file_id: str
    file_name: str
    start_ms: int
    end_ms: int
    segments: tuple[dict[str, object], ...]


def build_transcript_clusters(
    transcript: list[dict[str, object]],
) -> list[TranscriptCluster]:
    clusters: list[TranscriptCluster] = []
    for window in build_analysis_windows(transcript):
        identity = {
            "file_id": window.file_id,
            "start_ms": window.start_ms,
            "end_ms": window.end_ms,
            "segment_ids": [str(item["segment_id"]) for item in window.segments],
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        clusters.append(
            TranscriptCluster(
                cluster_id=f"cluster_{sha256(canonical.encode('utf-8')).hexdigest()[:20]}",
                file_id=window.file_id,
                file_name=str(window.segments[0]["file_name"]),
                start_ms=window.start_ms,
                end_ms=window.end_ms,
                segments=window.segments,
            )
        )
    return clusters


def event_hints_for_cluster(
    cluster: TranscriptCluster,
    event_map: EventMap,
    segment_lookup: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    cluster_segment_ids = {
        str(item["segment_id"]) for item in cluster.segments
    }
    hints: list[dict[str, object]] = []
    for event in event_map.events:
        evidence_ids = set(event.evidence_segment_ids)
        event_files = {
            str(segment_lookup[segment_id]["file_id"])
            for segment_id in evidence_ids
            if segment_id in segment_lookup
        }
        evidence_linked = bool(cluster_segment_ids & evidence_ids)
        time_linked = (
            cluster.file_id in event_files
            and event.start_ms < cluster.end_ms
            and event.end_ms > cluster.start_ms
        )
        if not evidence_linked and not time_linked:
            continue
        hints.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "title": event.title,
                "factual_summary": event.factual_summary,
                "start_ms": event.start_ms,
                "end_ms": event.end_ms,
                "candidate_scenes": list(event.candidate_scenes),
            }
        )
    return hints
