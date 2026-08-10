from __future__ import annotations

import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_memory.analysis.clusters import TranscriptCluster
from audio_memory.analysis.director import AnchoredSelection
from audio_memory.prompts.director_schema import Priority
from audio_memory.prompts.event_schema import SceneId


DOSSIER_MAX_SPAN_MS = 1_800_000
DOSSIER_MAX_SEGMENTS = 600


class DossierBuildError(ValueError):
    pass


class SceneDossier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dossier_id: str = Field(pattern=r"^dossier_[A-Za-z0-9_]+$")
    primary_event_id: str = Field(pattern=r"^event_[A-Za-z0-9_]+$")
    source_event_ids: tuple[str, ...]
    candidate_scenes: tuple[SceneId, ...]
    selected_cluster_ids: tuple[str, ...]
    expanded_cluster_ids: tuple[str, ...]
    allowed_segment_ids: tuple[str, ...]
    file_ids: tuple[str, ...]
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=160)
    selection_reason: str = Field(min_length=1, max_length=1_000)
    priority: Priority

    @model_validator(mode="after")
    def validate_scope(self) -> SceneDossier:
        if self.end_ms <= self.start_ms:
            raise ValueError("dossier end_ms must be greater than start_ms")
        for label, values in (
            ("source_event_ids", self.source_event_ids),
            ("candidate_scenes", self.candidate_scenes),
            ("selected_cluster_ids", self.selected_cluster_ids),
            ("expanded_cluster_ids", self.expanded_cluster_ids),
            ("allowed_segment_ids", self.allowed_segment_ids),
            ("file_ids", self.file_ids),
        ):
            if not values:
                raise ValueError(f"{label} must not be empty")
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if not set(self.selected_cluster_ids).issubset(self.expanded_cluster_ids):
            raise ValueError("selected clusters must remain in expanded scope")
        return self


def build_scene_dossiers(
    *,
    selections: list[AnchoredSelection],
    clusters: list[TranscriptCluster],
) -> list[SceneDossier]:
    cluster_indexes: dict[str, int] = {}
    for index, cluster in enumerate(clusters):
        if cluster.cluster_id in cluster_indexes:
            raise DossierBuildError("cluster IDs must be unique")
        cluster_indexes[cluster.cluster_id] = index

    dossiers: list[SceneDossier] = []
    for anchored in selections:
        try:
            selected_indexes = sorted(
                cluster_indexes[cluster_id]
                for cluster_id in anchored.selection.cluster_ids
            )
        except KeyError as exc:
            raise DossierBuildError("dossier references unknown cluster") from exc
        if selected_indexes != list(
            range(selected_indexes[0], selected_indexes[-1] + 1)
        ):
            raise DossierBuildError("selected clusters must be contiguous")
        selected_clusters = [clusters[index] for index in selected_indexes]
        if len({cluster.file_id for cluster in selected_clusters}) != 1:
            raise DossierBuildError("selected clusters cannot cross files")
        core_count = sum(len(cluster.segments) for cluster in selected_clusters)
        core_start = min(cluster.start_ms for cluster in selected_clusters)
        core_end = max(cluster.end_ms for cluster in selected_clusters)
        if (
            core_end - core_start > DOSSIER_MAX_SPAN_MS
            or core_count > DOSSIER_MAX_SEGMENTS
        ):
            raise DossierBuildError("selected core exceeds dossier limits")

        selected_file_id = selected_clusters[0].file_id
        candidates: list[tuple[int, int]] = []
        before_index = selected_indexes[0] - 1
        if (
            anchored.selection.context_before_clusters == 1
            and before_index >= 0
            and clusters[before_index].file_id == selected_file_id
        ):
            distance = max(0, core_start - clusters[before_index].end_ms)
            candidates.append((distance, before_index))
        after_index = selected_indexes[-1] + 1
        if (
            anchored.selection.context_after_clusters == 1
            and after_index < len(clusters)
            and clusters[after_index].file_id == selected_file_id
        ):
            distance = max(0, clusters[after_index].start_ms - core_end)
            candidates.append((distance, after_index))

        expanded_indexes = set(selected_indexes)
        for _, candidate_index in sorted(candidates, key=lambda item: item):
            proposed = sorted([*expanded_indexes, candidate_index])
            proposed_clusters = [clusters[index] for index in proposed]
            proposed_start = min(cluster.start_ms for cluster in proposed_clusters)
            proposed_end = max(cluster.end_ms for cluster in proposed_clusters)
            proposed_count = sum(
                len(cluster.segments) for cluster in proposed_clusters
            )
            if (
                proposed_end - proposed_start <= DOSSIER_MAX_SPAN_MS
                and proposed_count <= DOSSIER_MAX_SEGMENTS
            ):
                expanded_indexes.add(candidate_index)

        ordered_indexes = sorted(expanded_indexes)
        expanded_clusters = [clusters[index] for index in ordered_indexes]
        allowed_segment_ids = tuple(
            str(item["segment_id"])
            for cluster in expanded_clusters
            for item in cluster.segments
        )
        expanded_cluster_ids = tuple(
            cluster.cluster_id for cluster in expanded_clusters
        )
        dossier_identity = {
            "selection_id": anchored.selection.selection_id,
            "primary_event_id": anchored.primary_event_id,
            "source_event_ids": anchored.source_event_ids,
            "candidate_scenes": sorted(anchored.selection.candidate_scenes),
            "selected_cluster_ids": anchored.selection.cluster_ids,
            "expanded_cluster_ids": expanded_cluster_ids,
        }
        dossiers.append(
            SceneDossier(
                dossier_id=f"dossier_{_digest(dossier_identity)}",
                primary_event_id=anchored.primary_event_id,
                source_event_ids=anchored.source_event_ids,
                candidate_scenes=tuple(anchored.selection.candidate_scenes),
                selected_cluster_ids=tuple(anchored.selection.cluster_ids),
                expanded_cluster_ids=expanded_cluster_ids,
                allowed_segment_ids=allowed_segment_ids,
                file_ids=(selected_file_id,),
                start_ms=min(cluster.start_ms for cluster in expanded_clusters),
                end_ms=max(cluster.end_ms for cluster in expanded_clusters),
                title=anchored.selection.title,
                selection_reason=anchored.selection.selection_reason,
                priority=anchored.selection.priority,
            )
        )
    return dossiers


def dossiers_for_scene(
    dossiers: list[SceneDossier], scene_id: str
) -> list[SceneDossier]:
    return [
        dossier for dossier in dossiers if scene_id in dossier.candidate_scenes
    ]


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()[:20]
