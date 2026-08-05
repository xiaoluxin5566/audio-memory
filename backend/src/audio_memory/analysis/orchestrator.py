from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy import select

from audio_memory.analysis.profile import validate_profile_delta
from audio_memory.analysis.publisher import AnalysisOutcome, AnalysisPublisher
from audio_memory.db import Database
from audio_memory.domain import JobStage
from audio_memory.models import AnalysisJob, JobFile, ProfileFact, Transcript
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.schemas import SceneResult
from audio_memory.prompts.store import PROMPT_SCENES, PromptStore


class SceneAnalyzer(Protocol):
    async def analyze(self, scene_id, request, provider_snapshot) -> SceneResult: ...


class ProfileExtractor(Protocol):
    async def extract(self, transcript, existing, provider_snapshot): ...


class AnalysisOrchestrator:
    def __init__(
        self,
        *,
        database: Database,
        prompt_store: PromptStore,
        analyzer: SceneAnalyzer,
        profile_extractor: ProfileExtractor,
        publisher: AnalysisPublisher,
    ) -> None:
        self.database = database
        self.prompt_store = prompt_store
        self.analyzer = analyzer
        self.profile_extractor = profile_extractor
        self.publisher = publisher
        self.composer = PromptComposer()

    async def run(
        self, job_id: str, provider_snapshot: dict[str, str]
    ) -> AnalysisOutcome:
        job = await self._prepare_job(job_id, provider_snapshot)
        transcript = await self._transcript(job_id)
        existing_profile = await self._profile()
        staged = self._load_staged(job.staged_results_json)
        try:
            for scene_id in PROMPT_SCENES:
                if scene_id in staged:
                    continue
                prompt = self.prompt_store.get(scene_id)
                request = self.composer.compose(
                    scene_id,
                    transcript=transcript,
                    profile=existing_profile,
                    prompt=prompt,
                )
                result = await self.analyzer.analyze(
                    scene_id, request, provider_snapshot
                )
                if result.scene_id != scene_id:
                    raise ValueError("Analyzer returned a mismatched scene")
                staged[scene_id] = result.model_dump(mode="json")
                await self._save_staged(job_id, staged)
            raw_delta = await self.profile_extractor.extract(
                transcript, existing_profile, provider_snapshot
            )
            delta = validate_profile_delta(raw_delta)
            results = [SceneResult.model_validate(staged[item]) for item in PROMPT_SCENES]
            return await self.publisher.publish(job_id, results, delta)
        except BaseException:
            await self._fail(job_id)
            raise

    async def _prepare_job(
        self, job_id: str, provider_snapshot: dict[str, str]
    ) -> AnalysisJob:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            provider_changed = job.provider_id != provider_snapshot["provider_id"]
            job.provider_id = provider_snapshot["provider_id"]
            job.model_id = provider_snapshot["model_id"]
            job.stage = JobStage.ANALYZING.value
            job.error_code = None
            if provider_changed:
                job.staged_results_json = "[]"
            await session.commit()
            await session.refresh(job)
            return job

    async def _transcript(self, job_id: str) -> str:
        async with self.database.session() as session:
            rows = await session.execute(
                select(Transcript.text)
                .join(JobFile, JobFile.id == Transcript.job_file_id)
                .where(JobFile.job_id == job_id)
                .order_by(JobFile.position, Transcript.segment_index)
            )
            texts = [row[0] for row in rows]
        if not texts:
            raise ValueError("Analysis requires a completed transcript")
        return "\n".join(texts)

    async def _profile(self) -> list[dict[str, object]]:
        async with self.database.session() as session:
            rows = list(
                await session.scalars(
                    select(ProfileFact).where(ProfileFact.status == "active")
                )
            )
        return [
            {
                "dimension": row.dimension,
                "value": json.loads(row.value_json),
                "confidence": row.confidence,
            }
            for row in rows
        ]

    async def _save_staged(
        self, job_id: str, staged: dict[str, dict[str, object]]
    ) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is None:
                raise LookupError(f"Unknown analysis job: {job_id}")
            job.staged_results_json = json.dumps(staged, ensure_ascii=False)
            await session.commit()

    async def _fail(self, job_id: str) -> None:
        async with self.database.session() as session:
            job = await session.get(AnalysisJob, job_id)
            if job is not None:
                job.stage = JobStage.FAILED.value
                job.error_code = "model_analysis_failed"
                await session.commit()

    @staticmethod
    def _load_staged(raw: str) -> dict[str, dict[str, object]]:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

