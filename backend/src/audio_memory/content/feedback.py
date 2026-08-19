from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from audio_memory.db import Database
from audio_memory.config import PinnedDevelopmentRoot
from audio_memory.models import FeedbackIndex


class FeedbackWriter:
    def __init__(
        self,
        database: Database,
        folder: Path,
        *,
        write_boundary: PinnedDevelopmentRoot | None = None,
    ) -> None:
        self.database = database
        self.folder = folder
        self.write_boundary = write_boundary

    async def write(self, *, card_id, scene_id, rating, explanation, audio, transcript, qa, card=None, provider_id=None, model_id=None, prompt_snapshot=None) -> FeedbackIndex:
        if rating not in {"accurate", "inaccurate"}:
            raise ValueError("Unsupported feedback rating")
        if rating == "inaccurate" and not str(explanation or "").strip():
            raise ValueError("内容不准时必须填写具体问题")
        date_folder = self.folder / datetime.now(UTC).date().isoformat()
        if self.write_boundary is None:
            date_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            date_fd = self.write_boundary.open_directory(
                date_folder, create=True
            )
            assert date_fd is not None
            try:
                os.fchmod(date_fd, 0o700)
            finally:
                os.close(date_fd)
        feedback_id = str(uuid4())
        path = date_folder / f"{feedback_id}.json"
        payload = {"id": feedback_id, "created_at": datetime.now(UTC).isoformat(), "card_id": card_id, "scene_id": scene_id, "provider_id": provider_id, "model_id": model_id, "prompt_snapshot": prompt_snapshot or {}, "rating": rating, "explanation": explanation, "generated_content": card, "audio": audio, "transcript": transcript, "qa": qa}
        if self.write_boundary is None:
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        else:
            self.write_boundary.write_text_atomic(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        record = FeedbackIndex(id=feedback_id, card_id=card_id, scene_id=scene_id, file_path=str(path), rating=rating)
        async with self.database.session() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
        return record
