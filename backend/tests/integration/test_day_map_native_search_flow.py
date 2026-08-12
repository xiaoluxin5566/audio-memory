from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from audio_memory.analysis.native_search import normalize_search_results
from audio_memory.analysis.provider import ProviderAnalysisError
from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.analysis.runner import AnalysisRunner
from audio_memory.config import AppPaths
from audio_memory.content.service import ContentService
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    Card,
    JobFile,
    ProfileCandidate,
    ProfileFact,
    Transcript,
)
from audio_memory.prompts.autonomous_schema import AutonomousAnalysisResult
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.day_map_schema import (
    AutonomousDayMap,
    NativeSearchDecision,
    SearchResultItem,
)
from audio_memory.providers.adapters.base import NativeSearchCallResult


def search_decision(round_number: int = 1) -> NativeSearchDecision:
    return NativeSearchDecision.model_validate(
        {
            "action": "search",
            "rationale": "需要核对录音中的外部事实。",
            "queries": [
                {
                    "query": f"录音事实核对 第 {round_number} 轮",
                    "purpose": "核对而不改变录音证据",
                }
            ],
        }
    )


def finalize_decision() -> NativeSearchDecision:
    return NativeSearchDecision(
        action="finalize",
        rationale="已有信息足够形成最终结果。",
    )


def day_map() -> AutonomousDayMap:
    return AutonomousDayMap.model_validate(
        {
            "overview": {
                "title": "本次概览",
                "summary": "孩子互动之后，用户记录了一个可核对的节目观点。",
                "scene_ids": ["child-interaction", "media-note"],
            },
            "scenes": [
                {
                    "scene_id": "child-interaction",
                    "title": "孩子互动",
                    "description": "用户先回应孩子，再继续记录。",
                    "evidence_segment_ids": ["seg_0_0"],
                    "file_ids": ["file-new"],
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "recommend_deep_analysis": True,
                    "recommendation_reason": "有可复盘的沟通过程。",
                    "external_verification_need": None,
                },
                {
                    "scene_id": "media-note",
                    "title": "可识别的节目观点",
                    "description": "录音提到《示例节目》中的一个具体说法。",
                    "evidence_segment_ids": ["seg_0_1"],
                    "file_ids": ["file-new"],
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "recommend_deep_analysis": True,
                    "recommendation_reason": "观点值得核对来源。",
                    "external_verification_need": "核对节目观点。",
                },
            ],
            "search_action": search_decision().model_dump(mode="json"),
        }
    )


def final_result(source_ids: list[str]) -> AutonomousAnalysisResult:
    return AutonomousAnalysisResult.model_validate(
        {
            "cards": [
                {
                    "title": "孩子互动与节目笔记",
                    "summary": "录音证据和外部核对结果保持分离。",
                    "external_source_ids": source_ids,
                    "content": [
                        {
                            "type": "scene_reconstruction",
                            "title": "场景还原",
                            "body": "用户先和孩子交流，随后记录《示例节目》的观点。",
                            "evidence_segment_ids": ["seg_0_0", "seg_0_1"],
                        },
                        {
                            "type": "analysis",
                            "title": "分析",
                            "body": "节目观点可由真实外部来源补充，但不替代录音证据。",
                            "evidence_segment_ids": ["seg_0_1"],
                        },
                    ],
                    "evidence_segment_ids": ["seg_0_0", "seg_0_1"],
                }
            ]
        }
    )


class StableGeneration:
    async def credential_generation(self, provider_id: str) -> int:
        assert provider_id == "kimi"
        return 4

    @asynccontextmanager
    async def publication_guard(self, provider_id: str):
        assert provider_id == "kimi"
        yield 4


class RecordingProfileExtractor:
    async def extract(self, transcript, cards, existing, provider_snapshot):
        assert cards == []
        assert {item["segment_id"] for item in transcript} == {
            "seg_0_0",
            "seg_0_1",
        }
        return [
            {
                "subject_id": "user",
                "dimension": "external_leak",
                "value": {"url": "https://evidence.example/1"},
                "confidence": 0.9,
                "explicit": False,
                "evidence_segment_ids": ["seg_0_1"],
            },
            {
                "subject_id": "user",
                "dimension": "interaction_preference",
                "value": {"style": "先回应孩子"},
                "confidence": 0.9,
                "explicit": True,
                "evidence_segment_ids": ["seg_0_0"],
            },
        ]


class SearchProvider:
    def __init__(self, *, available: bool = True, continue_search: bool = False):
        self.available = available
        self.continue_search = continue_search
        self.native_rounds: list[int] = []
        self.day_map_calls = 0

    async def analyze_autonomous_day_map(self, request, provider_snapshot):
        self.day_map_calls += 1
        return day_map()

    async def analyze_autonomous_search_loop(self, request, provider_snapshot):
        if self.continue_search:
            return search_decision(len(self.native_rounds) + 1)
        return finalize_decision()

    async def native_search(
        self,
        provider_id,
        *,
        queries,
        round_number,
        model_id=None,
        timeout_seconds=60,
    ):
        assert provider_id == "kimi"
        assert queries == [f"录音事实核对 第 {round_number} 轮"]
        self.native_rounds.append(round_number)
        if not self.available:
            return NativeSearchCallResult(
                provider_id="kimi",
                model_id=model_id or "kimi-k2.5",
                tool_name="$web_search",
                available=False,
                errors=("Configured provider did not accept native web search.",),
            )
        source = normalize_search_results(
            provider_id="kimi",
            round_number=round_number,
            results=[
                SearchResultItem(
                    provider_result_id=f"result-{round_number}",
                    title=f"真实来源 {round_number}",
                    url=f"https://evidence.example/{round_number}",
                    publisher="Evidence Publisher",
                    snippet=f"第 {round_number} 轮核对结果。",
                )
            ],
        )[0]
        return NativeSearchCallResult(
            provider_id="kimi",
            model_id=model_id or "kimi-k2.5",
            tool_name="$web_search",
            available=True,
            sources=(source,),
        )

    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        return final_result([source.source_id for source in persisted_sources])


class InterruptAtFinalProvider(SearchProvider):
    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        raise RuntimeError("simulated process stop after durable search checkpoint")


class ResumeOnlyProvider(SearchProvider):
    async def analyze_autonomous_day_map(self, request, provider_snapshot):
        raise AssertionError("restart must reuse the persisted Day Map")

    async def analyze_autonomous_search_loop(self, request, provider_snapshot):
        raise AssertionError("restart must reuse the persisted terminal search phase")

    async def native_search(self, *args, **kwargs):
        raise AssertionError("restart must not repeat completed native search")


class NoopAnswerer:
    async def answer(self, **kwargs):
        raise AssertionError("question answering is outside this acceptance test")


async def seed_database(database: Database, tmp_path: Path) -> None:
    await database.create_schema()
    audio_path = tmp_path / "new.mp3"
    audio_path.write_bytes(b"test-audio-placeholder")
    async with database.session() as session:
        session.add_all(
            [
                AnalysisJob(id="job-old", stage="completed"),
                AnalysisJob(id="job-new", stage="analyzing"),
            ]
        )
        session.add_all(
            [
                JobFile(
                    id="file-old",
                    job_id="job-old",
                    original_name="historic.mp3",
                    extension=".mp3",
                    size_bytes=10,
                    sha256="a" * 64,
                    duration_ms=1_000,
                    position=0,
                    temporary_path=str(tmp_path / "historic.mp3"),
                ),
                JobFile(
                    id="file-new",
                    job_id="job-new",
                    original_name="new.mp3",
                    extension=".mp3",
                    size_bytes=22,
                    sha256="b" * 64,
                    duration_ms=2_000,
                    position=0,
                    temporary_path=str(audio_path),
                ),
            ]
        )
        session.add_all(
            [
                Transcript(
                    id="transcript-0",
                    job_file_id="file-new",
                    segment_index=0,
                    segment_uid="file-new:0",
                    start_ms=0,
                    end_ms=1_000,
                    text="我先回应孩子，等他平静下来。",
                    words_json="[]",
                    risk_classified=True,
                ),
                Transcript(
                    id="transcript-1",
                    job_file_id="file-new",
                    segment_index=1,
                    segment_uid="file-new:1",
                    start_ms=1_000,
                    end_ms=2_000,
                    text="《示例节目》提到一个值得核对的观点。",
                    words_json="[]",
                    risk_classified=True,
                ),
            ]
        )
        old_version = AnalysisVersion(
            id="version-old",
            source_job_id="job-old",
            batch_id=None,
            provider_id="deepseek",
            model_id="historic-model",
            credential_generation=1,
            prompt_snapshot_json="{}",
            profile_snapshot_json="[]",
            fixed_rules_hash="historic-rules",
            staged_results_json="{}",
            published_card_count=1,
            published_todo_count=0,
            status="completed",
        )
        new_version = AnalysisVersion(
            id="version-new",
            source_job_id="job-new",
            batch_id=None,
            provider_id="kimi",
            model_id="kimi-k2.5",
            credential_generation=4,
            prompt_snapshot_json="{}",
            profile_snapshot_json="[]",
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json="{}",
            status="running",
        )
        session.add_all([old_version, new_version])
        await session.flush()
        old_batch = Batch(
                    id="batch-old",
                    job_id="job-old",
                    provider_id="deepseek",
                    model_id="historic-model",
                    current_analysis_version_id="version-old",
                    uploaded_at="2026-08-01T10:00:00+00:00",
                    natural_date="2026-08-01",
                )
        new_batch = Batch(
                    id="batch-new",
                    job_id="job-new",
                    provider_id="kimi",
                    model_id="kimi-k2.5",
                    uploaded_at="2026-08-12T10:00:00+00:00",
                    natural_date="2026-08-12",
                )
        session.add_all([old_batch, new_batch])
        await session.flush()
        old_version.batch_id = old_batch.id
        new_version.batch_id = new_batch.id
        session.add(
                Card(
                    id="card-old",
                    batch_id="batch-old",
                    analysis_version_id="version-old",
                    scene_id="meeting",
                    position=0,
                    payload_json=json.dumps(
                        {"scene_id": "meeting", "cards": [{"title": "历史卡片"}]},
                        ensure_ascii=False,
                    ),
                )
        )
        await session.commit()


def build_runner(database: Database, paths: AppPaths, provider) -> AnalysisRunner:
    return AnalysisRunner(
        database=database,
        provider=provider,
        profile_extractor=RecordingProfileExtractor(),
        publisher=VersionPublisher(database, paths),
        generation_source=StableGeneration(),
    )


@pytest.mark.asyncio
async def test_old_batch_stays_visible_and_sources_never_enter_profile(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "compatibility.sqlite3")
    await seed_database(database, tmp_path)
    paths = AppPaths.from_home(tmp_path)
    provider = SearchProvider()

    outcome = await build_runner(database, paths, provider).run("version-new")
    feed = await ContentService(database, paths, NoopAnswerer()).feed()

    cards = [card for day in feed["days"] for card in day["cards"]]
    new_cards = [card for card in cards if card["batch_id"] == "batch-new"]
    old_cards = [card for card in cards if card["batch_id"] == "batch-old"]
    assert outcome.card_count == 2
    assert [card["scene_id"] for card in new_cards] == [
        "batch_overview",
        "analysis",
    ]
    assert sum(card["scene_id"] == "batch_overview" for card in new_cards) == 1
    assert old_cards[0]["payload"]["cards"][0]["title"] == "历史卡片"
    assert "sources" not in old_cards[0]
    assert new_cards[1]["sources"][0]["url"] == "https://evidence.example/1"
    assert new_cards[1]["evidence"][0]["segments"] == [
        {
            "segment_id": "seg_0_0",
            "start_ms": 0,
            "end_ms": 1_000,
            "playback_url": (
                f"/api/cards/{new_cards[1]['id']}/evidence/seg_0_0/audio"
            ),
        },
        {
            "segment_id": "seg_0_1",
            "start_ms": 1_000,
            "end_ms": 2_000,
            "playback_url": (
                f"/api/cards/{new_cards[1]['id']}/evidence/seg_0_1/audio"
            ),
        },
    ]

    async with database.session() as session:
        candidates = list(await session.scalars(select(ProfileCandidate)))
        facts = list(await session.scalars(select(ProfileFact)))
    assert [candidate.dimension for candidate in candidates] == [
        "interaction_preference"
    ]
    assert [fact.dimension for fact in facts] == ["interaction_preference"]
    assert "evidence.example" not in json.dumps(
        [candidate.value_json for candidate in candidates]
        + [fact.value_json for fact in facts]
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_native_search_unavailable_persists_fallback_and_publishes_audio_only(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "unavailable.sqlite3")
    await seed_database(database, tmp_path)
    provider = SearchProvider(available=False)

    await build_runner(database, AppPaths.from_home(tmp_path), provider).run(
        "version-new"
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-new")
    assert provider.native_rounds == [1]
    assert version is not None and version.status == "completed"
    rounds = json.loads(version.search_rounds_json)
    assert rounds[0]["sources"] == []
    assert rounds[0]["errors"] == [
        "Configured provider did not accept native web search."
    ]
    assert json.loads(version.external_sources_json) == []
    await database.dispose()


@pytest.mark.asyncio
async def test_search_exhaustion_stops_after_five_persisted_rounds(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "five-rounds.sqlite3")
    await seed_database(database, tmp_path)
    provider = SearchProvider(continue_search=True)

    await build_runner(database, AppPaths.from_home(tmp_path), provider).run(
        "version-new"
    )

    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-new")
    assert provider.native_rounds == [1, 2, 3, 4, 5]
    assert version is not None
    assert [item["round_number"] for item in json.loads(version.search_rounds_json)] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert len(json.loads(version.external_sources_json)) == 5
    await database.dispose()


@pytest.mark.asyncio
async def test_new_runner_resumes_persisted_terminal_search_without_repeating_calls(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "restart.sqlite3")
    await seed_database(database, tmp_path)
    paths = AppPaths.from_home(tmp_path)

    with pytest.raises(
        RuntimeError, match="simulated process stop after durable search checkpoint"
    ):
        await build_runner(database, paths, InterruptAtFinalProvider()).run(
            "version-new"
        )

    async with database.session() as session:
        interrupted = await session.get(AnalysisVersion, "version-new")
        assert interrupted is not None
        interrupted.status = "running"
        await session.commit()
        staged = json.loads(interrupted.staged_results_json)
    assert staged["search_phase"]["status"] == "finalized"
    assert len(staged["search_rounds"]) == 1

    resumed = ResumeOnlyProvider()
    await build_runner(database, paths, resumed).run("version-new")

    async with database.session() as session:
        completed = await session.get(AnalysisVersion, "version-new")
    assert completed is not None and completed.status == "completed"
    assert resumed.native_rounds == []
    await database.dispose()
