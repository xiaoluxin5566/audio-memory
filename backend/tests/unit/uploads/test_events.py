import pytest

from audio_memory.api.events import JobEventBroker


@pytest.mark.asyncio
async def test_job_event_ids_are_monotonic_and_resume_after_cursor() -> None:
    broker = JobEventBroker()
    await broker.emit("job-1", "upload.completed", {"file_id": "a"})
    await broker.emit("job-1", "upload.completed", {"file_id": "b"})

    stream = broker.stream("job-1", after=1)
    event = await anext(stream)
    await stream.aclose()

    assert "id: 2" in event
    assert '"file_id": "b"' in event
