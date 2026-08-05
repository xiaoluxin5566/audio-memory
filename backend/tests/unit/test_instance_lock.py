from pathlib import Path

import pytest

from audio_memory.instance_lock import InstanceAlreadyRunningError, InstanceLock


def test_second_instance_cannot_acquire_the_same_kernel_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / "audio-memory.lock"
    first = InstanceLock(lock_path)
    second = InstanceLock(lock_path)

    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()


def test_released_kernel_lock_can_be_acquired_again(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / "audio-memory.lock"
    first = InstanceLock(lock_path)
    second = InstanceLock(lock_path)

    first.acquire()
    first.release()
    second.acquire()
    second.release()

