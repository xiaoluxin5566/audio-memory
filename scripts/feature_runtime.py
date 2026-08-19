#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, ClassVar
from urllib.error import URLError
from urllib.request import urlopen


class FeatureRuntimeError(RuntimeError):
    """功能开发运行时不安全或已被其他功能占用。"""


def _verified_executable(path: Path, label: str) -> Path:
    candidate = path.absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FeatureRuntimeError(f"{label}不存在。") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise FeatureRuntimeError(f"{label}不可执行。")
    # 保留虚拟环境入口路径；返回实际目标会丢失 venv 语义。
    return candidate


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    feature_id: str
    feature_root: Path
    backend_argv: tuple[str, ...]
    backend_environment: dict[str, str]
    frontend_argv: tuple[str, ...]
    frontend_environment: dict[str, str]
    frontend_cwd: Path

    @classmethod
    def build(
        cls,
        *,
        controller_root: Path,
        feature_root: Path,
        home: Path,
        feature_id: str,
    ) -> RuntimePlan:
        controller = controller_root.resolve()
        feature = feature_root.resolve()
        required = (
            feature / "backend/src/audio_memory/main.py",
            feature / "prototype/package.json",
            controller / "scripts/dev_lifecycle.py",
        )
        if not all(path.is_file() for path in required):
            raise FeatureRuntimeError("功能 worktree 缺少必要的开发源码。")
        python = _verified_executable(
            controller / "backend/.venv/bin/python", "开发 Python"
        )
        vite = _verified_executable(
            controller / "prototype/node_modules/.bin/vite", "Vite"
        )
        backend_environment = dict(os.environ)
        backend_environment.update(
            {
                "HOME": str(home.resolve()),
                "AUDIO_MEMORY_DEV_PYTHON": str(python),
                "PYTHONPATH": str(feature / "backend/src"),
            }
        )
        frontend_environment = dict(os.environ)
        frontend_environment.update(
            {
                "AUDIO_MEMORY_BACKEND_URL": "http://127.0.0.1:8766",
                "AUDIO_MEMORY_EXPECTED_PROFILE": "development",
            }
        )
        return cls(
            feature_id=feature_id,
            feature_root=feature,
            backend_argv=(
                str(python),
                str(controller / "scripts/dev_lifecycle.py"),
                "start",
                "--project-root",
                str(feature),
                "--home",
                str(home.resolve()),
            ),
            backend_environment=backend_environment,
            frontend_argv=(
                str(vite),
                "--host",
                "127.0.0.1",
                "--port",
                "5173",
                "--strictPort",
            ),
            frontend_environment=frontend_environment,
            frontend_cwd=feature / "prototype",
        )


@dataclass(frozen=True, slots=True)
class RuntimeOwner:
    schema_version: int
    feature_id: str
    worktree: str
    supervisor_pid: int
    supervisor_started_at: str
    supervisor_argv: tuple[str, ...]
    backend_port: int
    frontend_port: int
    phase: str

    VALID_PHASES: ClassVar[frozenset[str]] = frozenset({"starting", "ready"})

    @classmethod
    def from_dict(cls, payload: object) -> RuntimeOwner:
        if not isinstance(payload, dict) or set(payload) != {
            item.name for item in fields(cls)
        }:
            raise FeatureRuntimeError("运行时所有者记录字段无效。")
        try:
            if payload["schema_version"] != 1:
                raise ValueError
            if (
                not isinstance(payload["feature_id"], str)
                or not payload["feature_id"]
                or not isinstance(payload["worktree"], str)
                or not Path(payload["worktree"]).is_absolute()
                or not isinstance(payload["supervisor_pid"], int)
                or isinstance(payload["supervisor_pid"], bool)
                or payload["supervisor_pid"] <= 0
                or not isinstance(payload["supervisor_started_at"], str)
                or not payload["supervisor_started_at"].strip()
                or not isinstance(payload["supervisor_argv"], list)
                or not payload["supervisor_argv"]
                or not all(isinstance(item, str) for item in payload["supervisor_argv"])
                or payload["backend_port"] != 8766
                or payload["frontend_port"] != 5173
                or payload["phase"] not in cls.VALID_PHASES
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise FeatureRuntimeError("运行时所有者记录内容无效。") from exc
        return cls(
            schema_version=1,
            feature_id=payload["feature_id"],
            worktree=payload["worktree"],
            supervisor_pid=payload["supervisor_pid"],
            supervisor_started_at=payload["supervisor_started_at"].strip(),
            supervisor_argv=tuple(payload["supervisor_argv"]),
            backend_port=8766,
            frontend_port=5173,
            phase=payload["phase"],
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["supervisor_argv"] = list(self.supervisor_argv)
        return payload


class RuntimeStore:
    FILE_NAME = "development-owner.json"

    def __init__(self, git_common_dir: Path) -> None:
        self.root = git_common_dir.resolve() / "audio-memory-governance" / "runtime"
        self.path = self.root / self.FILE_NAME

    def load(self) -> RuntimeOwner | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        self._validate_file(self.path)
        try:
            return RuntimeOwner.from_dict(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FeatureRuntimeError("运行时所有者记录无法读取。") from exc

    def save(self, owner: RuntimeOwner) -> None:
        validated = RuntimeOwner.from_dict(owner.to_dict())
        self._ensure_root()
        if self.path.exists() or self.path.is_symlink():
            self._validate_file(self.path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".development-owner.", suffix=".tmp", dir=self.root
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
            root_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def require_available_for(
        self,
        feature_id: str,
        *,
        owner_is_live: Callable[[RuntimeOwner], bool],
    ) -> RuntimeOwner | None:
        owner = self.load()
        if owner is None:
            return None
        if owner_is_live(owner):
            if owner.feature_id != feature_id:
                raise FeatureRuntimeError(
                    f"开发运行时已属于功能 {owner.feature_id}，"
                    f"不能由功能 {feature_id} 替换。"
                )
            return owner
        self.remove(owner)
        return None

    def remove(self, expected: RuntimeOwner) -> None:
        current = self.load()
        if current == expected:
            self.path.unlink()

    def _ensure_root(self) -> None:
        parent = self.root.parent
        if parent.exists() and parent.is_symlink():
            raise FeatureRuntimeError("运行时管理目录不能是符号链接。")
        parent.mkdir(mode=0o700, exist_ok=True)
        if self.root.exists() and self.root.is_symlink():
            raise FeatureRuntimeError("运行时目录不能是符号链接。")
        self.root.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def _validate_file(path: Path) -> None:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise FeatureRuntimeError("运行时所有者记录不能是符号链接。")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FeatureRuntimeError("运行时所有者记录必须是单链接普通文件。")


def _process_value(pid: int, field: str) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", f"{field}="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def owner_is_live(owner: RuntimeOwner) -> bool:
    return _process_value(owner.supervisor_pid, "lstart") == (
        owner.supervisor_started_at
    )


def _port_is_occupied(port: int) -> bool:
    return subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _backend_ready() -> bool:
    try:
        with urlopen("http://127.0.0.1:8766/api/health", timeout=2) as response:
            payload = json.loads(response.read())
    except (OSError, URLError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("profile") == "development"
    )


def _frontend_ready() -> bool:
    try:
        with urlopen("http://127.0.0.1:5173/", timeout=2) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _wait_ready(
    child: subprocess.Popen[bytes], probe: Callable[[], bool], label: str
) -> None:
    for _ in range(60):
        if probe():
            return
        if child.poll() is not None:
            raise FeatureRuntimeError(f"{label}在就绪前已退出。")
        time.sleep(0.5)
    raise FeatureRuntimeError(f"{label}在 30 秒内未就绪。")


def _terminate(child: subprocess.Popen[bytes] | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run_runtime(plan: RuntimePlan, store: RuntimeStore) -> int:
    existing = store.require_available_for(
        plan.feature_id, owner_is_live=owner_is_live
    )
    if existing is not None:
        print("Audio Memory 开发页面已在运行：http://127.0.0.1:5173")
        return 0
    occupied = [port for port in (8766, 5173) if _port_is_occupied(port)]
    if occupied:
        raise FeatureRuntimeError(
            f"开发端口已被未记录的进程占用：{occupied}"
        )
    started_at = _process_value(os.getpid(), "lstart")
    if not started_at:
        raise FeatureRuntimeError("无法记录开发运行时的启动身份。")
    owner = RuntimeOwner(
        schema_version=1,
        feature_id=plan.feature_id,
        worktree=str(plan.feature_root),
        supervisor_pid=os.getpid(),
        supervisor_started_at=started_at,
        supervisor_argv=tuple([sys.executable, *sys.argv]),
        backend_port=8766,
        frontend_port=5173,
        phase="starting",
    )
    store.save(owner)
    backend: subprocess.Popen[bytes] | None = None
    frontend: subprocess.Popen[bytes] | None = None
    received_signal: int | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum
        for child in (frontend, backend):
            if child is not None and child.poll() is None:
                child.send_signal(signum)

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        backend = subprocess.Popen(
            plan.backend_argv,
            cwd=plan.feature_root,
            env=plan.backend_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        _wait_ready(backend, _backend_ready, "8766 开发后端")
        runtime_root = plan.feature_root / ".runtime/dev/runtime"
        runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = runtime_root / "audio-memory-vite-dev.log"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        log_fd = os.open(log_path, flags, 0o600)
        try:
            metadata = os.fstat(log_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise FeatureRuntimeError("Vite 日志必须是单链接普通文件。")
            frontend = subprocess.Popen(
                plan.frontend_argv,
                cwd=plan.frontend_cwd,
                env=plan.frontend_environment,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        finally:
            os.close(log_fd)
        _wait_ready(frontend, _frontend_ready, "5173 开发页面")
        ready_owner = RuntimeOwner.from_dict(
            {**owner.to_dict(), "phase": "ready"}
        )
        store.save(ready_owner)
        owner = ready_owner
        print("Audio Memory 开发页面已启动：http://127.0.0.1:5173", flush=True)
        if os.environ.get("AUDIO_MEMORY_NO_OPEN") != "1":
            subprocess.run(
                ["open", "http://127.0.0.1:5173"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        while received_signal is None:
            if backend.poll() is not None or frontend.poll() is not None:
                raise FeatureRuntimeError("开发前端或后端意外退出。")
            time.sleep(0.25)
        return 128 + received_signal
    finally:
        _terminate(frontend)
        _terminate(backend)
        store.remove(owner)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def stop_runtime(feature_id: str, store: RuntimeStore) -> int:
    owner = store.load()
    if owner is None:
        print("Audio Memory 开发页面未运行。")
        return 0
    if owner.feature_id != feature_id:
        raise FeatureRuntimeError(
            f"开发运行时属于功能 {owner.feature_id}，"
            f"功能 {feature_id} 无权停止。"
        )
    if not owner_is_live(owner):
        store.remove(owner)
        raise FeatureRuntimeError("已清理过期的开发运行时记录。")
    os.kill(owner.supervisor_pid, signal.SIGTERM)
    print("Audio Memory 开发环境正在停止。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audio Memory 联合开发运行时")
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--feature-id", required=True)
    parser.add_argument("--git-common-dir", type=Path, required=True)
    parser.add_argument("--controller-root", type=Path)
    parser.add_argument("--feature-root", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    arguments = parser.parse_args(argv)
    store = RuntimeStore(arguments.git_common_dir)
    try:
        if arguments.command == "stop":
            return stop_runtime(arguments.feature_id, store)
        if arguments.controller_root is None or arguments.feature_root is None:
            raise FeatureRuntimeError("启动开发运行时缺少代码根目录。")
        plan = RuntimePlan.build(
            controller_root=arguments.controller_root,
            feature_root=arguments.feature_root,
            home=arguments.home,
            feature_id=arguments.feature_id,
        )
        return run_runtime(plan, store)
    except (FeatureRuntimeError, OSError) as exc:
        print(f"开发运行时操作失败：{exc}", file=sys.stderr)
        return 1


__all__ = [
    "FeatureRuntimeError",
    "RuntimeOwner",
    "RuntimePlan",
    "RuntimeStore",
]


if __name__ == "__main__":
    raise SystemExit(main())
