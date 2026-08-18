from __future__ import annotations

import asyncio
import logging
import platform
from collections.abc import Awaitable, Callable
from typing import Protocol


logger = logging.getLogger("uvicorn.error")


class Process(Protocol):
    returncode: int | None

    def terminate(self) -> None: ...

    async def wait(self) -> int: ...


Spawn = Callable[..., Awaitable[Process]]


async def _spawn(*command: str) -> Process:
    return await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


class SleepPreventionManager:
    """Hold one idle-system-sleep assertion while protected jobs are active."""

    def __init__(
        self,
        *,
        spawn: Spawn = _spawn,
        platform_name: str | None = None,
    ) -> None:
        self._spawn = spawn
        self._platform_name = platform_name or platform.system()
        self._jobs: set[str] = set()
        self._process: Process | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._restart_attempted = False
        self._unavailable = False
        self._lock = asyncio.Lock()

    @property
    def status(self) -> str:
        if self._unavailable:
            return "unavailable"
        if self._process is not None and self._process.returncode is None:
            return "active"
        return "inactive"

    async def acquire(self, job_id: str) -> str:
        async with self._lock:
            if job_id in self._jobs and self.status == "active":
                return "active"
            if not self._jobs:
                self._restart_attempted = False
            self._jobs.add(job_id)
            if self.status == "active":
                return "active"
            if self._platform_name != "Darwin":
                self._jobs.discard(job_id)
                self._unavailable = True
                return "unavailable"
            try:
                await self._start_process()
                return "active"
            except OSError as exc:
                self._jobs.discard(job_id)
                self._unavailable = True
                logger.warning(
                    "Sleep prevention unavailable error_type=%s", type(exc).__name__
                )
                return "unavailable"

    async def release(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.discard(job_id)
            if self._jobs or self._process is None:
                return
            await self._stop_process()

    async def close(self) -> None:
        async with self._lock:
            self._jobs.clear()
            if self._process is not None:
                await self._stop_process()
            self._unavailable = False

    async def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        await process.wait()

    async def _start_process(self) -> None:
        process = await self._spawn("/usr/bin/caffeinate", "-i")
        if process.returncode is not None:
            raise OSError("caffeinate exited before sleep prevention started")
        self._process = process
        self._unavailable = False
        self._watch_task = asyncio.create_task(self._watch(process))

    async def _watch(self, process: Process) -> None:
        await process.wait()
        async with self._lock:
            if self._process is not process:
                return
            self._process = None
            if not self._jobs:
                return
            if self._restart_attempted:
                self._unavailable = True
                return
            self._restart_attempted = True
            try:
                await self._start_process()
            except OSError as exc:
                self._unavailable = True
                logger.warning(
                    "Sleep prevention restart failed error_type=%s",
                    type(exc).__name__,
                )
