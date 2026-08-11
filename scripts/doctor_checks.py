#!/usr/bin/env python3
"""Semantic, read-only release checks used by doctor.sh."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

WHISPER_MODEL_ID = "mlx-community/whisper-large-v3-turbo"
DIARIZATION_PATHS = {
    "models/diarization/silero_vad.onnx",
    "models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx",
    "models/diarization/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
}
DIARIZATION_HASHES = {
    "models/diarization/silero_vad.onnx": {
        "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
    },
    "models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx": {
        "10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e",
    },
    "models/diarization/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx": {
        "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b",
    },
}
DIARIZATION_SIZES = {
    "10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e": 1540514,
}
EXPECTED_REVISIONS = [f"{number:04d}" for number in range(1, 12)]


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    return value


def _valid_file(path: Path, item: dict[str, Any]) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    size, digest = item.get("size"), item.get("sha256")
    if not (
        isinstance(size, int)
        and size > 0
        and path.stat().st_size == size
        and isinstance(digest, str)
        and len(digest) == 64
    ):
        return False
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == digest


def check_whisper(app_data: Path) -> bool:
    try:
        manifest = _json(app_data / "whisper-model-manifest.json")
        snapshot = Path(manifest["snapshot"]).resolve()
        files = manifest["files"]
        if manifest.get("model_id") != WHISPER_MODEL_ID or not isinstance(files, list) or not files:
            return False
        relative_paths = {
            item.get("path") for item in files if isinstance(item, dict)
        }
        if "config.json" not in relative_paths or not any(
            isinstance(path, str) and path.endswith(".safetensors")
            for path in relative_paths
        ):
            return False
        config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("model_type") != "whisper":
            return False
        blob_root = snapshot.parent.parent / "blobs"
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return False
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                return False
            path = snapshot / relative
            resolved = path.resolve()
            allowed = (
                resolved == snapshot
                or snapshot in resolved.parents
                or resolved == blob_root
                or blob_root in resolved.parents
            )
            if not allowed or not _valid_file(path, item):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def check_diarization(app_data: Path) -> bool:
    try:
        files = _json(app_data / "diarization-model-manifest.json")["files"]
        if not isinstance(files, list):
            return False
        indexed = {item.get("path"): item for item in files if isinstance(item, dict)}
        if set(indexed) != DIARIZATION_PATHS:
            return False
        for relative, item in indexed.items():
            digest = item.get("expected_sha256")
            if (
                digest != item.get("sha256")
                or digest not in DIARIZATION_HASHES[relative]
                or not _valid_file(app_data / relative, item)
            ):
                return False
            trusted_size = DIARIZATION_SIZES.get(digest)
            if item.get("expected_size") != trusted_size:
                return False
            if trusted_size is not None and item.get("size") != trusted_size:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _assignment(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return ast.literal_eval(node.value)
    raise ValueError(name)


def check_migrations(versions: Path) -> bool:
    try:
        chain: dict[str, str | None] = {}
        for path in versions.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            revision, down = _assignment(tree, "revision"), _assignment(tree, "down_revision")
            if not isinstance(revision, str) or revision in chain:
                return False
            if down is not None and not isinstance(down, str):
                return False
            chain[revision] = down
        return set(chain) == set(EXPECTED_REVISIONS) and all(
            chain[revision] == (None if index == 0 else EXPECTED_REVISIONS[index - 1])
            for index, revision in enumerate(EXPECTED_REVISIONS)
        )
    except (OSError, SyntaxError, ValueError):
        return False


def _connect(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{database}?mode=ro", uri=True)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def check_database(database: Path) -> bool:
    required = {
        "alembic_version": {"version_num"},
        "reanalysis_batches": {"id", "status"},
        "analysis_versions": {"id", "status", "worker_owner_id", "lease_expires_at"},
        "reanalysis_items": {"analysis_version_id", "status", "completed_at"},
    }
    try:
        with _connect(database) as connection:
            revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            return revisions == [("0011",)] and all(
                columns.issubset(_columns(connection, table))
                for table, columns in required.items()
            )
    except sqlite3.Error:
        return False


def check_recovery(database: Path) -> bool:
    queries = (
        "SELECT count(*) FROM reanalysis_batches WHERE status LIKE 'paused_%'",
        """SELECT count(*) FROM reanalysis_items item LEFT JOIN analysis_versions version
             ON version.id = item.analysis_version_id WHERE item.status = 'running'
             AND (version.id IS NULL OR version.status != 'running')""",
        """SELECT count(*) FROM analysis_versions WHERE status = 'running'
             AND (coalesce(worker_owner_id, '') = '' OR coalesce(lease_expires_at, '') = '')""",
        """SELECT count(*) FROM reanalysis_items
             WHERE (status = 'completed' AND completed_at IS NULL)
                OR (status IN ('pending', 'running') AND completed_at IS NOT NULL)""",
    )
    try:
        with _connect(database) as connection:
            return all(connection.execute(query).fetchone()[0] == 0 for query in queries)
    except sqlite3.Error:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("whisper", "diarization", "migrations", "database", "recovery"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    check = {"whisper": check_whisper, "diarization": check_diarization, "migrations": check_migrations, "database": check_database, "recovery": check_recovery}[args.check]
    return 0 if check(args.path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
