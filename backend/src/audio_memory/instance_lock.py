from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another backend process holds the kernel lock."""


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self.acquired:
            return

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        lock_file = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise InstanceAlreadyRunningError("Audio Memory 服务已运行。") from exc

        diagnostic = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(diagnostic, lock_file, ensure_ascii=False)
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        lock_file = self._file
        self._file = None
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

