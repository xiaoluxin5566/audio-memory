from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_memory.asr.client import (
    AsrProviderError,
    VolcanoAsrClient,
    VolcanoSubmission,
)
from audio_memory.asr.credentials import ASR_KEYCHAIN_ID
from audio_memory.asr.normalizer import normalize_volcano_result
from audio_memory.asr.repository import AsrRepository
from audio_memory.asr.storage import (
    ManagedOssClient,
    StorageAuthorizationError,
    UploadRequest,
)
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, JobFile
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


class AsrKeychain(Protocol):
    def read(self, credential_id: str) -> KeychainReadResult: ...


class AnalysisSubmitter(Protocol):
    async def submit_new_upload(self, request: object) -> str: ...


@dataclass(slots=True)
class VolcanoAsrCoordinator:
    database: Database
    runtime_root: Path
    repository: AsrRepository
    storage: ManagedOssClient
    volcano: VolcanoAsrClient
    keychain: AsrKeychain
    submit_concurrency: int = 2
    poll_interval_seconds: float = 2.0
    max_attempts: int = 3
    retry_base_seconds: float = 1.0

    async def run_job(
        self,
        *,
        job_id: str,
        analysis_request: object,
        analysis_submitter: AnalysisSubmitter,
    ) -> str:
        job, files = await self._job_and_files(job_id)
        if job.stage in {"analyzing", "ready_to_commit", "completed"}:
            return job.stage
        if job.stage != "transcribing":
            raise ValueError("cloud ASR job must be transcribing")

        tasks = []
        for file in files:
            source = Path(file.temporary_path).resolve()
            try:
                relative_source = source.relative_to(self.runtime_root.resolve())
            except ValueError as exc:
                raise ValueError("ASR source escaped the runtime root") from exc
            tasks.append(
                await self.repository.ensure_file_task(
                    job_id=job_id,
                    job_file_id=file.id,
                    relative_source_path=relative_source.as_posix(),
                    sha256=file.sha256,
                )
            )

        semaphore = asyncio.Semaphore(self.submit_concurrency)

        async def complete(task_id: str) -> None:
            async with semaphore:
                await self._complete_with_retry(task_id)

        try:
            await asyncio.gather(*(complete(task.id) for task in tasks))
            await analysis_submitter.submit_new_upload(analysis_request)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._mark_job_failed(job_id)
            raise
        await self._mark_job_analyzing(job_id)
        return "analyzing"

    async def _complete_with_retry(self, task_id: str) -> None:
        failures = 0
        while True:
            try:
                status = await self.advance_task(task_id)
            except (AsrProviderError, StorageAuthorizationError) as exc:
                if not exc.retriable or failures + 1 >= self.max_attempts:
                    raise
                failures += 1
                await asyncio.sleep(
                    min(self.retry_base_seconds * 2 ** (failures - 1), 4)
                )
                continue
            if status == "completed":
                return
            if status == "submission_unknown":
                raise AsrProviderError("submission_unknown", retriable=False)
            await asyncio.sleep(self.poll_interval_seconds)

    async def advance_task(self, task_id: str) -> str:
        task = await self.repository.get(task_id)
        if task.status == "submission_unknown":
            return "submission_unknown"
        file = await self._job_file(task.job_file_id)

        if task.status == "completed":
            await self._materialize_and_cleanup(task, file)
            return "completed"

        source = self._source_path(task.relative_source_path)
        await self._verify_source(source, expected_sha256=task.sha256)
        if task.storage_status == "pending":
            ticket = await self.storage.create_upload(
                UploadRequest(
                    content_type=file.mime_type or self._content_type(file.extension),
                    size_bytes=file.size_bytes,
                    sha256=file.sha256,
                )
            )
            await self.storage.upload_file(ticket, source)
            await self.repository.mark_storage_uploaded(task.id, ticket.object_id)
            task = await self.repository.get(task.id)

        api_key = self._api_key()
        if task.remote_task_id is None:
            if task.storage_object_id is None:
                raise RuntimeError("uploaded ASR task has no storage object")
            read_ticket = await self.storage.create_read_url(task.storage_object_id)
            try:
                remote_task_id = await self.volcano.submit(
                    api_key=api_key,
                    request=VolcanoSubmission(
                        request_id=task.request_id,
                        signed_url=read_ticket.url,
                        audio_format=file.extension.removeprefix("."),
                    ),
                )
            except AsrProviderError as exc:
                if exc.code in {"timeout", "network_error"}:
                    await self.repository.mark_submission_unknown(task.id, exc.code)
                raise
            await self.repository.mark_submitted(task.id, remote_task_id)
            task = await self.repository.get(task.id)

        assert task.remote_task_id is not None
        result = await self.volcano.poll(
            api_key=api_key,
            task_id=task.remote_task_id,
        )
        if not result.completed:
            return "polling"
        if result.payload is None:
            raise RuntimeError("completed ASR task has no result")
        result_json = json.dumps(
            result.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        await self.repository.mark_completed(task.id, result_json)
        task = await self.repository.get(task.id)
        await self._materialize_and_cleanup(task, file)
        return "completed"

    async def _materialize_and_cleanup(self, task, file: JobFile) -> None:
        if task.materialized_at is None:
            if task.result_json is None or file.duration_ms is None:
                raise RuntimeError("completed ASR task is missing result metadata")
            payload = json.loads(task.result_json)
            segments = normalize_volcano_result(
                file_id=file.id,
                duration_ms=file.duration_ms,
                payload=payload,
            )
            await self.repository.materialize(task.id, segments)
            task = await self.repository.get(task.id)
        if task.storage_status == "uploaded":
            if task.storage_object_id is None:
                raise RuntimeError("uploaded ASR task has no storage object")
            await self.storage.delete(task.storage_object_id)
            await self.repository.mark_storage_deleted(task.id)

    async def _job_file(self, file_id: str) -> JobFile:
        async with self.database.session() as session:
            return await self._require_file(session, file_id)

    async def _job_and_files(
        self, job_id: str
    ) -> tuple[AnalysisJob, list[JobFile]]:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(job_id)
            files = list(
                await session.scalars(
                    select(JobFile)
                    .where(JobFile.job_id == job_id)
                    .order_by(JobFile.position)
                )
            )
            if not files:
                raise ValueError("cloud ASR job has no files")
            return job, files

    async def _mark_job_analyzing(self, job_id: str) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(job_id)
            if job.stage == "transcribing":
                job.stage = "analyzing"
                job.error_code = None
                await session.commit()

    async def _mark_job_failed(self, job_id: str) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is not None and job.stage == "transcribing":
                job.stage = "failed"
                job.error_code = "cloud_asr_failed"
                await session.commit()

    @staticmethod
    async def _require_file(session: AsyncSession, file_id: str) -> JobFile:
        file = await session.get(JobFile, file_id)
        if file is None:
            raise LookupError(file_id)
        return file

    def _source_path(self, relative_path: str) -> Path:
        root = self.runtime_root.resolve()
        source = (root / relative_path).resolve()
        if not source.is_relative_to(root):
            raise ValueError("ASR source escaped the runtime root")
        return source

    @staticmethod
    async def _verify_source(source: Path, *, expected_sha256: str) -> None:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("ASR source hash changed")

    def _api_key(self) -> bytes:
        stored = self.keychain.read(ASR_KEYCHAIN_ID)
        if stored.status is not KeychainStatus.CONFIGURED or stored.secret is None:
            raise AsrProviderError("invalid_api_key", retriable=False)
        return stored.secret

    @staticmethod
    def _content_type(extension: str) -> str:
        return {".mp3": "audio/mpeg", ".aac": "audio/aac"}[extension]
