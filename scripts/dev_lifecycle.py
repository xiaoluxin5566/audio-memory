#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_config import development_config


PID_NAME = "audio-memory-dev.pid"
LOG_NAME = "audio-memory-dev.log"
GUARD_NAME = "audio-memory-dev.start.lock"
MAX_RECORD_BYTES = 64 * 1024


class LifecycleError(RuntimeError):
    pass


class StaleProcessRecord(LifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    started_at: str
    argv: tuple[str, ...]
    command: str
    port: int

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "version": 1,
                    "pid": self.pid,
                    "started_at": self.started_at,
                    "argv": list(self.argv),
                    "command": self.command,
                    "port": self.port,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded development lifecycle")
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    return parser


def _server_argv(project_root: Path, port: int) -> tuple[str, ...]:
    backend_source = project_root / "backend" / "src"
    return (
        str(Path(sys.executable).resolve()),
        "-m",
        "uvicorn",
        "audio_memory.main:app",
        "--app-dir",
        str(backend_source),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    )


def _expected_process_command(argv: tuple[str, ...]) -> str:
    return " ".join(argv)


def _server_environment(config: Any, project_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "AUDIO_MEMORY_PROFILE": "development",
            "AUDIO_MEMORY_DATA_ROOT": str(config.paths.root),
            "AUDIO_MEMORY_KEYCHAIN_SERVICE": config.keychain_service,
            "AUDIO_MEMORY_PORT": str(config.port),
            "PYTHONPATH": str(project_root / "backend" / "src"),
        }
    )
    if config.paths.models_writable:
        environment["AUDIO_MEMORY_MODEL_ROOT"] = str(config.paths.models)
    else:
        environment.pop("AUDIO_MEMORY_MODEL_ROOT", None)
    return environment


def _nofollow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:
        raise LifecycleError("当前系统不支持安全的 no-follow 文件打开。")
    return flag


def _open_regular_at(
    runtime_fd: int, name: str, flags: int, mode: int = 0o600
) -> int:
    try:
        fd = os.open(name, flags | _nofollow_flag(), mode, dir_fd=runtime_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LifecycleError(f"拒绝跟随运行文件的符号链接：{name}") from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise LifecycleError(f"运行文件必须是普通文件：{name}")
    return fd


def _open_runtime(config: Any, *, create: bool) -> int | None:
    runtime = config.paths.runtime
    if create:
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.validate_development_isolation()
    elif not runtime.exists():
        return None
    try:
        fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag())
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise LifecycleError("开发 runtime 目录不能是符号链接。") from exc
        raise
    if create:
        os.fchmod(fd, 0o700)
    return fd


def _unlink_at(runtime_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=runtime_fd)
    except FileNotFoundError:
        pass


def _read_record_bytes(runtime_fd: int) -> bytes | None:
    try:
        fd = _open_regular_at(runtime_fd, PID_NAME, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except LifecycleError as exc:
        _unlink_at(runtime_fd, PID_NAME)
        raise StaleProcessRecord("已清理不安全的 PID 记录。") from exc
    try:
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > MAX_RECORD_BYTES:
        raise StaleProcessRecord("PID 记录过大。")
    return data


def _parse_record(data: bytes, expected_argv: tuple[str, ...], port: int) -> ProcessRecord:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaleProcessRecord("PID 记录格式无效。") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "pid",
        "started_at",
        "argv",
        "command",
        "port",
    }:
        raise StaleProcessRecord("PID 记录字段无效。")
    pid = payload["pid"]
    recorded_port = payload["port"]
    argv = payload["argv"]
    started_at = payload["started_at"]
    command = payload["command"]
    if (
        payload["version"] != 1
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or recorded_port != port
        or not isinstance(argv, list)
        or not all(isinstance(item, str) for item in argv)
        or tuple(argv) != expected_argv
        or not isinstance(started_at, str)
        or not started_at.strip()
        or not isinstance(command, str)
        or command.strip() != _expected_process_command(expected_argv)
    ):
        raise StaleProcessRecord("PID 记录身份无效。")
    return ProcessRecord(pid, started_at.strip(), tuple(argv), command.strip(), port)


def _write_record(runtime_fd: int, record: ProcessRecord) -> bytes:
    data = record.to_bytes()
    temporary = f".{PID_NAME}.tmp.{os.getpid()}"
    fd = _open_regular_at(
        runtime_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(
            temporary,
            PID_NAME,
            src_dir_fd=runtime_fd,
            dst_dir_fd=runtime_fd,
        )
    except BaseException:
        _unlink_at(runtime_fd, temporary)
        raise
    return data


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, check=False)


def _health(port: int) -> tuple[str, dict[str, Any] | None]:
    result = _run(
        [
            "curl",
            "--silent",
            "--fail",
            "--max-time",
            "2",
            f"http://127.0.0.1:{port}/api/health",
        ]
    )
    if result.returncode != 0:
        return "unavailable", None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "invalid", None
    return "ok", payload if isinstance(payload, dict) else None


def _port_is_occupied(port: int) -> bool:
    return _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]).returncode == 0


def _process_value(pid: int, field: str) -> str:
    result = _run(["ps", "-p", str(pid), "-o", f"{field}="])
    if result.returncode != 0 or not result.stdout.strip():
        raise StaleProcessRecord("记录中的开发进程已不存在。")
    return result.stdout.strip()


def _snapshot(pid: int) -> tuple[str, str]:
    return _process_value(pid, "command"), _process_value(pid, "lstart")


def _owns_listener(pid: int, port: int) -> bool:
    result = _run(
        [
            "lsof",
            "-nP",
            "-a",
            "-p",
            str(pid),
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
            "-t",
        ]
    )
    return result.returncode == 0 and str(pid) in result.stdout.splitlines()


def _validate_live_identity(record: ProcessRecord) -> None:
    command, started_at = _snapshot(record.pid)
    if command != record.command or started_at != record.started_at:
        raise StaleProcessRecord("开发进程的命令或启动身份已改变。")
    if not _owns_listener(record.pid, record.port):
        raise StaleProcessRecord("记录中的 PID 未持有预期开发端口。")


def _remove_if_unchanged(runtime_fd: int, expected: bytes) -> None:
    try:
        current = _read_record_bytes(runtime_fd)
    except StaleProcessRecord:
        return
    if current == expected:
        _unlink_at(runtime_fd, PID_NAME)


def start(project_root: Path, home: Path) -> int:
    config = development_config(project_root=project_root, home=home)
    health_status, health_payload = _health(config.port)
    if health_status == "ok":
        if health_payload and health_payload.get("profile") == "development":
            print(f"Audio Memory 开发环境已在运行：http://127.0.0.1:{config.port}/")
            return 0
        raise LifecycleError(f"端口 {config.port} 上的服务不是 Audio Memory 开发环境。")
    if _port_is_occupied(config.port):
        raise LifecycleError(f"端口 {config.port} 已被其他程序占用。")

    runtime_fd = _open_runtime(config, create=True)
    assert runtime_fd is not None
    child: subprocess.Popen[Any] | None = None
    record_bytes: bytes | None = None
    try:
        guard_fd = _open_regular_at(runtime_fd, GUARD_NAME, os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LifecycleError("另一个开发启动正在进行。") from exc
            log_fd = _open_regular_at(
                runtime_fd, LOG_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT
            )
            try:
                argv = _server_argv(project_root, config.port)
                child = subprocess.Popen(
                    list(argv),
                    cwd=project_root / "backend",
                    env=_server_environment(config, project_root),
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            finally:
                os.close(log_fd)

            command = ""
            started_at = ""
            for _ in range(20):
                try:
                    command, started_at = _snapshot(child.pid)
                    break
                except StaleProcessRecord:
                    if child.poll() is not None:
                        raise LifecycleError("开发服务在记录身份前已退出。")
                    time.sleep(0.05)
            if not command or not started_at:
                raise LifecycleError("无法记录开发进程的启动身份。")
            if command != _expected_process_command(argv):
                raise LifecycleError("开发进程命令与预期的精确启动命令不匹配。")
            record = ProcessRecord(child.pid, started_at, argv, command, config.port)
            record_bytes = _write_record(runtime_fd, record)

            for _ in range(60):
                status, payload = _health(config.port)
                if status == "ok":
                    if payload and payload.get("profile") == "development":
                        print(f"Audio Memory 开发环境已启动：http://127.0.0.1:{config.port}/")
                        return child.wait()
                    raise LifecycleError("健康检查返回了错误的运行环境。")
                if child.poll() is not None:
                    return child.returncode or 0
                time.sleep(0.5)
            raise LifecycleError("开发服务在 30 秒内未就绪。")
        finally:
            os.close(guard_fd)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if record_bytes is not None:
            _remove_if_unchanged(runtime_fd, record_bytes)
        os.close(runtime_fd)


def stop(project_root: Path, home: Path) -> int:
    config = development_config(project_root=project_root, home=home)
    runtime_fd = _open_runtime(config, create=False)
    if runtime_fd is None:
        print("Audio Memory 开发环境未运行。")
        return 0
    try:
        try:
            raw_record = _read_record_bytes(runtime_fd)
            if raw_record is None:
                print("Audio Memory 开发环境未运行。")
                return 0
            record = _parse_record(
                raw_record, _server_argv(project_root, config.port), config.port
            )
            _validate_live_identity(record)
        except StaleProcessRecord as exc:
            _unlink_at(runtime_fd, PID_NAME)
            raise LifecycleError(f"{exc}已清理过期的 PID 记录。") from exc

        health_status, health_payload = _health(config.port)
        if health_status != "ok":
            raise LifecycleError("健康检查暂时不可用；已保留 PID 记录并拒绝停止。")
        if not health_payload or health_payload.get("profile") != "development":
            _unlink_at(runtime_fd, PID_NAME)
            raise LifecycleError("健康身份不是 development；已清理过期记录。")

        try:
            _validate_live_identity(record)
        except StaleProcessRecord as exc:
            _unlink_at(runtime_fd, PID_NAME)
            raise LifecycleError(f"{exc}已清理过期的 PID 记录。") from exc

        kill_command = shutil.which("kill") or "/bin/kill"
        result = _run([kill_command, "-TERM", "--", str(record.pid)])
        if result.returncode != 0:
            raise LifecycleError("无法向已验证的开发进程发送 TERM。")
        _remove_if_unchanged(runtime_fd, raw_record)
        print("Audio Memory 开发环境已停止。")
        return 0
    finally:
        os.close(runtime_fd)


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "start":
            return start(arguments.project_root.resolve(), arguments.home)
        return stop(arguments.project_root.resolve(), arguments.home)
    except (LifecycleError, OSError) as exc:
        print(f"{arguments.command} 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
