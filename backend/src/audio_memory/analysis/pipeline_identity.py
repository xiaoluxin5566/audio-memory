from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from audio_memory.prompts.beta8_composer import Beta8PromptComposer
from audio_memory.prompts.beta8_writing_composer import WritingPrompts
from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts
from audio_memory.prompts.composer import PromptComposer


SINGLE_REPORT_PIPELINE_KIND = "single_report_v1"
BETA8_LEGACY_PIPELINE_KIND = "beta8_multi_scene_v1"
BETA8_INDEXED_PIPELINE_KIND = "beta8_indexed_scene_v2"
BETA8_WRITING_PIPELINE_KIND = "beta8_writing_v1"
BETA8_P1_P5_PIPELINE_KIND = "beta8_p1_p5_v1"
BETA8_PIPELINE_KINDS = frozenset({
    BETA8_LEGACY_PIPELINE_KIND,
    BETA8_INDEXED_PIPELINE_KIND,
    BETA8_WRITING_PIPELINE_KIND,
    BETA8_P1_P5_PIPELINE_KIND,
})
KNOWN_PIPELINE_KINDS = frozenset({SINGLE_REPORT_PIPELINE_KIND}) | BETA8_PIPELINE_KINDS
_MODERN_FIELDS = frozenset({
    "pipeline_kind",
    "provider_id",
    "model_id",
    "search_provider_id",
    "search_model_id",
    "credential_generation",
    "fixed_rules_hash",
    "prompt_manifest",
})
_LEGACY_SINGLE_FIELDS = frozenset({
    "provider_id",
    "model_id",
    "credential_generation",
    "fixed_rules_hash",
    "prompt_manifest",
})
_LEGACY_BETA8_NO_FINGERPRINT_FIELDS = frozenset({
    "pipeline_kind",
    "search_provider_id",
    "search_model_id",
})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InvalidPipelineIdentityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PipelineIdentity:
    pipeline_kind: str
    search_provider_id: str | None
    search_model_id: str | None
    fixed_rules_hash: str
    prompt_manifest: tuple[dict[str, Any], ...]
    is_legacy: bool = False


def prompt_binding(pipeline_kind: str) -> tuple[str, list[dict[str, Any]]]:
    if pipeline_kind == BETA8_P1_P5_PIPELINE_KIND:
        return P1P5Prompts.fixed_rules_hash(), P1P5Prompts.manifest()
    if pipeline_kind == BETA8_WRITING_PIPELINE_KIND:
        return WritingPrompts.fixed_rules_hash(), WritingPrompts.manifest()
    if pipeline_kind == SINGLE_REPORT_PIPELINE_KIND:
        return PromptComposer.fixed_rules_hash(), [
            {
                "role": item["role"],
                "files": list(item["files"]),
                "sha256": item["sha256"],
            }
            for item in PromptComposer.final_report_prompt_manifest()
        ]
    if pipeline_kind in BETA8_PIPELINE_KINDS:
        return Beta8PromptComposer.fixed_rules_hash(), [
            {"prompt_id": item["prompt_id"], "sha256": item["sha256"]}
            for item in Beta8PromptComposer.prompt_manifest()
        ]
    raise InvalidPipelineIdentityError(
        f"Unknown report pipeline: {pipeline_kind}"
    )


def canonical_parameters_json(parameters: dict[str, Any]) -> str:
    return json.dumps(
        parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parameters_fingerprint(parameters: dict[str, Any]) -> str:
    return sha256(canonical_parameters_json(parameters).encode("utf-8")).hexdigest()


def verified_pipeline_identity_hash(
    version, identity: PipelineIdentity | None = None
) -> str:
    """Hash a successfully decoded stored pipeline identity."""
    verified = identity or validate_stored_pipeline_identity(version)
    payload = {
        "pipeline_kind": verified.pipeline_kind,
        "search_provider_id": verified.search_provider_id,
        "search_model_id": verified.search_model_id,
        "fixed_rules_hash": verified.fixed_rules_hash,
        "prompt_manifest": list(verified.prompt_manifest),
        "is_legacy": verified.is_legacy,
        "parameters_fingerprint": version.pipeline_parameters_fingerprint,
    }
    return parameters_fingerprint(payload)


def build_pipeline_parameters(
    *,
    pipeline_kind: str,
    provider_id: str,
    model_id: str,
    credential_generation: int,
    search_provider_id: str | None,
    search_model_id: str | None,
) -> tuple[dict[str, Any], str, str]:
    _validate_search_binding(search_provider_id, search_model_id)
    fixed_rules_hash, prompt_manifest = prompt_binding(pipeline_kind)
    parameters = {
        "pipeline_kind": pipeline_kind,
        "provider_id": provider_id,
        "model_id": model_id,
        "search_provider_id": search_provider_id,
        "search_model_id": search_model_id,
        "credential_generation": credential_generation,
        "fixed_rules_hash": fixed_rules_hash,
        "prompt_manifest": prompt_manifest,
    }
    canonical = canonical_parameters_json(parameters)
    return parameters, canonical, sha256(canonical.encode("utf-8")).hexdigest()


def _optional_string(parameters: dict[str, Any], field: str) -> str | None:
    value = parameters.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidPipelineIdentityError(
            f"Analysis version {field} must be null or a non-empty string"
        )
    return value


def _validate_search_binding(
    provider_id: str | None,
    model_id: str | None,
) -> None:
    for field, value in (
        ("search_provider_id", provider_id),
        ("search_model_id", model_id),
    ):
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise InvalidPipelineIdentityError(
                f"Analysis version {field} must be null or a non-empty string"
            )
    if (provider_id is None) != (model_id is None):
        raise InvalidPipelineIdentityError(
            "Search provider and model must be configured together"
        )


def _stored_manifest(
    parameters: dict[str, Any], pipeline_kind: str
) -> tuple[dict[str, Any], ...]:
    value = parameters.get("prompt_manifest")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise InvalidPipelineIdentityError(
            "Analysis version prompt manifest must be a list of objects"
        )
    if not value:
        raise InvalidPipelineIdentityError(
            "Analysis version prompt manifest must not be empty"
        )
    for item in value:
        expected_fields = (
            {"role", "files", "sha256"}
            if pipeline_kind == SINGLE_REPORT_PIPELINE_KIND
            else {"prompt_id", "sha256"}
        )
        if set(item) != expected_fields:
            raise InvalidPipelineIdentityError(
                "Analysis version prompt manifest has invalid fields"
            )
        identifier = item.get(
            "role" if pipeline_kind == SINGLE_REPORT_PIPELINE_KIND else "prompt_id"
        )
        if not isinstance(identifier, str) or not identifier.strip():
            raise InvalidPipelineIdentityError(
                "Analysis version prompt manifest identifier must not be empty"
            )
        digest = item.get("sha256")
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise InvalidPipelineIdentityError(
                "Analysis version prompt manifest sha256 must be a lowercase digest"
            )
        if pipeline_kind == SINGLE_REPORT_PIPELINE_KIND:
            files = item.get("files")
            if (
                not isinstance(files, list)
                or (not files and identifier != "report-schemas")
                or any(
                    not isinstance(filename, str) or not filename.strip()
                    for filename in files
                )
            ):
                raise InvalidPipelineIdentityError(
                    "Analysis version prompt manifest files must be non-empty strings"
                )
    return tuple(dict(item) for item in value)


def _validate_column_coherence(version, parameters: dict[str, Any]) -> None:
    coherence = {
        "provider_id": version.provider_id,
        "model_id": version.model_id,
        "credential_generation": version.credential_generation,
        "fixed_rules_hash": version.fixed_rules_hash,
    }
    for field, expected in coherence.items():
        if parameters[field] != expected:
            raise InvalidPipelineIdentityError(
                f"Analysis version pipeline {field} does not match its column"
            )


def validate_stored_pipeline_identity(version) -> PipelineIdentity:
    """Decode stored identity integrity without consulting current Prompt files."""
    try:
        parameters = json.loads(version.pipeline_parameters_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidPipelineIdentityError(
            "Analysis version pipeline parameters are invalid"
        ) from exc
    if not isinstance(parameters, dict):
        raise InvalidPipelineIdentityError(
            "Analysis version pipeline parameters must be an object"
        )
    pipeline_kind = parameters.get(
        "pipeline_kind", SINGLE_REPORT_PIPELINE_KIND
    )
    if not isinstance(pipeline_kind, str) or not pipeline_kind:
        raise InvalidPipelineIdentityError(
            "Analysis version pipeline_kind must be a non-empty string"
        )
    if pipeline_kind not in KNOWN_PIPELINE_KINDS:
        raise InvalidPipelineIdentityError(
            f"Unknown report pipeline: {pipeline_kind}"
        )
    fingerprint = version.pipeline_parameters_fingerprint
    if not fingerprint:
        if "pipeline_kind" not in parameters:
            if parameters:
                raise InvalidPipelineIdentityError(
                    "Fingerprintless legacy single-report parameters must be empty"
                )
            return PipelineIdentity(
                pipeline_kind=SINGLE_REPORT_PIPELINE_KIND,
                search_provider_id=None,
                search_model_id=None,
                fixed_rules_hash=version.fixed_rules_hash,
                prompt_manifest=tuple(),
                is_legacy=True,
            )
        if pipeline_kind == BETA8_LEGACY_PIPELINE_KIND:
            if not set(parameters).issubset(_LEGACY_BETA8_NO_FINGERPRINT_FIELDS):
                raise InvalidPipelineIdentityError(
                    "Historical Beta 8 parameters have an invalid field set"
                )
            search_provider_id = _optional_string(parameters, "search_provider_id")
            search_model_id = _optional_string(parameters, "search_model_id")
            _validate_search_binding(search_provider_id, search_model_id)
            return PipelineIdentity(
                pipeline_kind=pipeline_kind,
                search_provider_id=search_provider_id,
                search_model_id=search_model_id,
                fixed_rules_hash=version.fixed_rules_hash,
                prompt_manifest=tuple(),
                is_legacy=True,
            )
        raise InvalidPipelineIdentityError(
            "Analysis version with explicit pipeline identity requires a "
            "pipeline fingerprint"
        )
    actual_fingerprint = parameters_fingerprint(parameters)
    if fingerprint != actual_fingerprint:
        raise InvalidPipelineIdentityError(
            "Analysis version pipeline fingerprint does not match parameters"
        )
    fields = frozenset(parameters)
    if fields == _LEGACY_SINGLE_FIELDS:
        _validate_column_coherence(version, parameters)
        return PipelineIdentity(
            pipeline_kind=SINGLE_REPORT_PIPELINE_KIND,
            search_provider_id=None,
            search_model_id=None,
            fixed_rules_hash=parameters["fixed_rules_hash"],
            prompt_manifest=_stored_manifest(parameters, SINGLE_REPORT_PIPELINE_KIND),
            is_legacy=True,
        )
    if fields != _MODERN_FIELDS:
        raise InvalidPipelineIdentityError(
            "Analysis version pipeline parameters have an invalid field set"
        )
    _validate_column_coherence(version, parameters)
    search_provider_id = _optional_string(parameters, "search_provider_id")
    search_model_id = _optional_string(parameters, "search_model_id")
    _validate_search_binding(search_provider_id, search_model_id)
    return PipelineIdentity(
        pipeline_kind=pipeline_kind,
        search_provider_id=search_provider_id,
        search_model_id=search_model_id,
        fixed_rules_hash=parameters["fixed_rules_hash"],
        prompt_manifest=_stored_manifest(parameters, pipeline_kind),
    )


def is_current_pipeline_compatible(identity: PipelineIdentity) -> bool:
    """Compare a valid stored identity with the current runtime Prompt binding."""
    current_hash, current_manifest = prompt_binding(identity.pipeline_kind)
    if identity.fixed_rules_hash != current_hash:
        return False
    if not identity.prompt_manifest:
        return identity.is_legacy
    return list(identity.prompt_manifest) == current_manifest


def validate_version_pipeline_identity(version) -> PipelineIdentity:
    """Backward-compatible name for stored identity validation."""
    return validate_stored_pipeline_identity(version)
