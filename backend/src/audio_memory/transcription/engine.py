from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
import logging
from pathlib import Path
import shutil
import time
from uuid import uuid4

from audio_memory.config import AppPaths, WHISPER_MODEL_ID
from audio_memory.db import Database
from audio_memory.models import JobFile, TempFileManifest
from audio_memory.transcription.segments import TranscriptSegment
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.uploads.cleanup import assert_staging_path, remove_staged_file


WHISPER_CHUNK_SECONDS = 300
CHUNK_SEGMENT_STRIDE = 10_000
logger = logging.getLogger(__name__)


def _transcribe_worker(audio_path: str, model_id: str) -> list[dict[str, object]]:
    import mlx_whisper

    result = mlx_whisper.transcribe(audio_path, path_or_hf_repo=model_id)
    segments = result.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("Whisper returned an invalid segment list")
    return segments


def chunk_segment(*, file_id: str, chunk_index: int, chunk_seconds: int,
                  local_index: int, raw: dict[str, object]) -> TranscriptSegment:
    offset_seconds = chunk_index * chunk_seconds
    words = raw.get("words", [])
    shifted_words: list[dict[str, object]] = []
    if isinstance(words, list):
        for word in words:
            if not isinstance(word, dict):
                continue
            shifted = dict(word)
            for key in ("start", "end"):
                if isinstance(shifted.get(key), (int, float)):
                    shifted[key] = float(shifted[key]) + offset_seconds
            shifted_words.append(shifted)
    return TranscriptSegment(
        file_id=file_id,
        index=chunk_index * CHUNK_SEGMENT_STRIDE + local_index,
        start_ms=round((offset_seconds + float(raw.get("start", 0))) * 1000),
        end_ms=round((offset_seconds + float(raw.get("end", 0))) * 1000),
        text=str(raw.get("text", "")),
        words=shifted_words,
    )


def valid_chunk_segments(*, file_id: str, chunk_index: int, chunk_seconds: int,
                         raw_segments: list[dict[str, object]]):
    for local_index, raw in enumerate(raw_segments):
        try:
            yield chunk_segment(
                file_id=file_id, chunk_index=chunk_index,
                chunk_seconds=chunk_seconds, local_index=local_index, raw=raw,
            )
        except (TypeError, ValueError):
            logger.warning(
                "Skipping invalid Whisper segment file=%s chunk=%s segment=%s",
                file_id, chunk_index, local_index,
            )


class MLXWhisperEngine:
    def __init__(
        self,
        database: Database,
        paths: AppPaths,
        *,
        model_id: str = WHISPER_MODEL_ID,
        eta_tracker: TranscriptionEtaTracker | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.model_id = model_id
        self.eta_tracker = eta_tracker or TranscriptionEtaTracker()
        self._executor: ProcessPoolExecutor | None = None

    async def transcribe_file(self, file: JobFile, resume_from: int):
        source = Path(file.temporary_path)
        chunk_dir = source.with_name(f"{file.id}.whisper-chunks")
        manifest_id = str(uuid4())
        await self._register(file.job_id, manifest_id, chunk_dir)
        try:
            chunks = await self._normalize_to_chunks(source, chunk_dir)
            loop = asyncio.get_running_loop()
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1)
            for chunk_index, chunk in enumerate(chunks):
                if (chunk_index + 1) * CHUNK_SEGMENT_STRIDE <= resume_from:
                    continue
                started = time.monotonic()
                raw_segments = await loop.run_in_executor(
                    self._executor, _transcribe_worker, str(chunk), self.model_id,
                )
                valid_count = 0
                for segment in valid_chunk_segments(
                    file_id=file.id, chunk_index=chunk_index,
                    chunk_seconds=WHISPER_CHUNK_SECONDS,
                    raw_segments=raw_segments,
                ):
                    if segment.index >= resume_from:
                        valid_count += 1
                        yield segment
                if valid_count:
                    audio_ms = min(
                        WHISPER_CHUNK_SECONDS * 1000,
                        max(
                            0,
                            int(file.duration_ms or 0)
                            - chunk_index * WHISPER_CHUNK_SECONDS * 1000,
                        ),
                    )
                    self.eta_tracker.record(
                        file.job_id, audio_ms, time.monotonic() - started
                    )
        finally:
            safe_dir = assert_staging_path(chunk_dir, self.paths.staging)
            if safe_dir.exists():
                shutil.rmtree(safe_dir)
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

    @staticmethod
    async def _normalize_to_chunks(source: Path, target_dir: Path) -> list[Path]:
        target_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        pattern = target_dir / "chunk-%05d.wav"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(source),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            "-f", "segment", "-segment_time", str(WHISPER_CHUNK_SECONDS),
            "-reset_timestamps", "1", "-y", str(pattern),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Audio chunking failed ({process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[-200:]}"
            )
        chunks = sorted(target_dir.glob("chunk-*.wav"))
        if not chunks:
            raise RuntimeError("Audio chunking produced no output")
        return chunks

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
