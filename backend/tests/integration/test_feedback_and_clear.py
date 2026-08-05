from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_memory.content.clear import HistoryCleaner
from audio_memory.content.feedback import FeedbackWriter
from audio_memory.db import Database
from audio_memory.models import FeedbackIndex, ProviderMetadata


@pytest.mark.asyncio
async def test_feedback_file_contains_scene_audio_transcript_and_complete_qa(tmp_path: Path) -> None:
    database = Database(tmp_path / "feedback.sqlite3")
    await database.create_schema()
    folder = tmp_path / "意见反馈"
    writer = FeedbackWriter(database, folder)

    record = await writer.write(
        card_id="card-1",
        scene_id="parenting",
        rating="inaccurate",
        explanation="原因判断不对",
        audio=[{"name": "家庭录音.mp3", "duration_ms": 1000}],
        transcript="完整原文",
        qa=[{"role": "user", "content": "为什么？"}, {"role": "assistant", "content": "回答"}],
    )

    payload = json.loads(Path(record.file_path).read_text())
    assert payload["scene_id"] == "parenting"
    assert payload["audio"][0]["name"] == "家庭录音.mp3"
    assert payload["transcript"] == "完整原文"
    assert len(payload["qa"]) == 2
    await database.dispose()


@pytest.mark.asyncio
async def test_clear_history_preserves_provider_prompts_and_feedback(tmp_path: Path) -> None:
    database = Database(tmp_path / "clear.sqlite3")
    await database.create_schema()
    prompts = tmp_path / "prompts"
    feedback = tmp_path / "意见反馈"
    audio = tmp_path / "audio"
    for folder in (prompts, feedback, audio):
        folder.mkdir()
    (prompts / "keep.md").write_text("prompt")
    (feedback / "keep.json").write_text("{}")
    (audio / "delete.mp3").write_bytes(b"audio")
    async with database.session() as session:
        session.add(ProviderMetadata(provider_id="kimi", validation_status="available"))
        session.add(FeedbackIndex(id="feedback-1", scene_id="meeting", file_path=str(feedback / "keep.json"), rating="accurate"))
        await session.commit()

    await HistoryCleaner(database, audio).clear(confirm=True)

    assert list(audio.iterdir()) == []
    assert (prompts / "keep.md").exists()
    assert (feedback / "keep.json").exists()
    async with database.session() as session:
        assert await session.get(ProviderMetadata, "kimi") is not None
        assert await session.get(FeedbackIndex, "feedback-1") is not None
    await database.dispose()
