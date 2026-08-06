from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_memory.analysis.publisher import AnalysisOutcome
from audio_memory.analysis.runner import AnalysisRunner, CredentialChangedError
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    JobFile,
    ProfileCandidate,
    ReanalysisBatch,
    Transcript,
)
from audio_memory.prompts.event_schema import EventMap
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


class RecordingPublisher:
    def __init__(self) -> None:
        self.results = None

    async def publish(self, job_id, results, profile_delta):
        self.results = results
        return AnalysisOutcome("published-batch", 0, 0)


class StableGeneration:
    async def credential_generation(self, provider_id: str) -> int:
        return 4


class ChangingGeneration:
    def __init__(self) -> None:
        self.calls = 0

    async def credential_generation(self, provider_id: str) -> int:
        self.calls += 1
        return 4 if self.calls == 1 else 5


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
            )
        )
        if history_batch_id is not None:
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
                provider_id="kimi",
                model_id="kimi-k2.5",
                credential_generation=4,
                prompt_snapshot_json=json.dumps(prompts),
                profile_snapshot_json="[]",
                fixed_rules_hash="f" * 64,
                staged_results_json="{}",
                priority=0,
                status="running",
                reanalysis_batch_id=history_batch_id,
            )
        )
        await session.commit()


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
        assert json.loads(version.event_map_json or "null") == event_map().model_dump(
            mode="json"
        )
        assert list(json.loads(version.staged_results_json)) == ["todo", "meeting"]
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
    assert batch is not None and batch.status == "paused_credential_changed"
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_persists_version_scoped_profile_candidates(tmp_path) -> None:
    database = Database(tmp_path / "profile-candidates.sqlite3")
    await database.create_schema()
    await seed_version(database, tmp_path)
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
