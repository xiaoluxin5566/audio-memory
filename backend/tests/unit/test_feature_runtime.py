from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from feature_runtime import (  # noqa: E402
    FeatureRuntimeError,
    RuntimeOwner,
    RuntimePlan,
    RuntimeStore,
    stop_runtime,
)


def test_runtime_plan_uses_controller_tools_and_feature_sources() -> None:
    plan = RuntimePlan.build(
        controller_root=REPOSITORY_ROOT,
        feature_root=REPOSITORY_ROOT,
        home=Path.home(),
        feature_id="beta3-stability",
    )

    assert plan.backend_argv[0] == str(
        (REPOSITORY_ROOT / "backend/.venv/bin/python").resolve()
    )
    assert plan.backend_argv[1:3] == (
        str(REPOSITORY_ROOT / "scripts/dev_lifecycle.py"),
        "start",
    )
    assert plan.backend_environment["AUDIO_MEMORY_DEV_PYTHON"] == str(
        (REPOSITORY_ROOT / "backend/.venv/bin/python").resolve()
    )
    assert plan.frontend_argv == (
        str((REPOSITORY_ROOT / "prototype/node_modules/.bin/vite").resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        "5173",
        "--strictPort",
    )
    assert plan.frontend_cwd == REPOSITORY_ROOT / "prototype"
    assert plan.frontend_environment["AUDIO_MEMORY_BACKEND_URL"] == (
        "http://127.0.0.1:8766"
    )
    assert plan.frontend_environment["AUDIO_MEMORY_EXPECTED_PROFILE"] == (
        "development"
    )


def test_runtime_plan_rejects_feature_root_without_expected_sources(
    tmp_path: Path,
) -> None:
    with pytest.raises(FeatureRuntimeError, match="功能 worktree"):
        RuntimePlan.build(
            controller_root=REPOSITORY_ROOT,
            feature_root=tmp_path,
            home=Path.home(),
            feature_id="missing",
        )


def test_runtime_store_round_trip_and_rejects_other_feature_owner(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path)
    owner = RuntimeOwner(
        schema_version=1,
        feature_id="one",
        worktree="/tmp/one",
        supervisor_pid=123,
        supervisor_started_at="Tue Aug 19 11:00:00 2026",
        supervisor_argv=("/python", "/feature_runtime.py", "start"),
        backend_port=8766,
        frontend_port=5173,
        phase="ready",
    )
    store.save(owner)

    assert store.load() == owner
    with pytest.raises(FeatureRuntimeError, match="one.*two"):
        store.require_available_for("two", owner_is_live=lambda _: True)


def test_runtime_store_rejects_unknown_owner_fields(tmp_path: Path) -> None:
    runtime_root = tmp_path / "audio-memory-governance" / "runtime"
    runtime_root.mkdir(parents=True)
    (runtime_root / "development-owner.json").write_text(
        json.dumps({"schema_version": 1, "unexpected": True}),
        encoding="utf-8",
    )

    with pytest.raises(FeatureRuntimeError, match="运行时所有者记录"):
        RuntimeStore(tmp_path).load()


def test_stop_cannot_terminate_another_feature_owner(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path)
    store.save(RuntimeOwner(
        schema_version=1,
        feature_id="one",
        worktree="/tmp/one",
        supervisor_pid=123,
        supervisor_started_at="Tue Aug 19 11:00:00 2026",
        supervisor_argv=("/python", "/feature_runtime.py", "start"),
        backend_port=8766,
        frontend_port=5173,
        phase="ready",
    ))

    with pytest.raises(FeatureRuntimeError, match="one.*two.*无权"):
        stop_runtime("two", store)

    assert store.load() is not None
