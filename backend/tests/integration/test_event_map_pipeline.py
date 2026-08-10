from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from audio_memory.analysis import runner as runner_module
from audio_memory.analysis.provider import ProviderAnalysisError
from audio_memory.analysis.parser import SceneOutputError
from audio_memory.analysis.publisher import AnalysisOutcome, AnalysisPublisher
from audio_memory.analysis.runner import (
    AnalysisRunner,
    CredentialChangedError,
    FixedRulesChangedError,
    LeaseLostError,
)
from audio_memory.config import AppPaths
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    JobFile,
    ProfileCandidate,
    ReanalysisBatch,
    ReanalysisItem,
    Transcript,
)
from audio_memory.prompts.event_schema import EventMap
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.director_schema import DirectorResult
from audio_memory.prompts.schemas import SceneResultUnion
from audio_memory.prompts.store import PROMPT_SCENES
from pydantic import TypeAdapter
from sqlalchemy import select


SCENE_ADAPTER = TypeAdapter(SceneResultUnion)


def event_map() -> EventMap:
    return EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": None,
                "confidence": 0,
                "reasoning": "无法可靠识别用户",
                "evidence_segment_ids": [],
            },
            "events": [],
            "unassigned_segment_ids": ["seg_0_0"],
        }
    )


def empty_scene(scene_id: str):
    return SCENE_ADAPTER.validate_python(
        {
            "scene_id": scene_id,
            "should_generate": False,
            "generation_reason": "没有足够证据生成这个场景",
            "confidence": 0,
            "cards": [],
            "todos": [],
        }
    )


class RecordingProvider:
    def __init__(self, *, fail_scene: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_scene = fail_scene

    async def analyze_event_map(self, request, provider_snapshot):
        self.calls.append("event-map")
        return event_map()

    async def analyze_director(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        cluster_id = request.scene_id.split(":", 1)[1]
        return DirectorResult.model_validate(
            {
                "selections": [
                    {
                        "selection_id": "selection_001",
                        "cluster_ids": [cluster_id],
                        "source_event_ids": [],
                        "candidate_scenes": list(PROMPT_SCENES),
                        "title": "Synthetic valuable scene",
                        "selection_reason": "Synthetic evidence needs scene analysis.",
                        "value_signals": ["cross_scene_connection"],
                        "priority": "high",
                        "context_before_clusters": 0,
                        "context_after_clusters": 0,
                    }
                ]
            }
        )

    async def analyze_scene(self, scene_id, request, provider_snapshot):
        self.calls.append(scene_id)
        if scene_id == self.fail_scene:
            raise RuntimeError("provider interrupted")
        return empty_scene(scene_id)


class EmptyProfileExtractor:
    async def extract(self, transcript, existing, provider_snapshot):
        return []


class EvidenceProfileExtractor:
    async def extract(self, transcript, existing, provider_snapshot):
        return [
            {
                "subject_id": "user",
                "dimension": "role",
                "value": {"name": "产品经理"},
                "confidence": 0.9,
                "explicit": True,
                "evidence_segment_ids": ["seg_0_0"],
            }
        ]


class FailIfProfileExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, transcript, existing, provider_snapshot):
        self.calls += 1
        raise AssertionError("unknown identity must not reach profile extraction")


class RecordingPublisher:
    def __init__(self) -> None:
        self.results = None
        self.profile_delta = None

    async def publish(
        self,
        version_id,
        results,
        profile_delta,
        *,
        worker_owner_id=None,
    ):
        self.results = results
        self.profile_delta = profile_delta
        return AnalysisOutcome("published-batch", 0, 0)


class StableGeneration:
    async def credential_generation(self, provider_id: str) -> int:
        return 4

    @asynccontextmanager
    async def publication_guard(self, provider_id: str):
        yield 4


class ChangingGeneration:
    def __init__(self) -> None:
        self.calls = 0

    async def credential_generation(self, provider_id: str) -> int:
        self.calls += 1
        return 4 if self.calls == 1 else 5

    @asynccontextmanager
    async def publication_guard(self, provider_id: str):
        yield 5


class FailingProvider(RecordingProvider):
    async def analyze_event_map(self, request, provider_snapshot):
        raise ProviderAnalysisError("request failed")


class InvalidOutputProvider(RecordingProvider):
    async def analyze_event_map(self, request, provider_snapshot):
        raise SceneOutputError("second response still violates schema")


def assigned_event_map(
    *, evidence_id: str, start_ms: int = 0, end_ms: int = 1_000
) -> EventMap:
    return EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": None,
                "confidence": 0,
                "reasoning": "无法可靠识别用户",
                "evidence_segment_ids": [],
            },
            "events": [
                {
                    "event_id": "event_001",
                    "parent_event_id": None,
                    "event_type": "conversation",
                    "title": "普通讨论",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker_ids": ["unknown"],
                    "user_role": None,
                    "user_role_confidence": 0,
                    "factual_summary": "发生了一段普通讨论。",
                    "topics": ["普通讨论"],
                    "candidate_scenes": [],
                    "evidence_segment_ids": [evidence_id],
                    "boundary_confidence": 0.8,
                    "local_date": None,
                    "timezone": None,
                }
            ],
        }
    )


class AssignedOnlyProvider(RecordingProvider):
    def __init__(self, evidence_id: str = "seg_0_0") -> None:
        super().__init__()
        self.evidence_id = evidence_id

    async def analyze_event_map(self, request, provider_snapshot):
        self.calls.append("event-map")
        return assigned_event_map(evidence_id=self.evidence_id)


class SpecificFailureProvider(RecordingProvider):
    async def analyze_event_map(self, request, provider_snapshot):
        raise ProviderAnalysisError(
            "schema invalid", code="event_map_schema_invalid"
        )


class FailingDirectorProvider(AssignedOnlyProvider):
    async def analyze_director(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        raise ProviderAnalysisError(
            "synthetic director failure", code="director_schema_invalid"
        )


def decode_packet(user_data: str, name: str) -> object:
    opening = f"<untrusted_{name}>\n"
    closing = f"\n</untrusted_{name}>"
    start = user_data.index(opening) + len(opening)
    end = user_data.index(closing, start)
    return json.loads(user_data[start:end])


class SelectiveDirectorProvider(AssignedOnlyProvider):
    def __init__(self) -> None:
        super().__init__()
        self.director_segment_ids: list[str] = []

    async def analyze_director(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        clusters = decode_packet(request.user_data, "transcript_clusters")
        self.director_segment_ids.extend(
            segment["segment_id"]
            for cluster in clusters
            for segment in cluster["segments"]
        )
        cluster_id = request.scene_id.split(":", 1)[1]
        return DirectorResult.model_validate(
            {
                "selections": [
                    {
                        "selection_id": "selection_001",
                        "cluster_ids": [cluster_id],
                        "source_event_ids": [],
                        "candidate_scenes": ["meeting", "todo"],
                        "title": "Synthetic work communication",
                        "selection_reason": "Contains an assigned fact and adjacent context.",
                        "value_signals": ["follow_up_needed"],
                        "priority": "high",
                        "context_before_clusters": 0,
                        "context_after_clusters": 0,
                    }
                ]
            }
        )


class WindowedProvider(RecordingProvider):
    async def analyze_event_map(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        if request.scene_id == "event-map:window_0000":
            return assigned_event_map(evidence_id="seg_0_0")
        if request.scene_id == "event-map:window_0001":
            return assigned_event_map(
                evidence_id="seg_0_1",
                start_ms=50_000,
                end_ms=51_000,
            )
        return assigned_event_map(evidence_id="seg_0_0")


class FailingSecondWindowProvider(WindowedProvider):
    async def analyze_event_map(self, request, provider_snapshot):
        if request.scene_id == "event-map:window_0001":
            self.calls.append(request.scene_id)
            raise ProviderAnalysisError(
                "synthetic local schema failure",
                code="event_map_schema_invalid",
            )
        return await super().analyze_event_map(request, provider_snapshot)


class ObjectiveWorkProvider(RecordingProvider):
    async def analyze_event_map(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        return assigned_event_map(evidence_id="seg_0_0")

    async def analyze_scene(self, scene_id, request, provider_snapshot):
        self.calls.append(scene_id)
        if scene_id != "meeting":
            return empty_scene(scene_id)
        return SCENE_ADAPTER.validate_python(
            {
                "scene_id": "meeting",
                "should_generate": True,
                "generation_reason": "有一段具备回顾价值的客观工作沟通。",
                "confidence": 0.9,
                "cards": [
                    {
                        "event_ids": ["event_w0000_001"],
                        "card": {
                            "title": "讨论明确了产品范围",
                            "summary": "录音中的参与者讨论了一项产品范围。",
                        },
                        "confidence": 0.9,
                        "detail": {
                            "event_id": "event_w0000_001",
                            "topic": "产品范围",
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "background": "一段客观工作沟通。",
                            "participants": [],
                            "core_conclusions": [
                                {
                                    "content": "参与者讨论了一项产品范围。",
                                    "evidence_segment_ids": ["seg_0_0"],
                                }
                            ],
                            "decisions": [],
                            "open_questions": [],
                            "meeting_todos": [],
                            "discussion_topics": [],
                        },
                    }
                ],
                "todos": [],
            }
        )


class ChangesAfterProviderFailure(StableGeneration):
    def __init__(self) -> None:
        self.calls = 0

    async def credential_generation(self, provider_id: str) -> int:
        self.calls += 1
        return 4 if self.calls == 1 else 5


class ChangesOnlyAtPublication(StableGeneration):
    @asynccontextmanager
    async def publication_guard(self, provider_id: str):
        yield 5


class ReassignsOwnerDuringGenerationCheck(StableGeneration):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def credential_generation(self, provider_id: str) -> int:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, "version-1")
            assert version is not None
            version.worker_owner_id = "worker-b"
            await session.commit()
        return 5


class BlockingProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def analyze_event_map(self, request, provider_snapshot):
        self.started.set()
        await asyncio.Event().wait()


class ReleasableProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze_event_map(self, request, provider_snapshot):
        self.started.set()
        await self.release.wait()
        return event_map()


class InvalidEvidenceProfileExtractor:
    async def extract(self, transcript, existing, provider_snapshot):
        return [
            {
                "subject_id": "user",
                "dimension": "role",
                "value": {"name": "未经证实"},
                "confidence": 0.9,
                "explicit": True,
                "evidence_segment_ids": ["seg_missing"],
            }
        ]


async def seed_version(
    database: Database,
    tmp_path: Path,
    *,
    version_id: str = "version-1",
    history_batch_id: str | None = None,
) -> None:
    prompts = {
        scene_id: {"version": 7, "content": f"snapshot {scene_id}"}
        for scene_id in PROMPT_SCENES
    }
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="meeting.mp3",
                extension=".mp3",
                size_bytes=10,
                sha256="a" * 64,
                duration_ms=1000,
                position=0,
                temporary_path=str(tmp_path / "meeting.mp3"),
            )
        )
        session.add(
            Transcript(
                id="transcript-1",
                job_file_id="file-1",
                segment_index=0,
                segment_uid="file-1:0",
                speaker_id="speaker_0",
                start_ms=0,
                end_ms=1000,
                text="普通转写内容",
                words_json="[]",
                risk_classified=True,
            )
        )
        if history_batch_id is not None:
            session.add(
                Batch(
                    id="source-batch",
                    job_id="job-1",
                    natural_date="2026-08-05",
                )
            )
            session.add(
                ReanalysisBatch(
                    id=history_batch_id,
                    status="running",
                    provider_id="kimi",
                    model_id="kimi-k2.5",
                    credential_generation=4,
                    prompt_snapshot_json=json.dumps(prompts),
                    profile_snapshot_json="[]",
                    fixed_rules_hash="f" * 64,
                    snapshot_hash="s" * 64,
                )
            )
            await session.flush()
        session.add(
            AnalysisVersion(
                id=version_id,
                source_job_id="job-1",
                batch_id="source-batch" if history_batch_id is not None else None,
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=4,
                prompt_snapshot_json=json.dumps(prompts),
                profile_snapshot_json="[]",
                fixed_rules_hash=PromptComposer.fixed_rules_hash(),
                staged_results_json="{}",
                priority=0,
                status="running",
                reanalysis_batch_id=history_batch_id,
            )
        )
        await session.flush()
        if history_batch_id is not None:
            session.add(
                ReanalysisItem(
                    id="history-item",
                    reanalysis_batch_id=history_batch_id,
                    source_batch_id="source-batch",
                    analysis_version_id=version_id,
                    position=0,
                    status="running",
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_runner_completes_unassigned_segment_ids_before_checkpoint(
    tmp_path, monkeypatch
) -> None:
    logged: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        runner_module.logger, "info", lambda *values: logged.append(values)
    )
    database = Database(tmp_path / "local-coverage.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        session.add(
            Transcript(
                id="transcript-2",
                job_file_id="file-1",
                segment_index=1,
                segment_uid="file-1:1",
                speaker_id="unknown",
                start_ms=1_000,
                end_ms=2_000,
                text="第二段普通转写内容",
                words_json="[]",
                risk_classified=True,
            )
        )
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=AssignedOnlyProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )
    version = await runner._version("version-1", None)
    transcript = await runner._transcript("job-1")

    completed = await runner._event_map(
        version,
        transcript,
        [],
        {"provider_id": "kimi", "model_id": "kimi-k2.5"},
        None,
    )

    assert completed.unassigned_segment_ids == ["seg_0_1"]
    async with database.session() as session:
        stored = await session.get(AnalysisVersion, "version-1")
    assert stored is not None
    assert EventMap.model_validate_json(stored.event_map_json).unassigned_segment_ids == [
        "seg_0_1"
    ]
    assert runner_module.logger.name == "uvicorn.error"
    assert logged == [
        (
            "event_map_coverage windows=%d events=%d known=%d assigned=%d "
            "unassigned=%d unknown=0",
            1,
            1,
            2,
            1,
            1,
        )
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_builds_and_merges_one_event_map_per_analysis_window(
    tmp_path,
) -> None:
    database = Database(tmp_path / "windowed-event-map.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        session.add(
            Transcript(
                id="transcript-2",
                job_file_id="file-1",
                segment_index=1,
                segment_uid="file-1:1",
                speaker_id="unknown",
                start_ms=50_000,
                end_ms=51_000,
                text="Synthetic second work discussion",
                words_json="[]",
                risk_classified=True,
            )
        )
        await session.commit()
    provider = WindowedProvider()
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    await runner.run("version-1")

    assert provider.calls[:2] == [
        "event-map:window_0000",
        "event-map:window_0001",
    ]
    assert len(provider.calls[2:4]) == 2
    assert all(
        call.startswith("director:cluster_") for call in provider.calls[2:4]
    )
    assert provider.calls[4:] == list(PROMPT_SCENES)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None and version.event_map_json is not None
    merged = EventMap.model_validate_json(version.event_map_json)
    assert [event.event_id for event in merged.events] == [
        "event_w0000_001",
        "event_w0001_001",
    ]
    assert merged.unassigned_segment_ids == []
    assert publisher.results is not None
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_director_reads_all_segments_and_calls_only_routed_scenes(
    tmp_path,
) -> None:
    database = Database(tmp_path / "director-routing.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        session.add(
            Transcript(
                id="transcript-2",
                job_file_id="file-1",
                segment_index=1,
                segment_uid="file-1:1",
                speaker_id="unknown",
                start_ms=1_100,
                end_ms=2_000,
                text="Synthetic context omitted by Event Map",
                words_json="[]",
                risk_classified=True,
            )
        )
        await session.commit()
    provider = SelectiveDirectorProvider()
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    await runner.run("version-1")

    assert provider.director_segment_ids == ["seg_0_0", "seg_0_1"]
    assert [call for call in provider.calls if call in PROMPT_SCENES] == [
        "todo",
        "meeting",
    ]
    assert publisher.results is not None
    assert [result.scene_id for result in publisher.results] == list(PROMPT_SCENES)
    assert all(
        result.generation_reason == "no_selected_dossier"
        for result in publisher.results
        if result.scene_id not in {"todo", "meeting"}
    )
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None
    staged = json.loads(version.staged_results_json)
    context = staged["_scene_context"]
    assert len(context["dossiers"]) == 1
    assert context["dossiers"][0]["allowed_segment_ids"] == [
        "seg_0_0",
        "seg_0_1",
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_director_failure_leaves_no_context_or_supplemental_anchor(tmp_path) -> None:
    database = Database(tmp_path / "director-failure.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    provider = FailingDirectorProvider()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1")

    assert raised.value.code == "director_schema_invalid"
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None and version.event_map_json is not None
    stored_map = EventMap.model_validate_json(version.event_map_json)
    assert all(
        not event.event_id.startswith("event_context_")
        for event in stored_map.events
    )
    assert "_scene_context" not in json.loads(version.staged_results_json)
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_does_not_checkpoint_partial_window_event_maps(tmp_path) -> None:
    database = Database(tmp_path / "windowed-event-map-failure.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        session.add(
            Transcript(
                id="transcript-2",
                job_file_id="file-1",
                segment_index=1,
                segment_uid="file-1:1",
                speaker_id="unknown",
                start_ms=50_000,
                end_ms=51_000,
                text="Synthetic second work discussion",
                words_json="[]",
                risk_classified=True,
            )
        )
        await session.commit()
    provider = FailingSecondWindowProvider()
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1")

    assert raised.value.code == "event_map_schema_invalid"
    assert provider.calls == ["event-map:window_0000", "event-map:window_0001"]
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None
    assert version.event_map_json is None
    assert json.loads(version.staged_results_json) == {}
    assert publisher.results is None
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_skips_profile_extraction_when_global_identity_is_unknown(
    tmp_path,
) -> None:
    database = Database(tmp_path / "unknown-identity-profile.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    profile_extractor = FailIfProfileExtractor()
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=ObjectiveWorkProvider(),
        profile_extractor=profile_extractor,
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    await runner.run("version-1")

    assert profile_extractor.calls == 0
    assert publisher.results is not None
    assert publisher.profile_delta == []
    assert next(
        result for result in publisher.results if result.scene_id == "meeting"
    ).should_generate is True
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_rejects_undersegmented_empty_long_audio_before_publication(
    tmp_path,
) -> None:
    database = Database(tmp_path / "undersegmented-quality.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        session.add(
            Transcript(
                id="transcript-2",
                job_file_id="file-1",
                segment_index=1,
                segment_uid="file-1:1",
                speaker_id="unknown",
                start_ms=7_199_000,
                end_ms=7_200_000,
                text="Synthetic distant segment",
                words_json="[]",
                risk_classified=True,
            )
        )
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.event_map_json = EventMap.model_validate(
            {
                "user_speaker": {
                    "speaker_id": None,
                    "confidence": 0,
                    "reasoning": "Synthetic unknown identity.",
                    "evidence_segment_ids": [],
                },
                "events": [
                    {
                        "event_id": "event_existing_001",
                        "parent_event_id": None,
                        "event_type": "casual_chat",
                        "title": "Synthetic broad event",
                        "start_ms": 0,
                        "end_ms": 7_200_000,
                        "speaker_ids": ["unknown"],
                        "user_role": None,
                        "user_role_confidence": 0,
                        "factual_summary": "Synthetic broad event.",
                        "topics": ["synthetic"],
                        "candidate_scenes": [],
                        "evidence_segment_ids": ["seg_0_0", "seg_0_1"],
                        "boundary_confidence": 0.5,
                        "local_date": None,
                        "timezone": None,
                    }
                ],
                "unassigned_segment_ids": [],
            }
        ).model_dump_json()
        await session.commit()
    publisher = RecordingPublisher()
    profile_extractor = FailIfProfileExtractor()
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=profile_extractor,
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1")

    assert raised.value.code == "analysis_quality_insufficient"
    assert profile_extractor.calls == 0
    assert publisher.results is None
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        job = await session.get(AnalysisJob, "job-1")
    assert version is not None
    assert version.error_code == "analysis_quality_insufficient"
    assert set(json.loads(version.staged_results_json)) == {
        "_scene_context",
        *PROMPT_SCENES,
    }
    assert job is not None and job.error_code == "analysis_quality_insufficient"
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_rejects_unknown_event_evidence_without_checkpoint(tmp_path) -> None:
    database = Database(tmp_path / "unknown-coverage.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=AssignedOnlyProvider("seg_missing"),
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1")

    assert raised.value.code == "event_map_unknown_segment"
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        job = await session.get(AnalysisJob, "job-1")
    assert version is not None and version.error_code == "event_map_unknown_segment"
    assert version.event_map_json is None
    assert json.loads(version.staged_results_json) == {}
    assert job is not None and job.error_code == "event_map_unknown_segment"
    assert publisher.results is None
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_preserves_specific_event_map_schema_error(tmp_path) -> None:
    database = Database(tmp_path / "schema-error.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    runner = AnalysisRunner(
        database=database,
        provider=SpecificFailureProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner.run("version-1")

    assert raised.value.code == "event_map_schema_invalid"
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        job = await session.get(AnalysisJob, "job-1")
    assert version is not None and version.error_code == "event_map_schema_invalid"
    assert job is not None and job.error_code == "event_map_schema_invalid"
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_checkpoints_event_map_and_each_scene_then_resumes(tmp_path) -> None:
    database = Database(tmp_path / "runner.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    first_provider = RecordingProvider(fail_scene="parenting")
    runner = AnalysisRunner(
        database=database,
        provider=first_provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        await runner.run("version-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        checkpoint = EventMap.model_validate_json(version.event_map_json or "null")
        assert len(checkpoint.events) == 1
        assert checkpoint.events[0].event_id.startswith("event_context_")
        assert checkpoint.unassigned_segment_ids == []
        assert checkpoint.user_speaker.speaker_id is None
        assert list(json.loads(version.staged_results_json)) == [
            "_scene_context",
            "todo",
            "meeting",
        ]
        version.status = "running"
        version.error_code = None
        await session.commit()

    second_provider = RecordingProvider()
    publisher = RecordingPublisher()
    resumed = AnalysisRunner(
        database=database,
        provider=second_provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )
    outcome = await resumed.run("version-1")

    assert outcome.batch_id == "published-batch"
    assert second_provider.calls == ["parenting", "content", "growth", "inspiration"]
    assert publisher.results is not None and len(publisher.results) == 6
    await database.dispose()


@pytest.mark.asyncio
async def test_generation_change_discards_scenes_and_pauses_history(tmp_path) -> None:
    database = Database(tmp_path / "generation.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path, history_batch_id="history-1")
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=ChangingGeneration(),
    )

    with pytest.raises(CredentialChangedError):
        await runner.run("version-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        batch = await session.get(ReanalysisBatch, "history-1")
    assert version is not None
    assert version.status == "credential_changed"
    assert version.error_code == "credential_changed"
    assert json.loads(version.staged_results_json) == {}
    assert batch is not None and batch.status == "paused"
    async with database.session() as session:
        item = await session.get(ReanalysisItem, "history-item")
    assert item is not None and item.status == "pending"
    assert item.error_code == "credential_changed"
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_persists_version_scoped_profile_candidates(tmp_path) -> None:
    database = Database(tmp_path / "profile-candidates.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    reliable_map = event_map().model_copy(
        update={
            "user_speaker": event_map().user_speaker.model_copy(
                update={
                    "speaker_id": "speaker_0",
                    "confidence": 0.90,
                    "reasoning": "Synthetic reliable identity evidence.",
                    "evidence_segment_ids": ["seg_0_0"],
                }
            )
        }
    )
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.event_map_json = reliable_map.model_dump_json()
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=EvidenceProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )

    await runner.run("version-1")

    async with database.session() as session:
        candidates = list(await session.scalars(select(ProfileCandidate)))
    assert len(candidates) == 1
    assert candidates[0].analysis_version_id == "version-1"
    assert candidates[0].evidence_segment_ids_json == '["seg_0_0"]'
    assert candidates[0].origin == "explicit"
    await database.dispose()


@pytest.mark.asyncio
async def test_cancelled_runner_leaves_version_running_for_restart_recovery(tmp_path) -> None:
    database = Database(tmp_path / "cancelled-runner.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    provider = BlockingProvider()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )
    task = asyncio.create_task(runner.run("version-1"))
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None and version.status == "running"
    await database.dispose()


@pytest.mark.asyncio
async def test_invalid_profile_evidence_never_reaches_publisher(tmp_path) -> None:
    database = Database(tmp_path / "profile-filter.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=InvalidEvidenceProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    await runner.run("version-1")

    assert publisher.profile_delta == []
    await database.dispose()


@pytest.mark.asyncio
async def test_successful_history_run_completes_item_and_batch(tmp_path) -> None:
    from audio_memory.analysis.task_coordinator import AnalysisTaskCoordinator
    from audio_memory.reanalysis.worker import ReanalysisWorker

    database = Database(tmp_path / "history-terminal.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path, history_batch_id="history-1")
    publisher = AnalysisPublisher(database)
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )

    await runner.run("version-1")

    async with database.session() as session:
        item = await session.get(ReanalysisItem, "history-item")
        batch = await session.get(ReanalysisBatch, "history-1")
    assert item is not None and item.status == "succeeded"
    assert item.completed_at is not None
    assert batch is not None
    assert batch.status == "content_completed_profile_failed"

    await ReanalysisWorker(
        database=database,
        task_coordinator=AnalysisTaskCoordinator(database),
        publisher=publisher,
    ).tick()
    async with database.session() as session:
        batch = await session.get(ReanalysisBatch, "history-1")
    assert batch is not None and batch.status == "completed"
    assert batch.completed_at is not None
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_rejects_changed_fixed_rules_before_remote_request(tmp_path) -> None:
    database = Database(tmp_path / "rules-changed.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        version.fixed_rules_hash = "0" * 64
        version.event_map_json = json.dumps(event_map().model_dump(mode="json"))
        version.staged_results_json = json.dumps(
            {"todo": empty_scene("todo").model_dump(mode="json")}
        )
        await session.commit()
    provider = RecordingProvider()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(FixedRulesChangedError):
        await runner.run("version-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert provider.calls == []
    assert version is not None and version.status == "fixed_rules_changed"
    assert version.event_map_json is None
    assert json.loads(version.staged_results_json) == {}
    await database.dispose()


@pytest.mark.asyncio
async def test_provider_failure_after_key_replacement_is_credential_changed(tmp_path) -> None:
    database = Database(tmp_path / "provider-error-generation.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    runner = AnalysisRunner(
        database=database,
        provider=FailingProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=ChangesAfterProviderFailure(),
    )

    with pytest.raises(CredentialChangedError):
        await runner.run("version-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None and version.status == "credential_changed"
    await database.dispose()


@pytest.mark.asyncio
async def test_generic_remote_output_failure_after_key_replacement_pauses_history(
    tmp_path,
) -> None:
    database = Database(tmp_path / "generic-output-generation.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path, history_batch_id="history-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.staged_results_json = json.dumps(
            {"todo": empty_scene("todo").model_dump(mode="json")}
        )
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=InvalidOutputProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=ChangesAfterProviderFailure(),
    )

    with pytest.raises(CredentialChangedError):
        await runner.run("version-1")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        item = await session.get(ReanalysisItem, "history-item")
        history = await session.get(ReanalysisBatch, "history-1")
    assert version is not None and version.status == "credential_changed"
    assert json.loads(version.staged_results_json) == {}
    assert item is not None and item.status == "pending"
    assert item.error_code == "credential_changed"
    assert history is not None and history.status == "paused"
    await database.dispose()


@pytest.mark.asyncio
async def test_final_publication_guard_blocks_changed_generation(tmp_path) -> None:
    database = Database(tmp_path / "publication-generation.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=ChangesOnlyAtPublication(),
    )

    with pytest.raises(CredentialChangedError):
        await runner.run("version-1")

    assert publisher.results is None
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None and version.status == "credential_changed"
    await database.dispose()


@pytest.mark.asyncio
async def test_publication_is_idempotent_by_version_and_commits_terminal_snapshot(
    tmp_path,
) -> None:
    database = Database(tmp_path / "idempotent-publication.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    results = [empty_scene(scene_id) for scene_id in PROMPT_SCENES]
    publisher = AnalysisPublisher(database)

    first = await publisher.publish("version-1", results, [])
    second = await publisher.publish("version-1", results, [])

    async with database.session() as session:
        batches = list(await session.scalars(select(Batch)))
        version = await session.get(AnalysisVersion, "version-1")
        job = await session.get(AnalysisJob, "job-1")
    assert first == second
    assert len(batches) == 1
    assert version is not None and version.status == "completed"
    assert version.batch_id == first.batch_id
    assert batches[0].current_analysis_version_id == "version-1"
    assert job is not None and job.provider_id == "kimi"
    assert job.model_id == "kimi-k2.5"
    await database.dispose()


@pytest.mark.asyncio
async def test_publication_recovers_when_audio_was_moved_before_database_commit(
    tmp_path,
) -> None:
    database = Database(tmp_path / "moved-before-commit.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    paths = AppPaths.from_home(tmp_path / "home")
    paths.ensure_directories()
    source = tmp_path / "meeting.mp3"
    source.write_bytes(b"audio")
    batch_id = str(uuid5(NAMESPACE_URL, "audio-memory-batch:job-1"))
    destination = paths.audio / batch_id / "file-1.mp3"
    destination.parent.mkdir(parents=True)
    os.replace(source, destination)
    publisher = AnalysisPublisher(database, paths)

    outcome = await publisher.publish(
        "version-1",
        [empty_scene(scene_id) for scene_id in PROMPT_SCENES],
        [],
    )

    async with database.session() as session:
        stored_file = await session.get(JobFile, "file-1")
        version = await session.get(AnalysisVersion, "version-1")
    assert outcome.batch_id == batch_id
    assert stored_file is not None and stored_file.temporary_path == str(destination)
    assert version is not None and version.status == "completed"
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_stops_after_its_worker_lease_is_reassigned(tmp_path) -> None:
    database = Database(tmp_path / "lease-fence-runner.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.worker_owner_id = "worker-a"
        await session.commit()
    provider = ReleasableProvider()
    publisher = RecordingPublisher()
    runner = AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=EmptyProfileExtractor(),
        publisher=publisher,
        generation_source=StableGeneration(),
    )
    task = asyncio.create_task(
        runner.run("version-1", worker_owner_id="worker-a")
    )
    await provider.started.wait()
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.worker_owner_id = "worker-b"
        await session.commit()
    provider.release.set()

    with pytest.raises(LeaseLostError):
        await task

    assert publisher.results is None
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert version is not None and version.status == "running"
    assert version.worker_owner_id == "worker-b"
    await database.dispose()


@pytest.mark.asyncio
async def test_publisher_rejects_a_stale_worker_owner(tmp_path) -> None:
    database = Database(tmp_path / "lease-fence-publisher.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.worker_owner_id = "worker-b"
        await session.commit()

    with pytest.raises(RuntimeError, match="lease"):
        await AnalysisPublisher(database).publish(
            "version-1",
            [empty_scene(scene_id) for scene_id in PROMPT_SCENES],
            [],
            worker_owner_id="worker-a",
        )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        batches = list(await session.scalars(select(Batch)))
    assert version is not None and version.status == "running"
    assert version.worker_owner_id == "worker-b"
    assert batches == []
    await database.dispose()


@pytest.mark.asyncio
async def test_stale_worker_cannot_mark_credential_changed(tmp_path) -> None:
    database = Database(tmp_path / "lease-fence-credential.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.worker_owner_id = "worker-a"
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=ReassignsOwnerDuringGenerationCheck(database),
    )

    with pytest.raises(LeaseLostError):
        await runner.run("version-1", worker_owner_id="worker-a")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        job = await session.get(AnalysisJob, "job-1")
    assert version is not None and version.status == "running"
    assert version.worker_owner_id == "worker-b"
    assert job is not None and job.stage == "analyzing"
    await database.dispose()


@pytest.mark.asyncio
async def test_stale_worker_cannot_mark_fixed_rules_changed(tmp_path) -> None:
    database = Database(tmp_path / "lease-fence-rules.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        assert version is not None
        version.fixed_rules_hash = "0" * 64
        version.worker_owner_id = "worker-a"
        await session.commit()
    async with database.session() as session:
        stale_version = await session.get(AnalysisVersion, "version-1")
        assert stale_version is not None
    async with database.session() as session:
        current = await session.get(AnalysisVersion, "version-1")
        assert current is not None
        current.worker_owner_id = "worker-b"
        await session.commit()
    runner = AnalysisRunner(
        database=database,
        provider=RecordingProvider(),
        profile_extractor=EmptyProfileExtractor(),
        publisher=RecordingPublisher(),
        generation_source=StableGeneration(),
    )

    with pytest.raises(LeaseLostError):
        await runner._require_fixed_rules(stale_version, "worker-a")

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
        job = await session.get(AnalysisJob, "job-1")
    assert version is not None and version.status == "running"
    assert version.worker_owner_id == "worker-b"
    assert version.fixed_rules_hash == "0" * 64
    assert job is not None and job.stage == "analyzing"
    await database.dispose()
