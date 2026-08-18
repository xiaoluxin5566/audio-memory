from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from audio_memory.uploads.cleanup import assert_staging_path


def physical_checkpoint_fingerprint(
    *,
    audio_fingerprint: str,
    model_id: str,
    parameters_fingerprint: str,
    batch_index: int,
) -> str:
    payload = {
        "audio_fingerprint": audio_fingerprint,
        "model_id": model_id,
        "parameters_fingerprint": parameters_fingerprint,
        "batch_index": batch_index,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def save_physical_chunk_checkpoint(
    path: Path,
    *,
    staging_root: Path,
    fingerprint: str,
    part_index: int,
    segments: list[dict[str, object]],
    language: str | None,
    language_confidence: float | None,
) -> None:
    safe_path = assert_staging_path(path, staging_root)
    safe_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "fingerprint": fingerprint,
        "part_index": part_index,
        "segments": segments,
        "language": language,
        "language_confidence": language_confidence,
    }
    temporary = assert_staging_path(
        safe_path.with_suffix(safe_path.suffix + ".tmp"), staging_root
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(safe_path)


def load_physical_chunk_checkpoint(
    path: Path,
    *,
    staging_root: Path,
    expected_fingerprint: str,
    expected_part_index: int,
) -> dict[str, object] | None:
    safe_path = assert_staging_path(path, staging_root)
    try:
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("version") != 1
        or payload.get("fingerprint") != expected_fingerprint
        or payload.get("part_index") != expected_part_index
        or not isinstance(payload.get("segments"), list)
    ):
        return None
    language = payload.get("language")
    confidence = payload.get("language_confidence")
    if language is not None and not isinstance(language, str):
        return None
    if confidence is not None and not isinstance(confidence, (int, float)):
        return None
    return {
        "segments": payload["segments"],
        "language": language,
        "language_confidence": confidence,
    }
