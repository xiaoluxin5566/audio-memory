from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from feature_governance import (  # noqa: E402
    FeatureRecord,
    FeatureStore,
    GovernanceError,
)


@pytest.mark.parametrize(
    "value",
    ["", "../main", "A B", "feature/child", "-leading", "trailing-"],
)
def test_feature_id_rejects_path_and_branch_injection(value: str) -> None:
    with pytest.raises(GovernanceError, match="功能标识符"):
        FeatureRecord.new(value, base_commit="a" * 40)


def test_feature_store_round_trip_preserves_only_workflow_state(
    tmp_path: Path,
) -> None:
    store = FeatureStore(tmp_path)
    record = replace(
        FeatureRecord.new("report-progress", base_commit="a" * 40),
        current_step="真实开发环境验收",
    )

    store.save(record)

    assert store.load("report-progress") == record
    payload = json.loads(
        (tmp_path / "audio-memory-governance/features/report-progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload == {
        "schema_version": 1,
        "feature_id": "report-progress",
        "branch": "codex/report-progress",
        "base_branch": "main",
        "target_version": "v0.1.0-beta.3",
        "status": "in_progress",
        "worktree": ".worktrees/report-progress",
        "base_commit": "a" * 40,
        "head_commit": "a" * 40,
        "current_step": "真实开发环境验收",
        "required_checks": [
            "backend",
            "frontend",
            "browser",
            "runtime-isolation",
        ],
        "passed_checks": [],
        "merge_approved": False,
    }


def test_store_rejects_symlinked_features_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "audio-memory-governance").mkdir()
    (tmp_path / "audio-memory-governance" / "features").symlink_to(outside)

    with pytest.raises(GovernanceError, match="符号链接"):
        FeatureStore(tmp_path).save(
            FeatureRecord.new("search", base_commit="a" * 40)
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        json.dumps(
            {
                "schema_version": 2,
                "feature_id": "search",
                "branch": "codex/search",
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "feature_id": "search",
                "branch": "codex/search",
                "unexpected": "field",
            }
        ),
    ],
)
def test_store_rejects_corrupt_or_unknown_state(
    tmp_path: Path, payload: str
) -> None:
    state_root = tmp_path / "audio-memory-governance" / "features"
    state_root.mkdir(parents=True)
    (state_root / "search.json").write_text(payload, encoding="utf-8")

    with pytest.raises(GovernanceError, match="状态文件"):
        FeatureStore(tmp_path).load("search")


def test_store_rejects_hardlinked_state_file(tmp_path: Path) -> None:
    state_root = tmp_path / "audio-memory-governance" / "features"
    state_root.mkdir(parents=True)
    source = tmp_path / "outside.json"
    source.write_text("{}", encoding="utf-8")
    (state_root / "search.json").hardlink_to(source)

    with pytest.raises(GovernanceError, match="硬链接"):
        FeatureStore(tmp_path).load("search")
