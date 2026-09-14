from __future__ import annotations

from hashlib import sha256
import json

from pydantic import BaseModel, ConfigDict, Field, JsonValue


BETA8_STAGE_KEYS = (
    "beta8_event_index",
    "beta8_all_scenes_v1",
    "beta8_initial_audit_units",
    "beta8_initial_audit_aggregate",
    "beta8_orchestration",
    "beta8_search_packets",
    "beta8_revised_cards",
    "beta8_final_candidate",
    "beta8_final_audit_units",
    "beta8_final_audit_aggregate",
    "beta8_publication_ready",
)
BETA8_SCHEMA_VERSION = 1


class CheckpointNotReusableError(ValueError):
    """A valid checkpoint belongs to different compatibility inputs."""


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return sha256(encoded).hexdigest()


class Beta8StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=BETA8_SCHEMA_VERSION, ge=1)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_generation: int = Field(ge=0)
    upstream_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: JsonValue

    @classmethod
    def create(
        cls,
        *,
        payload: JsonValue,
        prompt_hash: str,
        transcript_fingerprint: str,
        provider_generation: int,
        upstream_artifact_hash: str,
    ) -> "Beta8StageRecord":
        return cls(
            prompt_hash=prompt_hash,
            transcript_fingerprint=transcript_fingerprint,
            provider_generation=provider_generation,
            upstream_artifact_hash=upstream_artifact_hash,
            artifact_hash=canonical_hash(payload),
            payload=payload,
        )


def validate_stage_record(
    record: Beta8StageRecord,
    *,
    prompt_hash: str,
    transcript_fingerprint: str,
    provider_generation: int,
    upstream_artifact_hash: str,
    schema_version: int = BETA8_SCHEMA_VERSION,
) -> None:
    if record.artifact_hash != canonical_hash(record.payload):
        raise ValueError("artifact_hash changed; checkpoint payload is corrupt")
    expected = {
        "schema_version": schema_version,
        "prompt_hash": prompt_hash,
        "transcript_fingerprint": transcript_fingerprint,
        "provider_generation": provider_generation,
        "upstream_artifact_hash": upstream_artifact_hash,
    }
    for field, value in expected.items():
        if getattr(record, field) != value:
            raise CheckpointNotReusableError(
                f"{field} changed; checkpoint is not reusable"
            )
