from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from audio_memory.config import AppPaths, PinnedDevelopmentRoot
from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, JobFile, TempFileManifest, Transcript
from audio_memory.runtime_tools import RuntimeToolUnavailable
from audio_memory.transcription.segments import progress_percent
from audio_memory.transcription.eta import TranscriptionEtaTracker
from audio_memory.uploads.cleanup import remove_staged_file
from audio_memory.uploads.probe import probe_audio, supports
from audio_memory.api.events import JobEventBroker


FORMAT_MESSAGE = "不支持该文件格式，请上传 MP3/AAC 格式文件"
RUNTIME_MESSAGE = "本地音频组件不可用，请重新安装 Audio Memory"


class UploadError(RuntimeError):
    def __init__(self, message: str, *, code: str, file_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.file_id = file_id


@dataclass(frozen=True, slots=True)
class UploadedFileView:
    id: str
    job_id: str
    original_name: str
    extension: str
    size_bytes: int
    duration_ms: int | None
    recording_started_at: str | None
    recording_time_source: str
    timezone: str | None
    position: int
    upload_progress: int = 100


@dataclass(frozen=True, slots=True)
class UploadJobView:
    id: str
    stage: str
    error_code: str | None
    provider_id: str | None
    model_id: str | None
    files: list[UploadedFileView]
    progress_percent: int = 0
    live_progress_percent: float = 0.0
    eta_state: str = "unavailable"
    eta_seconds: int | None = None
    local_phase: str | None = None
    batch_current: int = 0
    batch_total: int = 0


class UploadService:
    def __init__(
        self,
        database: Database,
        paths: AppPaths,
        events: JobEventBroker | None = None,
        eta_tracker: TranscriptionEtaTracker | None = None,
        write_boundary: PinnedDevelopmentRoot | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.events = events
        self.eta_tracker = eta_tracker or TranscriptionEtaTracker()
        self.write_boundary = write_boundary

    async def create_job(self) -> AnalysisJob:
        job = AnalysisJob(id=str(uuid4()), stage=JobStage.UPLOADING.value)
        async with self.database.session() as session:
            session.add(job)
            await session.commit()
            await session.refresh(job)
        job_folder = self.paths.staging / job.id
        if self.write_boundary is None:
            job_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            folder_fd = self.write_boundary.open_directory(job_folder, create=True)
            assert folder_fd is not None
            try:
                os.fchmod(folder_fd, 0o700)
            finally:
                os.close(folder_fd)
        await self._emit(job.id, "job.created", {"stage": job.stage})
        return job

    async def get_job(self, job_id: str) -> UploadJobView:
        job = await self._get_job(job_id)
        async with self.database.session() as session:
            files = await session.scalars(
                select(JobFile)
                .where(JobFile.job_id == job_id)
                .order_by(JobFile.position)
            )
            file_rows = list(files)
            processed_ms = 0
            for item in file_rows:
                processed_ms += int(
                    await session.scalar(
                        select(func.max(Transcript.end_ms)).where(
                            Transcript.job_file_id == item.id
                        )
                    )
                    or 0
                )
            total_ms = sum(int(item.duration_ms or 0) for item in file_rows)
            eta_seconds = None
            eta_state = "unavailable"
            local_progress = self.eta_tracker.progress(job.id)
            durable_progress = progress_percent(
                processed_ms=processed_ms, total_ms=total_ms
            )
            live_progress = float(durable_progress)
            active_file_id = self.eta_tracker.active_file(job.id)
            if local_progress and active_file_id and total_ms > 0:
                active_index = next(
                    (index for index, item in enumerate(file_rows) if item.id == active_file_id),
                    None,
                )
                current, total = local_progress[1], local_progress[2]
                if active_index is not None and current > 0 and total > 0:
                    prior_ms = sum(int(item.duration_ms or 0) for item in file_rows[:active_index])
                    active_ms = int(file_rows[active_index].duration_ms or 0)
                    batch_fraction = min(
                        1.0,
                        max(
                            0.0,
                            (current - 1 + self.eta_tracker.phase_fraction(job.id)) / total,
                        ),
                    )
                    live_processed_ms = prior_ms + active_ms * batch_fraction
                    live_progress = max(
                        live_progress,
                        min(100.0, live_processed_ms / total_ms * 100),
                    )
            if job.stage == JobStage.TRANSCRIBING.value:
                eta_seconds = self.eta_tracker.estimate_seconds(
                    job.id, max(0, total_ms - processed_ms)
                )
                eta_state = "ready" if eta_seconds is not None else "estimating"
            return UploadJobView(
                id=job.id,
                stage=job.stage,
                error_code=job.error_code,
                provider_id=job.provider_id,
                model_id=job.model_id,
                files=[self._view(item) for item in file_rows],
                progress_percent=durable_progress,
                live_progress_percent=round(live_progress, 3),
                eta_state=eta_state,
                eta_seconds=eta_seconds,
                local_phase=local_progress[0] if local_progress else None,
                batch_current=local_progress[1] if local_progress else 0,
                batch_total=local_progress[2] if local_progress else 0,
            )

    async def get_active_job(self) -> UploadJobView | None:
        recoverable_stages = {
            JobStage.TRANSCRIBING.value,
            JobStage.ANALYZING.value,
            JobStage.INTERRUPTED.value,
            JobStage.FAILED.value,
        }
        async with self.database.session() as session:
            job_id = await session.scalar(
                select(AnalysisJob.id)
                .where(AnalysisJob.stage.in_(recoverable_stages))
                .order_by(AnalysisJob.updated_at.desc(), AnalysisJob.created_at.desc())
                .limit(1)
            )
        return await self.get_job(job_id) if job_id else None

    async def upload(
        self,
        job_id: str,
        upload: UploadFile,
        *,
        file_modified: int | None = None,
        timezone: str | None = None,
    ) -> UploadedFileView:
        job = await self._get_job(job_id)
        if job.stage != JobStage.UPLOADING.value:
            raise UploadError("This job no longer accepts files", code="job_locked")
        if job.error_code == "unsupported_format":
            raise UploadError(FORMAT_MESSAGE, code="batch_paused")

        file_id = str(uuid4())
        original_name = Path(upload.filename or "audio").name
        extension = Path(original_name).suffix.casefold()
        target = self.paths.staging / job_id / f"{file_id}{extension or '.upload'}"
        manifest_id = str(uuid4())
        await self._register_manifest(manifest_id, job_id, target)

        digest = hashlib.sha256()
        size = 0
        try:
            if self.write_boundary is None:
                output_context = target.open("xb")
            else:
                output_fd = self.write_boundary.open_regular_file(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    create_parents=False,
                )
                output_context = os.fdopen(output_fd, "wb")
            with output_context as output:
                while chunk := await upload.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            await self._remove_manifest_and_file(manifest_id, target)
            raise
        finally:
            await upload.close()

        position = await self._next_position(job_id)
        try:
            probe = await probe_audio(target) if extension in {".mp3", ".aac"} else None
        except RuntimeToolUnavailable as exc:
            await self._remove_manifest_and_file(manifest_id, target)
            raise UploadError(RUNTIME_MESSAGE, code="audio_runtime_unavailable") from exc
        accepted = supports(extension, probe)
        recording_started_at = probe.creation_time if probe else None
        recording_time_source = "embedded" if recording_started_at else "unknown"
        if recording_started_at is None and file_modified is not None:
            try:
                recording_started_at = datetime.fromtimestamp(
                    file_modified / 1000, UTC
                ).isoformat(timespec="seconds")
            except (OSError, OverflowError, ValueError):
                recording_started_at = None
            else:
                recording_time_source = "file_modified"
        record = JobFile(
            id=file_id,
            job_id=job_id,
            original_name=original_name,
            extension=extension,
            mime_type=upload.content_type,
            size_bytes=size,
            sha256=digest.hexdigest(),
            duration_ms=probe.duration_ms if accepted and probe else None,
            recording_started_at=recording_started_at,
            recording_time_source=recording_time_source,
            timezone=timezone,
            position=position,
            temporary_path=str(target),
        )
        try:
            async with self.database.session() as session:
                session.add(record)
                if not accepted:
                    stored_job = await session.get(AnalysisJob, job_id)
                    if stored_job is not None:
                        stored_job.error_code = "unsupported_format"
                await session.commit()
        except IntegrityError as exc:
            await self._remove_manifest_and_file(manifest_id, target)
            raise UploadError("该音频已在本批次中", code="duplicate_file") from exc

        if not accepted:
            await self._emit(
                job_id,
                "upload.rejected",
                {"file_id": file_id, "message": FORMAT_MESSAGE},
            )
            raise UploadError(FORMAT_MESSAGE, code="unsupported_format", file_id=file_id)
        await self._emit(
            job_id,
            "upload.completed",
            {"file_id": file_id, "position": position, "progress": 100},
        )
        return self._view(record)

    async def remove_file(self, job_id: str, file_id: str) -> None:
        async with self.database.session() as session:
            async with session.begin():
                record = await session.get(JobFile, file_id)
                if record is None or record.job_id != job_id:
                    raise LookupError("Unknown upload file")
                path = Path(record.temporary_path)
                await session.delete(record)
                job = await session.get(AnalysisJob, job_id)
                if job is not None and job.error_code == "unsupported_format":
                    remaining_invalid = await session.scalar(
                        select(func.count(JobFile.id)).where(
                            JobFile.job_id == job_id,
                            JobFile.duration_ms.is_(None),
                        )
                    )
                    if remaining_invalid == 0:
                        job.error_code = None
                manifest = await session.scalar(
                    select(TempFileManifest).where(
                        TempFileManifest.file_path == str(path)
                    )
                )
                remove_staged_file(
                    path,
                    self.paths.staging,
                    write_boundary=self.write_boundary,
                )
                if manifest is not None:
                    await session.delete(manifest)
        await self._emit(job_id, "upload.removed", {"file_id": file_id})

    async def start(self, job_id: str, *, provider_id: str, model_id: str) -> UploadJobView:
        async with self.database.session() as session:
            async with session.begin():
                job = await session.get(AnalysisJob, job_id)
                if job is None:
                    raise LookupError("Unknown upload job")
                if job.error_code:
                    raise UploadError(FORMAT_MESSAGE, code="batch_paused")
                file_count = await session.scalar(
                    select(func.count(JobFile.id)).where(JobFile.job_id == job_id)
                )
                if not file_count:
                    raise UploadError("请先上传音频文件", code="empty_batch")
                if job.stage != JobStage.UPLOADING.value:
                    raise UploadError("This job has already started", code="job_locked")
                job.provider_id = provider_id
                job.model_id = model_id
                job.stage = JobStage.TRANSCRIBING.value
        await self._emit(
            job_id,
            "job.started",
            {"stage": JobStage.TRANSCRIBING.value, "provider_id": provider_id},
        )
        return await self.get_job(job_id)

    async def cancel_job(self, job_id: str) -> None:
        self.eta_tracker.clear(job_id)
        async with self.database.session() as session:
            async with session.begin():
                job = await session.get(AnalysisJob, job_id)
                if job is None:
                    raise LookupError("Unknown upload job")
                files = list(
                    await session.scalars(
                        select(JobFile).where(JobFile.job_id == job_id)
                    )
                )
                for file in files:
                    remove_staged_file(
                        Path(file.temporary_path),
                        self.paths.staging,
                        write_boundary=self.write_boundary,
                    )
                manifests = list(
                    await session.scalars(
                        select(TempFileManifest).where(
                            TempFileManifest.task_uuid == job_id
                        )
                    )
                )
                for manifest in manifests:
                    path = Path(manifest.file_path)
                    remove_staged_file(
                        path,
                        self.paths.staging,
                        write_boundary=self.write_boundary,
                    )
                    await session.delete(manifest)
                await session.delete(job)
        await self._emit(job_id, "job.cancelled", {})

    async def _get_job(self, job_id: str) -> AnalysisJob:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError("Unknown upload job")
            return job

    async def _next_position(self, job_id: str) -> int:
        async with self.database.session() as session:
            maximum = await session.scalar(
                select(func.max(JobFile.position)).where(JobFile.job_id == job_id)
            )
            return int(maximum if maximum is not None else -1) + 1

    async def _register_manifest(
        self, manifest_id: str, job_id: str, target: Path
    ) -> None:
        async with self.database.session() as session:
            session.add(
                TempFileManifest(
                    id=manifest_id,
                    task_uuid=job_id,
                    file_path=str(target),
                )
            )
            await session.commit()

    async def _remove_manifest_and_file(self, manifest_id: str, target: Path) -> None:
        remove_staged_file(
            target,
            self.paths.staging,
            write_boundary=self.write_boundary,
        )
        async with self.database.session() as session:
            manifest = await session.get(TempFileManifest, manifest_id)
            if manifest is not None:
                await session.delete(manifest)
                await session.commit()

    @staticmethod
    def _view(record: JobFile) -> UploadedFileView:
        return UploadedFileView(
            id=record.id,
            job_id=record.job_id,
            original_name=record.original_name,
            extension=record.extension,
            size_bytes=record.size_bytes,
            duration_ms=record.duration_ms,
            recording_started_at=record.recording_started_at,
            recording_time_source=record.recording_time_source or "unknown",
            timezone=record.timezone,
            position=record.position,
        )

    async def _emit(
        self, job_id: str, event: str, data: dict[str, object]
    ) -> None:
        if self.events is not None:
            await self.events.emit(job_id, event, data)
