#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.request import urlopen

from feature_runtime import (
    RuntimeOwner,
    RuntimePlan,
    RuntimeStore,
    owner_is_live,
    run_runtime,
)


RUNTIME_FEATURE_ID = "integration-acceptance"
VERSION_PATTERN = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-beta\.(?:0|[1-9][0-9]*)$"
)


class AcceptanceError(RuntimeError):
    """版本集成验收环境不安全或与 main 不一致。"""


@dataclass(frozen=True, slots=True)
class AcceptanceRecord:
    schema_version: int
    version: str
    commit: str
    main_worktree: str
    runtime_feature_id: str

    @classmethod
    def from_dict(cls, payload: object) -> AcceptanceRecord:
        if not isinstance(payload, dict) or set(payload) != {
            item.name for item in fields(cls)
        }:
            raise AcceptanceError("集成验收记录字段无效。")
        try:
            validate_version(payload["version"])
            if (
                payload["schema_version"] != 1
                or not isinstance(payload["commit"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", payload["commit"])
                or not isinstance(payload["main_worktree"], str)
                or not Path(payload["main_worktree"]).is_absolute()
                or payload["runtime_feature_id"] != RUNTIME_FEATURE_ID
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceError("集成验收记录内容无效。") from exc
        return cls(**payload)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class AcceptanceStore:
    def __init__(self, git_common_dir: Path) -> None:
        self.root = git_common_dir.resolve() / "audio-memory-governance" / "runtime"
        self.path = self.root / "integration-acceptance.json"

    def load(self) -> AcceptanceRecord | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AcceptanceError("集成验收记录必须是普通文件。")
        try:
            return AcceptanceRecord.from_dict(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError("集成验收记录无法读取。") from exc

    def save(self, record: AcceptanceRecord) -> None:
        validated = AcceptanceRecord.from_dict(record.to_dict())
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".integration-acceptance.", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(validated.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def remove(self, expected: AcceptanceRecord) -> None:
        if self.load() == expected:
            self.path.unlink()


def validate_version(version: str) -> str:
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise AcceptanceError("版本号必须使用 v0.1.0-beta.3 格式。")
    return version


def _run_git(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AcceptanceError("无法校验 main worktree。")
    return result.stdout


def assert_safe_main(
    main_root: Path,
    *,
    run_git: Callable[[list[str]], str] | None = None,
) -> str:
    runner = run_git or (lambda arguments: _run_git(main_root, arguments))
    branch = runner(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    head = runner(["rev-parse", "HEAD"]).strip()
    main = runner(["rev-parse", "refs/heads/main"]).strip()
    dirty = runner(["status", "--porcelain", "--untracked-files=normal"])
    if branch != "main" or head != main:
        raise AcceptanceError("集成验收 worktree 必须与当前 main 完全一致。")
    if dirty:
        raise AcceptanceError("集成验收不允许使用未提交的 main worktree。")
    return head


def handoff_existing_runtime(
    runtime_store: RuntimeStore,
    *,
    owner_is_live: Callable[[RuntimeOwner], bool],
    load_active_job: Callable[[], dict[str, object] | None],
    terminate: Callable[[int], None],
    wait_stopped: Callable[[RuntimeOwner], bool],
) -> None:
    existing = runtime_store.load()
    if existing is None:
        return
    if not owner_is_live(existing):
        runtime_store.remove(existing)
        return
    try:
        active = load_active_job()
    except Exception as exc:
        raise AcceptanceError("无法确认当前开发环境已空闲，拒绝切换。") from exc
    if active is not None:
        stage = active.get("stage", "unknown")
        raise AcceptanceError(f"当前任务仍处于 {stage}，拒绝切换。")
    terminate(existing.supervisor_pid)
    if not wait_stopped(existing):
        raise AcceptanceError("原开发环境未在限时内安全停止。")


def _load_active_job() -> dict[str, object] | None:
    with urlopen("http://127.0.0.1:8766/api/jobs/active", timeout=3) as response:
        payload = json.loads(response.read())
    if payload is not None and not isinstance(payload, dict):
        raise AcceptanceError("开发后端返回了无效任务状态。")
    return payload


def _wait_owner_stopped(store: RuntimeStore, expected: RuntimeOwner) -> bool:
    for _ in range(80):
        current = store.load()
        if current is None or current != expected or not owner_is_live(expected):
            return True
        time.sleep(0.1)
    return False


def spawn_detached_supervisor(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    log_fd = os.open(log_path, flags, 0o600)
    try:
        metadata = os.fstat(log_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AcceptanceError("集成验收启动日志必须是单链接普通文件。")
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(log_fd)


def discover_main_worktree(repository_root: Path, *, listing: str | None = None) -> Path:
    if listing is None:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AcceptanceError("无法读取 Git worktree 列表。")
        listing = result.stdout
    for block in listing.strip().split("\n\n"):
        values: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            values[key] = value
        if values.get("branch") == "refs/heads/main" and values.get("worktree"):
            return Path(values["worktree"]).resolve()
    raise AcceptanceError("未找到当前 main worktree。")


def build_acceptance_plan(
    *,
    controller_root: Path,
    main_root: Path,
    git_common_dir: Path,
    home: Path,
    version: str,
    commit: str,
) -> tuple[RuntimePlan, AcceptanceRecord]:
    del git_common_dir
    validated_version = validate_version(version)
    plan = RuntimePlan.build(
        controller_root=controller_root,
        feature_root=main_root,
        home=home,
        feature_id=RUNTIME_FEATURE_ID,
    )
    plan.backend_environment["AUDIO_MEMORY_ENVIRONMENT_LABEL"] = (
        f"{validated_version} 集成验收"
    )
    return plan, AcceptanceRecord(
        schema_version=1,
        version=validated_version,
        commit=commit,
        main_worktree=str(main_root.resolve()),
        runtime_feature_id=RUNTIME_FEATURE_ID,
    )


def stop_acceptance(
    runtime_store: RuntimeStore,
    acceptance_store: AcceptanceStore,
    *,
    owner_is_live: Callable[[RuntimeOwner], bool],
    terminate: Callable[[int], None],
    wait_stopped: Callable[[RuntimeOwner], bool],
) -> None:
    record = acceptance_store.load()
    owner = runtime_store.load()
    if record is None and owner is None:
        return
    if owner is None or not owner_is_live(owner):
        if owner is not None:
            runtime_store.remove(owner)
        if record is not None:
            acceptance_store.remove(record)
        return
    if owner.feature_id != RUNTIME_FEATURE_ID:
        raise AcceptanceError("当前开发运行时不属于集成验收，拒绝停止。")
    if record is None:
        raise AcceptanceError("缺少集成验收元数据，拒绝停止。")
    terminate(owner.supervisor_pid)
    if not wait_stopped(owner):
        raise AcceptanceError("集成验收环境未在限时内停止。")
    acceptance_store.remove(record)


def _common_dir(repository_root: Path) -> Path:
    value = _run_git(repository_root, ["rev-parse", "--git-common-dir"]).strip()
    path = Path(value)
    return (repository_root / path).resolve() if not path.is_absolute() else path.resolve()


def start_acceptance(
    version: str,
    *,
    controller_root: Path,
    main_root: Path,
    git_common_dir: Path,
    home: Path,
) -> int:
    commit = assert_safe_main(main_root)
    plan, record = build_acceptance_plan(
        controller_root=controller_root,
        main_root=main_root,
        git_common_dir=git_common_dir,
        home=home,
        version=version,
        commit=commit,
    )
    runtime_store = RuntimeStore(git_common_dir)
    acceptance_store = AcceptanceStore(git_common_dir)
    existing_owner = runtime_store.load()
    existing_record = acceptance_store.load()
    if (
        existing_owner is not None
        and owner_is_live(existing_owner)
        and existing_owner.feature_id == RUNTIME_FEATURE_ID
        and existing_record == record
    ):
        print(f"{record.version} 集成验收页面已在运行：http://127.0.0.1:5173")
        return 0
    handoff_existing_runtime(
        runtime_store,
        owner_is_live=owner_is_live,
        load_active_job=_load_active_job,
        terminate=lambda pid: os.kill(pid, signal.SIGTERM),
        wait_stopped=lambda expected: _wait_owner_stopped(runtime_store, expected),
    )
    if existing_record is not None:
        acceptance_store.remove(existing_record)
    acceptance_store.save(record)
    try:
        return run_runtime(plan, runtime_store)
    finally:
        acceptance_store.remove(record)


def status_acceptance(
    runtime_store: RuntimeStore, acceptance_store: AcceptanceStore
) -> dict[str, object]:
    owner = runtime_store.load()
    record = acceptance_store.load()
    running = bool(
        owner is not None
        and record is not None
        and owner.feature_id == RUNTIME_FEATURE_ID
        and owner_is_live(owner)
    )
    return {
        "running": running,
        "version": record.version if running and record else None,
        "commit": record.commit if running and record else None,
        "main_worktree": record.main_worktree if running and record else None,
        "frontend_url": "http://127.0.0.1:5173" if running else None,
        "backend_url": "http://127.0.0.1:8766" if running else None,
    }


def launch_acceptance(
    version: str,
    *,
    controller_root: Path,
    git_common_dir: Path,
) -> int:
    validated_version = validate_version(version)
    log_path = (
        git_common_dir.resolve()
        / "audio-memory-governance/runtime/integration-acceptance-launch.log"
    )
    child = spawn_detached_supervisor(
        (
            sys.executable,
            str(controller_root / "scripts/integration_runtime.py"),
            "supervise",
            validated_version,
        ),
        cwd=controller_root,
        environment=dict(os.environ),
        log_path=log_path,
    )
    runtime_store = RuntimeStore(git_common_dir)
    acceptance_store = AcceptanceStore(git_common_dir)
    for _ in range(120):
        if child.poll() is not None:
            raise AcceptanceError(
                f"集成验收监督进程在就绪前退出，请查看 {log_path}。"
            )
        status = status_acceptance(runtime_store, acceptance_store)
        if status["running"] and _backend_ready_for_acceptance(validated_version):
            print(f"{validated_version} 集成验收页面已启动：http://127.0.0.1:5173")
            return 0
        time.sleep(0.25)
    raise AcceptanceError(f"集成验收环境未在 30 秒内就绪，请查看 {log_path}。")


def _backend_ready_for_acceptance(version: str) -> bool:
    try:
        with urlopen("http://127.0.0.1:8766/api/health", timeout=2) as response:
            payload = json.loads(response.read())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("profile") == "development"
        and payload.get("environment_label") == f"{version} 集成验收"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audio Memory 版本集成验收环境")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("version")
    supervise_parser = subparsers.add_parser("supervise")
    supervise_parser.add_argument("version")
    subparsers.add_parser("status")
    subparsers.add_parser("stop")
    arguments = parser.parse_args(argv)
    controller_root = Path(__file__).resolve().parent.parent
    git_common_dir = _common_dir(controller_root)
    runtime_store = RuntimeStore(git_common_dir)
    acceptance_store = AcceptanceStore(git_common_dir)
    try:
        if arguments.command == "start":
            return launch_acceptance(
                arguments.version,
                controller_root=controller_root,
                git_common_dir=git_common_dir,
            )
        if arguments.command == "supervise":
            main_root = discover_main_worktree(controller_root)
            return start_acceptance(
                arguments.version,
                controller_root=controller_root,
                main_root=main_root,
                git_common_dir=git_common_dir,
                home=Path.home(),
            )
        if arguments.command == "status":
            print(json.dumps(
                status_acceptance(runtime_store, acceptance_store),
                ensure_ascii=False,
                indent=2,
            ))
            return 0
        stop_acceptance(
            runtime_store,
            acceptance_store,
            owner_is_live=owner_is_live,
            terminate=lambda pid: os.kill(pid, signal.SIGTERM),
            wait_stopped=lambda expected: _wait_owner_stopped(runtime_store, expected),
        )
        print("Audio Memory 集成验收环境已停止。")
        return 0
    except (AcceptanceError, OSError) as exc:
        print(f"集成验收环境操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
