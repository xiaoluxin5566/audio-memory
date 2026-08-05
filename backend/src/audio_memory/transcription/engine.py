from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from uuid import uuid4

from audio_memory.config import AppPaths, WHISPER_MODEL_ID
from audio_memory.db import Database
from audio_memory.models import JobFile, TempFileManifest
from audio_memory.transcription.segments import TranscriptSegment
from audio_memory.uploads.cleanup import remove_staged_file


def _transcribe_worker(audio_path: str, model_id: str) -> list[dict[str, object]]:
    import mlx_whisper

    result = mlx_whisper.transcribe(audio_path, path_or_hf_repo=model_id)
    segments = result.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("Whisper returned an invalid segment list")
    return segments


class MLXWhisperEngine:
    def __init__(
        self,
        database: Database,
        paths: AppPaths,
        *,
        model_id: str = WHISPER_MODEL_ID,
    ) -> None:
        self.database = database
        self.paths = paths
        self.model_id = model_id
        self._executor: ProcessPoolExecutor | None = None

    async def transcribe_file(self, file: JobFile, resume_from: int):
        source = Path(file.temporary_path)
        normalized = source.with_name(f"{file.id}.normalized.wav")
        manifest_id = str(uuid4())
        await self._register(file.job_id, manifest_id, normalized)
        try:
            await self._normalize(source, normalized)
            loop = asyncio.get_running_loop()
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1)
            raw_segments = await loop.run_in_executor(
                self._executor,
                _transcribe_worker,
                str(normalized),
                self.model_id,
            )
            for index, raw in enumerate(raw_segments):
                if index < resume_from:
                    continue
                start_ms = round(float(raw.get("start", 0)) * 1000)
                end_ms = round(float(raw.get("end", 0)) * 1000)
                words = raw.get("words", [])
                yield TranscriptSegment(
                    file_id=file.id,
                    index=index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=str(raw.get("text", "")),
                    words=words if isinstance(words, list) else [],
                )
        finally:
            remove_staged_file(normalized, self.paths.staging)
            await self._remove_manifest(manifest_id)

    async def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    @staticmethod
    async def _normalize(source: Path, target: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Audio normalization failed ({process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[-200:]}"
            )

    async def _register(self, job_id: str, manifest_id: str, path: Path) -> None:
        async with self.database.session() as session:
            session.add(
                TempFileManifest(
                    id=manifest_id,
                    task_uuid=job_id,
                    file_path=str(path),
                )
            )
            await session.commit()

    async def _remove_manifest(self, manifest_id: str) -> None:
        async with self.database.session() as session:
            record = await session.get(TempFileManifest, manifest_id)
            if record is not None:
                await session.delete(record)
                await session.commit()
