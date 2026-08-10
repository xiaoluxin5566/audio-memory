from __future__ import annotations

import pytest

from audio_memory.analysis.clusters import TranscriptCluster
from audio_memory.analysis.director import AnchoredSelection
from audio_memory.analysis.dossiers import (
    DossierBuildError,
    build_scene_dossiers,
    dossiers_for_scene,
)
from audio_memory.prompts.director_schema import DirectorSelection


def cluster(
    cluster_id: str,
    file_id: str,
    start_ms: int,
    end_ms: int,
    count: int,
) -> TranscriptCluster:
    duration = max(1, end_ms - start_ms)
    segments = tuple(
        {
            "segment_id": f"seg_{cluster_id}_{index}",
            "file_id": file_id,
            "file_name": f"{file_id}.mp3",
            "recording_started_at": None,
            "local_date": None,
            "timezone": None,
            "start_ms": start_ms + (duration * index // count),
            "end_ms": start_ms + (duration * (index + 1) // count),
            "speaker_id": "unknown",
            "text": f"synthetic {index}",
        }
        for index in range(count)
    )
    return TranscriptCluster(
        cluster_id=cluster_id,
        file_id=file_id,
        file_name=f"{file_id}.mp3",
        start_ms=start_ms,
        end_ms=end_ms,
        segments=segments,
    )


def anchored(
    selected_cluster_id: str,
    *,
    before: int = 1,
    after: int = 1,
    scenes: list[str] | None = None,
) -> AnchoredSelection:
    selection = DirectorSelection.model_validate(
        {
            "selection_id": "selection_1234567890abcdefabcd",
            "cluster_ids": [selected_cluster_id],
            "source_event_ids": [],
            "candidate_scenes": scenes or ["meeting", "todo"],
            "title": "Synthetic bounded discussion",
            "selection_reason": "Contains a bounded work decision.",
            "value_signals": ["explicit_decision"],
            "priority": "high",
            "context_before_clusters": before,
            "context_after_clusters": after,
        }
    )
    return AnchoredSelection(selection, "event_001", ("event_001",))


def test_dossier_expands_only_direct_same_file_neighbors() -> None:
    clusters = [
        cluster("cluster_aaaaaaaaaaaaaaaaaaaa", "file-a", 0, 300_000, 2),
        cluster("cluster_bbbbbbbbbbbbbbbbbbbb", "file-a", 350_000, 650_000, 2),
        cluster("cluster_cccccccccccccccccccc", "file-b", 0, 300_000, 2),
    ]

    dossier = build_scene_dossiers(
        selections=[anchored(clusters[1].cluster_id)], clusters=clusters
    )[0]

    assert dossier.selected_cluster_ids == (clusters[1].cluster_id,)
    assert dossier.expanded_cluster_ids == (
        clusters[0].cluster_id,
        clusters[1].cluster_id,
    )
    assert dossier.file_ids == ("file-a",)
    assert dossier.start_ms == 0
    assert dossier.end_ms == 650_000
    assert len(dossier.allowed_segment_ids) == 4
    assert dossiers_for_scene([dossier], "meeting") == [dossier]
    assert dossiers_for_scene([dossier], "content") == []


def test_dossier_trims_farthest_neighbor_when_span_cap_would_be_exceeded() -> None:
    clusters = [
        cluster("cluster_aaaaaaaaaaaaaaaaaaaa", "file-a", 0, 500_000, 2),
        cluster("cluster_bbbbbbbbbbbbbbbbbbbb", "file-a", 700_000, 1_600_000, 2),
        cluster("cluster_cccccccccccccccccccc", "file-a", 1_650_000, 1_890_000, 2),
    ]

    dossier = build_scene_dossiers(
        selections=[anchored(clusters[1].cluster_id)], clusters=clusters
    )[0]

    assert dossier.expanded_cluster_ids == (
        clusters[1].cluster_id,
        clusters[2].cluster_id,
    )
    assert dossier.start_ms == 700_000
    assert dossier.end_ms == 1_890_000


def test_dossier_enforces_six_hundred_segment_cap() -> None:
    clusters = [
        cluster("cluster_aaaaaaaaaaaaaaaaaaaa", "file-a", 0, 300_000, 250),
        cluster("cluster_bbbbbbbbbbbbbbbbbbbb", "file-a", 350_000, 650_000, 300),
        cluster("cluster_cccccccccccccccccccc", "file-a", 660_000, 900_000, 100),
    ]

    dossier = build_scene_dossiers(
        selections=[anchored(clusters[1].cluster_id)], clusters=clusters
    )[0]

    assert dossier.expanded_cluster_ids == (
        clusters[1].cluster_id,
        clusters[2].cluster_id,
    )
    assert len(dossier.allowed_segment_ids) == 400


@pytest.mark.parametrize(
    "core",
    [
        cluster("cluster_aaaaaaaaaaaaaaaaaaaa", "file-a", 0, 1_800_001, 2),
        cluster("cluster_bbbbbbbbbbbbbbbbbbbb", "file-a", 0, 1_000, 601),
    ],
)
def test_dossier_rejects_core_that_exceeds_expansion_limits(
    core: TranscriptCluster,
) -> None:
    with pytest.raises(DossierBuildError, match="core"):
        build_scene_dossiers(
            selections=[anchored(core.cluster_id, before=0, after=0)],
            clusters=[core],
        )


def test_dossier_id_is_stable_for_the_same_selection_and_expansion() -> None:
    clusters = [
        cluster("cluster_aaaaaaaaaaaaaaaaaaaa", "file-a", 0, 300_000, 2),
        cluster("cluster_bbbbbbbbbbbbbbbbbbbb", "file-a", 350_000, 650_000, 2),
    ]

    first = build_scene_dossiers(
        selections=[anchored(clusters[1].cluster_id)], clusters=clusters
    )[0]
    second = build_scene_dossiers(
        selections=[anchored(clusters[1].cluster_id)], clusters=clusters
    )[0]

    assert first.dossier_id == second.dossier_id
    assert first.dossier_id.startswith("dossier_")
