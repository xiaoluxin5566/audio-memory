from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from audio_memory.power.sleep_prevention import SleepPreventionManager
from audio_memory.api.jobs import protect_job_if_enabled


class FakeProcess:
    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.waited = False
        self._exited = asyncio.Event()
        if returncode is not None:
            self._exited.set()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self._exited.set()

    async def wait(self) -> int:
        self.waited = True
        await self._exited.wait()
        return self.returncode or 0

    def exit_unexpectedly(self) -> None:
        self.returncode = 1
        self._exited.set()


@pytest.mark.asyncio
async def test_one_caffeinate_process_covers_all_active_analysis_jobs() -> None:
    processes: list[FakeProcess] = []
    calls: list[tuple[str, ...]] = []

    async def spawn(*command: str) -> FakeProcess:
        calls.append(command)
        process = FakeProcess()
        processes.append(process)
        return process

    manager = SleepPreventionManager(spawn=spawn, platform_name="Darwin")

    assert await manager.acquire("job-1") == "active"
    assert await manager.acquire("job-2") == "active"
    await manager.release("job-1")

    assert calls == [("/usr/bin/caffeinate", "-i")]
    assert processes[0].terminated is False

    await manager.release("job-2")

    assert processes[0].terminated is True
    assert processes[0].waited is True


@pytest.mark.asyncio
async def test_duplicate_job_acquire_does_not_leak_sleep_prevention() -> None:
    process = FakeProcess()

    async def spawn(*_command: str) -> FakeProcess:
        return process

    manager = SleepPreventionManager(spawn=spawn, platform_name="Darwin")

    await manager.acquire("job-1")
    await manager.acquire("job-1")
    await manager.release("job-1")

    assert process.terminated is True


@pytest.mark.asyncio
async def test_spawn_failure_degrades_without_blocking_analysis() -> None:
    async def spawn(*_command: str) -> FakeProcess:
        raise OSError("caffeinate unavailable")

    manager = SleepPreventionManager(spawn=spawn, platform_name="Darwin")

    assert await manager.acquire("job-1") == "unavailable"
    assert manager.status == "unavailable"
    await manager.release("job-1")


@pytest.mark.asyncio
async def test_close_releases_assertion_even_with_active_jobs() -> None:
    process = FakeProcess()

    async def spawn(*_command: str) -> FakeProcess:
        return process

    manager = SleepPreventionManager(spawn=spawn, platform_name="Darwin")
    await manager.acquire("job-1")

    await manager.close()

    assert process.terminated is True
    assert manager.status == "inactive"


@pytest.mark.asyncio
async def test_unexpected_caffeinate_exit_restarts_once_for_the_active_job() -> None:
    processes = [FakeProcess(), FakeProcess()]

    async def spawn(*_command: str) -> FakeProcess:
        return processes.pop(0)

    manager = SleepPreventionManager(spawn=spawn, platform_name="Darwin")
    await manager.acquire("job-1")
    first = manager._process
    assert isinstance(first, FakeProcess)

    first.exit_unexpectedly()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert manager.status == "active"
    assert manager._process is not first
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "expected_status", "expected_jobs"),
    [(False, "disabled", []), (True, "active", ["job-1"])],
)
async def test_job_protection_follows_persisted_user_setting(
    enabled: bool, expected_status: str, expected_jobs: list[str]
) -> None:
    class Settings:
        async def prevent_sleep_enabled(self) -> bool:
            return enabled

    class Prevention:
        def __init__(self) -> None:
            self.jobs: list[str] = []

        async def acquire(self, job_id: str) -> str:
            self.jobs.append(job_id)
            return "active"

    prevention = Prevention()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings_repository=Settings(), sleep_prevention=prevention
            )
        )
    )

    status = await protect_job_if_enabled(request, "job-1")

    assert status == expected_status
    assert prevention.jobs == expected_jobs
