from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import delete, select

from audio_memory.analysis.profile import validate_profile_delta
from audio_memory.analysis.provider import ProviderAnalysisError
from audio_memory.analysis.publisher import AnalysisOutcome
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    JobFile,
    ProfileCandidate,
    ReanalysisBatch,
    ReanalysisItem,
    Transcript,
)
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.evidence import validate_evidence_integrity
from audio_memory.prompts.event_schema import EventMap
from audio_memory.prompts.schemas import (
    ContentSceneResult,
    GrowthSceneResult,
    InspirationSceneResult,
    MeetingSceneResult,
    ParentingSceneResult,
    SceneResultBase,
    TodoSceneResult,
)
from audio_memory.prompts.store import PROMPT_SCENES, PromptDocument


class CredentialChangedError(RuntimeError):
    pass


class FixedRulesChangedError(RuntimeError):
    pass


class StrictAnalysisProvider(Protocol):
    async def analyze_event_map(self, request, provider_snapshot) -> EventMap: ...

    async def analyze_scene(
        self, scene_id, request, provider_snapshot
    ) -> SceneResultBase: ...


class ProfileExtractor(Protocol):
    async def extract(self, transcript, existing, provider_snapshot): ...


class Publisher(Protocol):
    async def publish(self, version_id, results, profile_delta) -> AnalysisOutcome: ...


class GenerationSource(Protocol):
    async def credential_generation(self, provider_id: str) -> int: ...

    def publication_guard(self, provider_id: str): ...


_SCENE_MODELS = {
    "todo": TodoSceneResult,
    "meeting": MeetingSceneResult,
    "parenting": ParentingSceneResult,
    "content": ContentSceneResult,
    "growth": GrowthSceneResult,
    "inspiration": InspirationSceneResult,
}
_SCENE_ADAPTERS = {
    scene_id: TypeAdapter(model) for scene_id, model in _SCENE_MODELS.items()
}


class AnalysisRunner:
    def __init__(
        self,
        *,
        database: Database,
        provider: StrictAnalysisProvider,
        profile_extractor: ProfileExtractor,
        publisher: Publisher,
        generation_source: GenerationSource,
    ) -> None:
        self.database = database
        self.provider = provider
        self.profile_extractor = profile_extractor
        self.publisher = publisher
        self.generation_source = generation_source
        self.composer = PromptComposer()

    async def run(self, version_id: str) -> AnalysisOutcome:
        version = await self._version(version_id)
        await self._require_fixed_rules(version)
        provider_snapshot = {
            "provider_id": version.provider_id,
            "model_id": version.model_id,
            "credential_generation": version.credential_generation,
        }
        transcript = await self._transcript(version.source_job_id)
        segment_ids = {str(item["segment_id"]) for item in transcript}
        profile = self._json_list(version.profile_snapshot_json)
        prompts = self._json_object(version.prompt_snapshot_json)
        staged = self._json_object(version.staged_results_json)
        try:
            await self._require_generation(version)
            event_map = await self._event_map(
                version, transcript, profile, provider_snapshot
            )
            results: list[SceneResultBase] = []
            for scene_id in PROMPT_SCENES:
                adapter = _SCENE_ADAPTERS[scene_id]
                if scene_id in staged:
                    result = adapter.validate_python(staged[scene_id])
                else:
                    prompt = self._prompt_from_snapshot(scene_id, prompts)
                    request = self.composer.compose_scene(
                        scene_id,
                        transcript=transcript,
                        event_map=event_map,
                        profile=profile,
                        prompt=prompt,
                        schema=_SCENE_MODELS[scene_id].model_json_schema(),
                    )
                    await self._require_generation(version)
                    generated = await self.provider.analyze_scene(
                        scene_id, request, provider_snapshot
                    )
                    await self._require_generation(version)
                    result = adapter.validate_python(
                        generated.model_dump(mode="python")
                        if hasattr(generated, "model_dump")
                        else generated
                    )
                    validate_evidence_integrity(result, event_map, segment_ids)
                    staged[scene_id] = result.model_dump(mode="json")
                    await self._save_staged(version.id, staged)
                validate_evidence_integrity(result, event_map, segment_ids)
                results.append(result)

            await self._require_generation(version)
            raw_delta = await self.profile_extractor.extract(
                transcript, profile, provider_snapshot
            )
            await self._require_generation(version)
            verified_delta = await self._save_profile_candidates(
                version.id, raw_delta, segment_ids
            )
            delta = validate_profile_delta(verified_delta)
            async with self.generation_source.publication_guard(
                version.provider_id
            ) as final_generation:
                if final_generation != version.credential_generation:
                    await self._mark_credential_changed(version)
                outcome = await self.publisher.publish(version.id, results, delta)
            return outcome
        except asyncio.CancelledError:
            raise
        except FixedRulesChangedError:
            raise
        except CredentialChangedError:
            raise
        except ProviderAnalysisError:
            await self._require_generation(version)
            await self._mark_failed(version.id)
            raise
        except BaseException:
            await self._mark_failed(version.id)
            raise

    async def _event_map(
        self,
        version: AnalysisVersion,
        transcript: list[dict[str, object]],
        profile: list[dict[str, object]],
        provider_snapshot: dict[str, object],
    ) -> EventMap:
        if version.event_map_json:
            return EventMap.model_validate_json(version.event_map_json)
        request = self.composer.compose_event_map(
            transcript=transcript,
            profile=profile,
            schema=EventMap.model_json_schema(),
        )
        generated = await self.provider.analyze_event_map(request, provider_snapshot)
        await self._require_generation(version)
        event_map = EventMap.model_validate(
            generated.model_dump(mode="python")
            if hasattr(generated, "model_dump")
            else generated
        )
        transcript_ids = {str(item["segment_id"]) for item in transcript}
        covered = {
            segment_id
            for event in event_map.events
            for segment_id in event.evidence_segment_ids
        } | set(event_map.unassigned_segment_ids)
        unknown = covered - transcript_ids
        missing = transcript_ids - covered
        if unknown or missing:
            raise ValueError(
                f"Event map coverage mismatch; unknown={sorted(unknown)}, "
                f"missing={sorted(missing)}"
            )
        serialized = json.dumps(
            event_map.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.database.session() as session:
            current = await session.get(AnalysisVersion, version.id)
            if current is None:
                raise LookupError(f"Unknown analysis version: {version.id}")
            current.event_map_json = serialized
            current.event_map_hash = sha256(serialized.encode("utf-8")).hexdigest()
            await session.commit()
        version.event_map_json = serialized
        version.event_map_hash = sha256(serialized.encode("utf-8")).hexdigest()
        return event_map

    async def _transcript(self, job_id: str) -> list[dict[str, object]]:
        async with self.database.session() as session:
            rows = list(
                await session.scalars(
                    select(Transcript)
                    .join(JobFile, JobFile.id == Transcript.job_file_id)
                    .where(JobFile.job_id == job_id)
                    .order_by(JobFile.position, Transcript.segment_index)
                )
            )
            files = {
                item.id: item
                for item in await session.scalars(
                    select(JobFile).where(JobFile.job_id == job_id)
                )
            }
        if not rows:
            raise ValueError("Analysis requires a completed transcript")
        file_positions = {file.id: file.position for file in files.values()}
        structured: list[dict[str, object]] = []
        for row in rows:
            file = files[row.job_file_id]
            started = file.recording_started_at
            local_date = started[:10] if started else None
            structured.append(
                {
                    "segment_id": f"seg_{file_positions[file.id]}_{row.segment_index}",
                    "file_id": file.id,
                    "file_name": file.original_name,
                    "recording_started_at": started,
                    "local_date": local_date,
                    "timezone": file.timezone,
                    "start_ms": row.start_ms,
                    "end_ms": row.end_ms,
                    "speaker_id": row.speaker_id or "unknown",
                    "text": row.text,
                }
            )
        return structured

    async def _require_generation(self, version: AnalysisVersion) -> None:
        current = await self.generation_source.credential_generation(
            version.provider_id
        )
        if current == version.credential_generation:
            return
        await self._mark_credential_changed(version)

    async def _mark_credential_changed(self, version: AnalysisVersion) -> None:
        async with self.database.session() as session:
            stored = await session.get(AnalysisVersion, version.id)
            if stored is not None:
                stored.status = "credential_changed"
                stored.error_code = "credential_changed"
                stored.staged_results_json = "{}"
                if stored.reanalysis_batch_id is not None:
                    history = await session.get(
                        ReanalysisBatch, stored.reanalysis_batch_id
                    )
                    if history is not None:
                        history.status = "paused_credential_changed"
                    item = await session.scalar(
                        select(ReanalysisItem).where(
                            ReanalysisItem.analysis_version_id == stored.id
                        )
                    )
                    if item is not None:
                        item.status = "pending"
                        item.error_code = "credential_changed"
                else:
                    job = await session.get(AnalysisJob, stored.source_job_id)
                    if job is not None:
                        job.stage = "failed"
                        job.error_code = "credential_changed"
                await session.commit()
        raise CredentialChangedError(
            f"Credential generation changed for {version.provider_id}"
        )

    async def _require_fixed_rules(self, version: AnalysisVersion) -> None:
        current_hash = PromptComposer.fixed_rules_hash()
        if version.fixed_rules_hash == current_hash:
            return
        async with self.database.session() as session:
            stored = await session.get(AnalysisVersion, version.id)
            if stored is not None:
                stored.status = "fixed_rules_changed"
                stored.error_code = "fixed_rules_changed"
                stored.event_map_json = None
                stored.event_map_hash = None
                stored.staged_results_json = "{}"
                if stored.reanalysis_batch_id is not None:
                    history = await session.get(
                        ReanalysisBatch, stored.reanalysis_batch_id
                    )
                    if history is not None:
                        history.status = "paused_rules_changed"
                    item = await session.scalar(
                        select(ReanalysisItem).where(
                            ReanalysisItem.analysis_version_id == stored.id
                        )
                    )
                    if item is not None:
                        item.status = "pending"
                        item.error_code = "fixed_rules_changed"
                else:
                    job = await session.get(AnalysisJob, stored.source_job_id)
                    if job is not None:
                        job.stage = "failed"
                        job.error_code = "fixed_rules_changed"
                await session.commit()
        raise FixedRulesChangedError("Fixed analysis rules changed")

    async def _version(self, version_id: str) -> AnalysisVersion:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is None:
                raise LookupError(f"Unknown analysis version: {version_id}")
            if version.status != "running":
                raise ValueError(f"Analysis version is not running: {version.status}")
            return version

    async def _save_staged(
        self, version_id: str, staged: dict[str, object]
    ) -> None:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is None:
                raise LookupError(f"Unknown analysis version: {version_id}")
            version.staged_results_json = json.dumps(
                staged, ensure_ascii=False, separators=(",", ":")
            )
            await session.commit()

    async def _save_profile_candidates(
        self,
        version_id: str,
        raw_candidates: list[dict[str, object]],
        segment_ids: set[str],
    ) -> list[dict[str, object]]:
        accepted: list[ProfileCandidate] = []
        verified: list[dict[str, object]] = []
        for item in raw_candidates:
            if item.get("subject_id") != "user":
                continue
            dimension = item.get("dimension")
            value = item.get("value")
            confidence = item.get("confidence")
            evidence = item.get("evidence_segment_ids", [])
            if (
                not isinstance(dimension, str)
                or not isinstance(value, dict)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
                or not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(item_id, str) for item_id in evidence)
                or not set(evidence).issubset(segment_ids)
            ):
                continue
            accepted.append(
                ProfileCandidate(
                    id=str(uuid4()),
                    analysis_version_id=version_id,
                    subject_id="user",
                    dimension=dimension,
                    value_json=json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ),
                    confidence=float(confidence),
                    evidence_segment_ids_json=json.dumps(
                        evidence, ensure_ascii=False, separators=(",", ":")
                    ),
                    origin="explicit" if item.get("explicit") else "inferred",
                )
            )
            verified.append(item)
        async with self.database.session() as session:
            await session.execute(
                delete(ProfileCandidate).where(
                    ProfileCandidate.analysis_version_id == version_id
                )
            )
            session.add_all(accepted)
            await session.commit()
        return verified

    async def _mark_failed(self, version_id: str) -> None:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is not None and version.status == "running":
                version.status = "failed"
                version.error_code = "model_analysis_failed"
                if version.reanalysis_batch_id is not None:
                    item = await session.scalar(
                        select(ReanalysisItem).where(
                            ReanalysisItem.analysis_version_id == version.id
                        )
                    )
                    if item is not None:
                        item.status = "failed"
                        item.error_code = "model_analysis_failed"
                    history = await session.get(
                        ReanalysisBatch, version.reanalysis_batch_id
                    )
                    if history is not None:
                        history.status = "paused_error"
                job = await session.get(AnalysisJob, version.source_job_id)
                if job is not None and version.batch_id is None:
                    job.stage = "failed"
                    job.error_code = "model_analysis_failed"
                await session.commit()

    @staticmethod
    def _prompt_from_snapshot(
        scene_id: str, snapshot: dict[str, object]
    ) -> PromptDocument:
        value = snapshot.get(scene_id)
        if not isinstance(value, dict):
            raise ValueError(f"Prompt snapshot is missing scene: {scene_id}")
        content = value.get("content")
        version = value.get("version", 0)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Prompt snapshot has invalid content: {scene_id}")
        if not isinstance(version, int):
            raise ValueError(f"Prompt snapshot has invalid version: {scene_id}")
        return PromptDocument(scene_id, version, content)

    @staticmethod
    def _json_object(raw: str) -> dict[str, object]:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(raw: str) -> list[dict[str, object]]:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
