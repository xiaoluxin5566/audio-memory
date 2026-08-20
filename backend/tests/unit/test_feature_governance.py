from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FEATURE_START = REPOSITORY_ROOT / "scripts" / "feature-start.sh"
FEATURE_STATUS = REPOSITORY_ROOT / "scripts" / "feature-status.sh"
FEATURE_STOP = REPOSITORY_ROOT / "scripts" / "feature-stop.sh"
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from feature_governance import (  # noqa: E402
    FeatureRecord,
    FeatureService,
    FeatureStore,
    GitRepository,
    GovernanceError,
    ReleaseService,
)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.com")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    (root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    git(root, "add", "README.md", ".gitignore")
    git(root, "commit", "-m", "initial")
    return root


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


def test_start_creates_one_branch_worktree_and_shared_progress_record(
    git_repository: Path,
) -> None:
    service = FeatureService(GitRepository(git_repository))

    started = service.start("report-progress", "v0.1.0-beta.3")

    assert started.record.branch == "codex/report-progress"
    assert started.path == git_repository / ".worktrees/report-progress"
    assert git(started.path, "branch", "--show-current") == "codex/report-progress"
    assert git(git_repository, "status", "--porcelain") == ""
    common_dir = Path(git(git_repository, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = git_repository / common_dir
    assert (
        common_dir
        / "audio-memory-governance/features/report-progress.json"
    ).is_file()


def test_start_existing_feature_resumes_without_creating_another_branch(
    git_repository: Path,
) -> None:
    service = FeatureService(GitRepository(git_repository))
    first = service.start("search", "v0.1.0-beta.3")

    second = service.start("search", "v0.1.0-beta.3")

    assert second == first
    branches = git(git_repository, "branch", "--format=%(refname:short)")
    assert branches.splitlines().count("codex/search") == 1


def test_start_refuses_dirty_main(git_repository: Path) -> None:
    (git_repository / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(GovernanceError, match="main.*未提交"):
        FeatureService(GitRepository(git_repository)).start(
            "search", "v0.1.0-beta.3"
        )

    assert not (git_repository / ".worktrees/search").exists()


def test_start_refuses_unignored_worktree_directory(git_repository: Path) -> None:
    git(git_repository, "rm", ".gitignore")
    git(git_repository, "commit", "-m", "remove ignore rule")

    with pytest.raises(GovernanceError, match=r"\.worktrees.*ignore"):
        FeatureService(GitRepository(git_repository)).start(
            "search", "v0.1.0-beta.3"
        )

    assert not (git_repository / ".worktrees/search").exists()


def test_start_preflights_progress_storage_before_creating_git_state(
    git_repository: Path,
) -> None:
    common_dir = Path(git(git_repository, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = git_repository / common_dir
    outside = git_repository.parent / "outside-governance"
    outside.mkdir()
    (common_dir / "audio-memory-governance").symlink_to(outside)

    with pytest.raises(GovernanceError, match="符号链接"):
        FeatureService(GitRepository(git_repository)).start(
            "search", "v0.1.0-beta.3"
        )

    assert not (git_repository / ".worktrees/search").exists()
    assert "codex/search" not in git(
        git_repository, "branch", "--format=%(refname:short)"
    ).splitlines()


def test_start_refuses_progress_record_without_its_worktree(
    git_repository: Path,
) -> None:
    repository = GitRepository(git_repository)
    service = FeatureService(repository)
    started = service.start("search", "v0.1.0-beta.3")
    git(git_repository, "worktree", "remove", str(started.path))

    with pytest.raises(GovernanceError, match="开发进度记录.*worktree"):
        service.start("search", "v0.1.0-beta.3")


def test_status_lists_tracks_from_shared_git_state(git_repository: Path) -> None:
    service = FeatureService(GitRepository(git_repository))
    service.start("one", "v0.1.0-beta.3")

    statuses = service.status()

    assert [(item.record.feature_id, item.valid) for item in statuses] == [
        ("one", True)
    ]


def test_shell_entries_create_then_report_the_same_track(
    git_repository: Path,
) -> None:
    environment = {
        **os.environ,
        "AUDIO_MEMORY_FEATURE_NO_RUNTIME": "1",
    }
    started = subprocess.run(
        [str(FEATURE_START), "shell-track"],
        cwd=git_repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    reported = subprocess.run(
        [str(FEATURE_STATUS), "shell-track"],
        cwd=git_repository,
        text=True,
        capture_output=True,
        check=False,
    )

    assert started.returncode == 0, started.stderr
    assert reported.returncode == 0, reported.stderr
    assert json.loads(started.stdout)["branch"] == "codex/shell-track"
    assert json.loads(reported.stdout) == [
        {
            "feature_id": "shell-track",
            "branch": "codex/shell-track",
            "worktree": str(git_repository / ".worktrees/shell-track"),
            "status": "in_progress",
            "valid": True,
            "diagnostic": None,
        }
    ]


def test_feature_stop_is_safe_when_no_runtime_is_recorded(
    git_repository: Path,
) -> None:
    result = subprocess.run(
        [str(FEATURE_STOP), "shell-track"],
        cwd=git_repository,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "未运行" in result.stdout


def test_finish_never_marks_dirty_feature_ready(git_repository: Path) -> None:
    service = FeatureService(GitRepository(git_repository))
    started = service.start("search", "v0.1.0-beta.3")
    (started.path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(GovernanceError, match="未提交"):
        service.finish("search", lambda _: FeatureRecord.REQUIRED_CHECKS)

    assert service.store.load("search").status == "in_progress"


def test_finish_records_exact_tested_commit_and_checks(
    git_repository: Path,
) -> None:
    service = FeatureService(GitRepository(git_repository))
    started = service.start("search", "v0.1.0-beta.3")

    finished = service.finish(
        "search", lambda _: FeatureRecord.REQUIRED_CHECKS
    )

    assert finished.record.status == "ready_to_merge"
    assert finished.record.head_commit == git(started.path, "rev-parse", "HEAD")
    assert finished.record.passed_checks == FeatureRecord.REQUIRED_CHECKS
    assert finished.record.current_step == "等待合并"


def test_failed_gate_keeps_feature_in_progress(git_repository: Path) -> None:
    service = FeatureService(GitRepository(git_repository))
    service.start("search", "v0.1.0-beta.3")

    with pytest.raises(GovernanceError, match="browser"):
        service.finish(
            "search",
            lambda _: ("backend", "frontend", "runtime-isolation"),
        )

    assert service.store.load("search").status == "in_progress"


def test_new_commit_invalidates_ready_evidence(git_repository: Path) -> None:
    service = FeatureService(GitRepository(git_repository))
    started = service.start("search", "v0.1.0-beta.3")
    service.finish("search", lambda _: FeatureRecord.REQUIRED_CHECKS)
    (started.path / "change.txt").write_text("later\n", encoding="utf-8")
    git(started.path, "add", "change.txt")
    git(started.path, "commit", "-m", "later change")

    status = service.status("search")[0]

    assert status.record.status == "in_progress"
    assert status.record.passed_checks == ()
    assert service.store.load("search").status == "ready_to_merge"


def ready_feature(root: Path, feature_id: str) -> str:
    service = FeatureService(GitRepository(root))
    started = service.start(feature_id, "v0.1.0-beta.3")
    (started.path / f"{feature_id}.txt").write_text(feature_id, encoding="utf-8")
    git(started.path, "add", f"{feature_id}.txt")
    git(started.path, "commit", "-m", f"add {feature_id}")
    return service.finish(
        feature_id, lambda _: FeatureRecord.REQUIRED_CHECKS
    ).record.head_commit


def test_prepare_lists_only_selected_ready_commits(git_repository: Path) -> None:
    one_sha = ready_feature(git_repository, "one")
    FeatureService(GitRepository(git_repository)).start(
        "two", "v0.1.0-beta.3"
    )
    release = ReleaseService(GitRepository(git_repository))

    manifest, path = release.prepare("v0.1.0-beta.3", ["one"])

    assert [(item.feature_id, item.tested_commit) for item in manifest.features] == [
        ("one", one_sha)
    ]
    assert path.is_file()
    with pytest.raises(GovernanceError, match="尚未通过"):
        release.prepare("v0.1.0-beta.3", ["two"])


def test_integrate_without_exact_approval_does_not_change_main(
    git_repository: Path,
) -> None:
    ready_feature(git_repository, "one")
    release = ReleaseService(GitRepository(git_repository))
    _, path = release.prepare("v0.1.0-beta.3", ["one"])
    before = git(git_repository, "rev-parse", "main")

    with pytest.raises(GovernanceError, match="确认"):
        release.integrate(path, approval_token=None, gate_runner=lambda *_: True)

    assert git(git_repository, "rev-parse", "main") == before


def test_integration_stops_at_first_failed_feature(git_repository: Path) -> None:
    for feature_id in ("one", "two", "three"):
        ready_feature(git_repository, feature_id)
    release = ReleaseService(GitRepository(git_repository))
    manifest, path = release.prepare(
        "v0.1.0-beta.3", ["one", "two", "three"]
    )

    result = release.integrate(
        path,
        approval_token=manifest.digest(),
        gate_runner=lambda feature_id, _: feature_id != "two",
    )

    assert result.merged == ("one",)
    assert result.failed == "two"
    assert (git_repository / "one.txt").is_file()
    assert not (git_repository / "two.txt").exists()
    assert not (git_repository / "three.txt").exists()
    store = FeatureService(GitRepository(git_repository)).store
    assert store.load("one").status == "merged"
    assert store.load("two").status == "in_progress"
    assert store.load("three").status == "ready_to_merge"


def test_integrate_refuses_stale_main_before_merging(git_repository: Path) -> None:
    ready_feature(git_repository, "one")
    release = ReleaseService(GitRepository(git_repository))
    manifest, path = release.prepare("v0.1.0-beta.3", ["one"])
    (git_repository / "main-change.txt").write_text("later", encoding="utf-8")
    git(git_repository, "add", "main-change.txt")
    git(git_repository, "commit", "-m", "advance main")
    before = git(git_repository, "rev-parse", "HEAD")

    with pytest.raises(GovernanceError, match="main 已变更"):
        release.integrate(path, manifest.digest(), lambda *_: True)

    assert git(git_repository, "rev-parse", "HEAD") == before
    assert not (git_repository / "one.txt").exists()


def test_integration_aborts_conflict_and_keeps_prior_success(
    git_repository: Path,
) -> None:
    service = FeatureService(GitRepository(git_repository))
    for feature_id, content in (("one", "from one\n"), ("two", "from two\n")):
        started = service.start(feature_id, "v0.1.0-beta.3")
        (started.path / "README.md").write_text(content, encoding="utf-8")
        git(started.path, "add", "README.md")
        git(started.path, "commit", "-m", f"change readme in {feature_id}")
        service.finish(feature_id, lambda _: FeatureRecord.REQUIRED_CHECKS)
    release = ReleaseService(GitRepository(git_repository))
    manifest, path = release.prepare("v0.1.0-beta.3", ["one", "two"])

    result = release.integrate(path, manifest.digest(), lambda *_: True)

    assert result.merged == ("one",)
    assert result.failed == "two"
    assert (git_repository / "README.md").read_text(encoding="utf-8") == "from one\n"
    assert git(git_repository, "status", "--porcelain") == ""
    assert service.store.load("two").status == "in_progress"


def integrated_candidate(
    git_repository: Path,
) -> tuple[ReleaseService, object, Path]:
    (git_repository / "VERSION").write_text("0.1.0-beta.3\n", encoding="utf-8")
    git(git_repository, "add", "VERSION")
    git(git_repository, "commit", "-m", "set beta 3 version")
    ready_feature(git_repository, "one")
    release = ReleaseService(GitRepository(git_repository))
    manifest, path = release.prepare("v0.1.0-beta.3", ["one"])
    result = release.integrate(path, manifest.digest(), lambda *_: True)
    assert result.failed is None
    return release, manifest, path


def test_release_build_authorization_requires_separate_exact_confirmation(
    git_repository: Path,
) -> None:
    release, manifest, path = integrated_candidate(git_repository)

    with pytest.raises(GovernanceError, match="发布确认"):
        release.authorize_build(path, None)

    authorized = release.authorize_build(path, manifest.digest())
    assert authorized == manifest


def test_integrated_main_seal_requires_exact_digest_and_complete_gate(
    git_repository: Path,
) -> None:
    (git_repository / "VERSION").write_text("0.1.0-beta.3\n", encoding="utf-8")
    git(git_repository, "add", "VERSION")
    git(git_repository, "commit", "-m", "set beta 3 version")
    release = ReleaseService(GitRepository(git_repository))
    feature_ids = ["analysis-handoff", "interrupted-controls", "toast-center"]

    manifest = release.preview_integrated_main("v0.1.0-beta.3", feature_ids)

    assert [item.feature_id for item in manifest.features] == feature_ids
    assert {item.tested_commit for item in manifest.features} == {
        git(git_repository, "rev-parse", "HEAD")
    }
    assert not release.store.manifest_path("v0.1.0-beta.3").exists()
    with pytest.raises(GovernanceError, match="精确确认"):
        release.seal_integrated_main(
            "v0.1.0-beta.3", feature_ids, "wrong", lambda _: True
        )
    with pytest.raises(GovernanceError, match="全量验收失败"):
        release.seal_integrated_main(
            "v0.1.0-beta.3", feature_ids, manifest.digest(), lambda _: False
        )
    assert not release.store.manifest_path("v0.1.0-beta.3").exists()

    sealed, path = release.seal_integrated_main(
        "v0.1.0-beta.3", feature_ids, manifest.digest(), lambda _: True
    )

    assert sealed == manifest
    assert release.store.load_manifest(path) == manifest
    assert release.authorize_build(path, manifest.digest()) == manifest
    assert release.records_to_mark_released(manifest) == ()


def test_release_prevalidates_feature_records_before_creating_tag(
    git_repository: Path,
) -> None:
    release, manifest, _ = integrated_candidate(git_repository)

    records = release.records_to_mark_released(manifest)

    assert [record.feature_id for record in records] == ["one"]
    release.features.store.save(replace(records[0], status="in_progress"))
    with pytest.raises(GovernanceError, match="发布状态"):
        release.records_to_mark_released(manifest)


@pytest.mark.parametrize("fault", ["dirty", "not_main", "moved_head"])
def test_integrated_main_seal_rejects_mutable_source(
    git_repository: Path, fault: str
) -> None:
    (git_repository / "VERSION").write_text("0.1.0-beta.3\n", encoding="utf-8")
    git(git_repository, "add", "VERSION")
    git(git_repository, "commit", "-m", "set beta 3 version")
    release = ReleaseService(GitRepository(git_repository))
    feature_ids = ["analysis-handoff"]
    manifest = release.preview_integrated_main("v0.1.0-beta.3", feature_ids)
    if fault == "dirty":
        (git_repository / "dirty.txt").write_text("dirty", encoding="utf-8")
    elif fault == "not_main":
        git(git_repository, "switch", "-c", "codex/release")
    else:
        (git_repository / "later.txt").write_text("later", encoding="utf-8")
        git(git_repository, "add", "later.txt")
        git(git_repository, "commit", "-m", "move head")

    with pytest.raises(GovernanceError):
        release.seal_integrated_main(
            "v0.1.0-beta.3", feature_ids, manifest.digest(), lambda _: True
        )

    assert not release.store.manifest_path("v0.1.0-beta.3").exists()


@pytest.mark.parametrize("fault", ["dirty", "wrong_version", "tag_exists"])
def test_release_build_refuses_mutable_or_existing_version(
    git_repository: Path, fault: str
) -> None:
    release, manifest, path = integrated_candidate(git_repository)
    if fault == "dirty":
        (git_repository / "dirty.txt").write_text("dirty", encoding="utf-8")
    elif fault == "wrong_version":
        (git_repository / "VERSION").write_text("0.1.0-beta.2\n", encoding="utf-8")
    else:
        git(git_repository, "tag", "v0.1.0-beta.3")

    with pytest.raises(GovernanceError):
        release.authorize_build(path, manifest.digest())


def legacy_feature_worktree(root: Path, feature_id: str) -> Path:
    path = root / ".worktrees" / feature_id
    git(root, "worktree", "add", "-b", f"codex/{feature_id}", str(path), "main")
    return path


def test_adopt_preview_is_read_only_and_describes_current_track(
    git_repository: Path,
) -> None:
    feature_path = legacy_feature_worktree(git_repository, "legacy")
    service = FeatureService(GitRepository(feature_path))

    record, digest = service.adopt_preview("legacy", "v0.1.0-beta.3")

    assert record.branch == "codex/legacy"
    assert record.worktree == ".worktrees/legacy"
    assert record.head_commit == git(feature_path, "rev-parse", "HEAD")
    assert len(digest) == 64
    assert not service.store.exists("legacy")


def test_adopt_requires_exact_preview_digest_before_writing(
    git_repository: Path,
) -> None:
    feature_path = legacy_feature_worktree(git_repository, "legacy")
    service = FeatureService(GitRepository(feature_path))
    _, digest = service.adopt_preview("legacy", "v0.1.0-beta.3")

    with pytest.raises(GovernanceError, match="纳管确认"):
        service.adopt("legacy", "v0.1.0-beta.3", "wrong")
    assert not service.store.exists("legacy")

    adopted = service.adopt("legacy", "v0.1.0-beta.3", digest)
    assert adopted.record.status == "in_progress"
    assert service.store.load("legacy") == adopted.record
