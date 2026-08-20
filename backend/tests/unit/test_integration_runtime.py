from __future__ import annotations

from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from feature_runtime import RuntimeOwner, RuntimeStore  # noqa: E402
from integration_runtime import (  # noqa: E402
    AcceptanceError,
    AcceptanceRecord,
    AcceptanceStore,
    assert_safe_main,
    build_acceptance_plan,
    discover_main_worktree,
    handoff_existing_runtime,
    stop_acceptance,
    validate_version,
)


def owner() -> RuntimeOwner:
    return RuntimeOwner(
        schema_version=1,
        feature_id="beta3-stability",
        worktree="/tmp/beta3-stability",
        supervisor_pid=123,
        supervisor_started_at="Tue Aug 20 11:00:00 2026",
        supervisor_argv=("/python", "/feature_runtime.py", "start"),
        backend_port=8766,
        frontend_port=5173,
        phase="ready",
    )


def test_acceptance_store_round_trips_version_commit_and_main_worktree(
    tmp_path: Path,
) -> None:
    store = AcceptanceStore(tmp_path)
    record = AcceptanceRecord(
        schema_version=1,
        version="v0.1.0-beta.3",
        commit="a" * 40,
        main_worktree="/tmp/main",
        runtime_feature_id="integration-acceptance",
    )

    store.save(record)

    assert store.load() == record


@pytest.mark.parametrize("version", ["beta3", "v0.1-beta.3", "v0.1.0-beta.03", ""])
def test_version_must_use_canonical_beta_format(version: str) -> None:
    with pytest.raises(AcceptanceError, match="v0.1.0-beta.3"):
        validate_version(version)


def test_safe_main_requires_clean_checkout_at_exact_main_commit(tmp_path: Path) -> None:
    responses = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "main\n",
        ("rev-parse", "HEAD"): f"{'a' * 40}\n",
        ("rev-parse", "refs/heads/main"): f"{'b' * 40}\n",
        ("status", "--porcelain", "--untracked-files=normal"): "",
    }

    with pytest.raises(AcceptanceError, match="main.*一致"):
        assert_safe_main(tmp_path, run_git=lambda args: responses[tuple(args)])


def test_active_job_blocks_handoff_without_terminating_owner(tmp_path: Path) -> None:
    runtime_store = RuntimeStore(tmp_path)
    runtime_store.save(owner())
    terminated: list[int] = []

    with pytest.raises(AcceptanceError, match="transcribing.*拒绝切换"):
        handoff_existing_runtime(
            runtime_store,
            owner_is_live=lambda _: True,
            load_active_job=lambda: {"id": "job-1", "stage": "transcribing"},
            terminate=lambda pid: terminated.append(pid),
            wait_stopped=lambda _: True,
        )

    assert terminated == []
    assert runtime_store.load() == owner()


def test_idle_feature_runtime_is_terminated_before_acceptance_start(
    tmp_path: Path,
) -> None:
    runtime_store = RuntimeStore(tmp_path)
    runtime_store.save(owner())
    terminated: list[int] = []

    handoff_existing_runtime(
        runtime_store,
        owner_is_live=lambda _: True,
        load_active_job=lambda: None,
        terminate=lambda pid: terminated.append(pid),
        wait_stopped=lambda expected: expected.supervisor_pid == 123,
    )

    assert terminated == [123]


def test_stale_acceptance_runtime_is_terminated_before_new_commit_starts(
    tmp_path: Path,
) -> None:
    runtime_store = RuntimeStore(tmp_path)
    stale = owner()
    stale = RuntimeOwner.from_dict({
        **stale.to_dict(),
        "feature_id": "integration-acceptance",
    })
    runtime_store.save(stale)
    terminated: list[int] = []

    handoff_existing_runtime(
        runtime_store,
        owner_is_live=lambda _: True,
        load_active_job=lambda: None,
        terminate=lambda pid: terminated.append(pid),
        wait_stopped=lambda _: True,
    )

    assert terminated == [123]


def test_handoff_refuses_when_health_cannot_confirm_idle_state(tmp_path: Path) -> None:
    runtime_store = RuntimeStore(tmp_path)
    runtime_store.save(owner())

    with pytest.raises(AcceptanceError, match="无法确认.*空闲"):
        handoff_existing_runtime(
            runtime_store,
            owner_is_live=lambda _: True,
            load_active_job=lambda: (_ for _ in ()).throw(OSError("offline")),
            terminate=lambda _: None,
            wait_stopped=lambda _: True,
        )


def test_discovers_the_worktree_that_owns_main(tmp_path: Path) -> None:
    main_root = tmp_path / "main-checkout"
    listing = (
        f"worktree {tmp_path / 'feature'}\nHEAD {'a' * 40}\n"
        "branch refs/heads/codex/example\n\n"
        f"worktree {main_root}\nHEAD {'b' * 40}\nbranch refs/heads/main\n\n"
    )

    assert discover_main_worktree(tmp_path, listing=listing) == main_root


def test_acceptance_plan_pins_main_sources_and_version_label(tmp_path: Path) -> None:
    plan, record = build_acceptance_plan(
        controller_root=REPOSITORY_ROOT,
        main_root=REPOSITORY_ROOT,
        git_common_dir=tmp_path,
        home=Path.home(),
        version="v0.1.0-beta.3",
        commit="a" * 40,
    )

    assert plan.feature_root == REPOSITORY_ROOT
    assert plan.feature_id == "integration-acceptance"
    assert plan.backend_environment["AUDIO_MEMORY_ENVIRONMENT_LABEL"] == (
        "v0.1.0-beta.3 集成验收"
    )
    assert record.commit == "a" * 40
    assert record.main_worktree == str(REPOSITORY_ROOT)


def test_stop_acceptance_cannot_terminate_feature_runtime(tmp_path: Path) -> None:
    runtime_store = RuntimeStore(tmp_path)
    runtime_store.save(owner())
    acceptance_store = AcceptanceStore(tmp_path)
    acceptance_store.save(AcceptanceRecord(
        schema_version=1,
        version="v0.1.0-beta.3",
        commit="a" * 40,
        main_worktree="/tmp/main",
        runtime_feature_id="integration-acceptance",
    ))
    terminated: list[int] = []

    with pytest.raises(AcceptanceError, match="不属于集成验收"):
        stop_acceptance(
            runtime_store,
            acceptance_store,
            owner_is_live=lambda _: True,
            terminate=lambda pid: terminated.append(pid),
            wait_stopped=lambda _: True,
        )

    assert terminated == []
