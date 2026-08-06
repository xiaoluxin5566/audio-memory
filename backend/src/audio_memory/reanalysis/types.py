from __future__ import annotations

from dataclasses import asdict, dataclass


BATCH_STATES = frozenset(
    {
        "pending",
        "running",
        "paused",
        "stopping",
        "completed",
        "completed_with_failures",
        "content_completed_profile_failed",
        "stopped",
    }
)
ITEM_STATES = frozenset({"pending", "running", "succeeded", "failed", "stopped"})


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    batch_id: str
    job_id: str
    audio_file_count: int
    transcript_character_count: int
    transcript_sha256: str


@dataclass(frozen=True, slots=True)
class PromptSummary:
    version: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReanalysisSnapshot:
    sources: tuple[SourceSnapshot, ...]
    provider_id: str
    provider_display_name: str
    model_id: str
    credential_generation: int
    prompt_snapshot: dict[str, dict[str, object]]
    prompt_hashes: dict[str, str]
    fixed_rule_hashes: dict[str, str]
    fixed_rules_hash: str
    profile_snapshot: tuple[dict[str, object], ...]
    profile_hash: str
    source_batch_count: int
    audio_file_count: int
    transcript_character_count: int
    estimated_calls_min: int
    estimated_calls_max: int
    scope: str = "all_completed_history"

    def canonical_payload(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "sources": [asdict(item) for item in self.sources],
            "source_batch_ids": [item.batch_id for item in self.sources],
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "credential_generation": self.credential_generation,
            "prompt_hashes": self.prompt_hashes,
            "fixed_rule_hashes": self.fixed_rule_hashes,
            "fixed_rules_hash": self.fixed_rules_hash,
            "profile_hash": self.profile_hash,
            "counts": {
                "source_batches": self.source_batch_count,
                "audio_files": self.audio_file_count,
                "transcript_characters": self.transcript_character_count,
                "estimated_calls_min": self.estimated_calls_min,
                "estimated_calls_max": self.estimated_calls_max,
            },
        }


@dataclass(frozen=True, slots=True)
class ReanalysisPreview:
    source_batch_count: int
    audio_file_count: int
    transcript_character_count: int
    provider_id: str
    provider_display_name: str
    model_id: str
    credential_generation: int
    prompt_summary: dict[str, PromptSummary]
    estimated_calls_min: int
    estimated_calls_max: int
    whisper_calls: int
    diarization_calls: int
    blockers: list[str]
    preview_token: str
    snapshot_hash: str
    expires_at: str
    snapshot: ReanalysisSnapshot


@dataclass(frozen=True, slots=True)
class ReanalysisItemView:
    id: str
    source_batch_id: str
    position: int
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ReanalysisBatchView:
    id: str
    status: str
    provider_id: str
    model_id: str
    credential_generation: int
    snapshot_hash: str
    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    stopped: int
    current_item_id: str | None
    items: tuple[ReanalysisItemView, ...]
    created_at: str
    updated_at: str
    completed_at: str | None
