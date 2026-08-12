from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from audio_memory.analysis.native_search import normalize_search_results
from audio_memory.analysis.provider import ProviderAnalysisClient, ProviderAnalysisError
from audio_memory.analysis.publisher import AnalysisOutcome, VersionPublisher
from audio_memory.analysis.runner import AnalysisRunner
from audio_memory.prompts.autonomous_schema import AutonomousAnalysisResult
from audio_memory.prompts.day_map_schema import (
    AutonomousDayMap,
    ExternalSource,
    NativeSearchDecision,
    SearchResultItem,
)
from audio_memory.providers.adapters.base import NativeSearchCallResult
from audio_memory.providers.keychain import KeychainReadResult, KeychainStatus


def segment(index: int, text: str = "录音原文") -> dict[str, object]:
    return {
        "segment_id": f"seg_{index}",
        "file_id": "file-1",
        "file_name": "recording.m4a",
        "start_ms": index * 1_000,
        "end_ms": (index + 1) * 1_000,
        "text": text,
    }


def decision(action: str = "finalize", query: str = "") -> dict[str, object]:
    if action == "search":
        return {
            "action": "search",
            "rationale": "需要核对外部事实。",
            "queries": [{"query": query or "verify claim", "purpose": "核对"}],
        }
    return {"action": "finalize", "rationale": "原录音证据已足够。", "queries": []}


def day_map(search_action: dict[str, object] | None = None) -> AutonomousDayMap:
    return AutonomousDayMap.model_validate(
        {
            "overview": {
                "title": "本次概览",
                "summary": "录音包含一个值得回顾的单元。",
                "scene_ids": ["free-scene"],
            },
            "scenes": [
                {
                    "scene_id": "free-scene",
                    "title": "自由命名的现实单元",
                    "description": "不使用服务端分类。",
                    "evidence_segment_ids": ["seg_0"],
                    "file_ids": ["file-1"],
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "recommend_deep_analysis": True,
                    "recommendation_reason": "对用户有独立价值。",
                    "external_verification_need": None,
                }
            ],
            "search_action": search_action or decision(),
        }
    )


def final_result(source_ids: list[str] | None = None) -> AutonomousAnalysisResult:
    return AutonomousAnalysisResult.model_validate(
        {
            "cards": [
                {
                    "title": "最终分析",
                    "summary": "依据原录音形成判断。",
                    "external_source_ids": source_ids or [],
                    "content": [
                        {
                            "type": "scene_reconstruction",
                            "title": "场景还原",
                            "body": "录音中出现了一个问题。",
                            "evidence_segment_ids": ["seg_0"],
                        },
                        {
                            "type": "analysis",
                            "title": "分析",
                            "body": "这个问题值得进一步处理。",
                            "evidence_segment_ids": ["seg_0"],
                        },
                    ],
                    "evidence_segment_ids": ["seg_0"],
                }
            ]
        }
    )


def source(round_number: int) -> ExternalSource:
    raw = SearchResultItem(
        provider_result_id=f"result-{round_number}",
        title=f"Source {round_number}",
        url=f"https://example.test/{round_number}",
        publisher="Example Publisher",
        snippet=f"External support text {round_number}",
    )
    return normalize_search_results(
        provider_id="kimi", round_number=round_number, results=[raw]
    )[0]


class RecordingProvider:
    def __init__(self, *, initial: str = "finalize", followup: str = "finalize"):
        self.initial = initial
        self.followup = followup
        self.calls: list[str] = []
        self.native_calls: list[tuple[int, list[str]]] = []
        self.final_sources: list[ExternalSource] = []
        self.request_segment_counts: list[int] = []

    async def analyze_autonomous_day_map(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        self.request_segment_counts.append(request.segment_count)
        return day_map(decision(self.initial, "initial query"))

    async def analyze_autonomous_search_loop(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        return NativeSearchDecision.model_validate(
            decision(self.followup, f"query round {len(self.native_calls) + 1}")
        )

    async def native_search(
        self, provider_id, *, queries, round_number, model_id=None, timeout_seconds=60
    ):
        self.native_calls.append((round_number, list(queries)))
        item = source(round_number)
        return NativeSearchCallResult(
            provider_id=provider_id,
            model_id=model_id or "kimi-k2.5",
            tool_name="$web_search",
            available=True,
            sources=(item,),
        )

    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        self.calls.append(request.scene_id)
        self.request_segment_counts.append(request.segment_count)
        self.final_sources = list(persisted_sources)
        return final_result([item.source_id for item in persisted_sources])


class IsolatedRunner(AnalysisRunner):
    def __init__(self, provider) -> None:
        self.provider = provider
        from audio_memory.prompts.composer import PromptComposer

        self.composer = PromptComposer()
        self.saved: list[dict[str, object]] = []

    async def _require_ownership(self, *args):
        return None

    async def _require_generation(self, *args):
        return None

    async def _save_staged(self, version_id, staged, worker_owner_id):
        self.saved.append(copy.deepcopy(staged))


async def run_pipeline(provider, transcript, staged=None):
    runner = IsolatedRunner(provider)
    version = type("Version", (), {"id": "version-1"})()
    result = await runner._day_map_autonomous(
        version,
        transcript,
        [],
        {"provider_id": "kimi", "model_id": "kimi-k2.5"},
        staged if staged is not None else {},
        None,
    )
    return runner, result


@pytest.mark.asyncio
async def test_full_transcript_is_the_default_above_the_compact_threshold() -> None:
    transcript = [segment(index, "录" * 6_000) for index in range(6)]
    provider = RecordingProvider()

    await run_pipeline(provider, transcript)

    assert provider.calls == ["autonomous-day-map", "autonomous-final-analysis"]
    assert provider.request_segment_counts == [6, 6]
    assert provider.native_calls == []


@pytest.mark.asyncio
async def test_no_search_day_map_goes_straight_to_forced_final_pass() -> None:
    provider = RecordingProvider(initial="finalize")

    runner, result = await run_pipeline(provider, [segment(0)])

    assert result.cards[0].title == "最终分析"
    assert provider.calls == ["autonomous-day-map", "autonomous-final-analysis"]
    assert runner.saved[-1]["search_rounds"] == []
    assert runner.saved[-1]["external_sources"] == []
    assert runner.saved[-1]["search_phase"] == {
        "status": "finalized",
        "decision": decision(),
        "completed_rounds": 0,
    }
    assert "autonomous" in runner.saved[-1]


@pytest.mark.asyncio
async def test_supported_search_executes_and_checkpoints_at_most_five_rounds() -> None:
    provider = RecordingProvider(initial="search", followup="search")

    runner, _ = await run_pipeline(provider, [segment(0)])

    assert [item[0] for item in provider.native_calls] == [1, 2, 3, 4, 5]
    assert len(runner.saved[-1]["search_rounds"]) == 5
    assert len(runner.saved[-1]["external_sources"]) == 5
    assert len(provider.final_sources) == 5


class UnsupportedSearchProvider(RecordingProvider):
    async def native_search(
        self, provider_id, *, queries, round_number, model_id=None, timeout_seconds=60
    ):
        self.native_calls.append((round_number, list(queries)))
        return NativeSearchCallResult(
            provider_id=provider_id,
            model_id=model_id or "deepseek-v4-pro",
            tool_name=None,
            available=False,
            errors=("Native web search is unavailable.",),
        )


@pytest.mark.asyncio
async def test_unsupported_native_search_continues_with_pure_audio() -> None:
    provider = UnsupportedSearchProvider(initial="search")

    runner, result = await run_pipeline(provider, [segment(0)])

    assert result.cards
    assert provider.native_calls == [(1, ["initial query"])]
    assert provider.final_sources == []
    assert runner.saved[-1]["search_rounds"][0]["errors"] == [
        "Native web search is unavailable."
    ]


class TransientSearchProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__(initial="search", followup="finalize")
        self.attempts = 0

    async def native_search(self, *args, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise ProviderAnalysisError(
                "temporary network failure", retriable=True, code="network_timeout"
            )
        return await super().native_search(*args, **kwargs)


@pytest.mark.asyncio
async def test_transient_native_search_uses_the_existing_single_retry() -> None:
    provider = TransientSearchProvider()

    _, result = await run_pipeline(provider, [segment(0)])

    assert result.cards
    assert provider.attempts == 2


class ExhaustedStructuredTransientProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__(initial="search", followup="finalize")
        self.attempts = 0

    async def native_search(
        self, provider_id, *, queries, round_number, model_id=None, timeout_seconds=60
    ):
        self.attempts += 1
        return NativeSearchCallResult(
            provider_id=provider_id,
            model_id=model_id or "kimi-k2.5",
            tool_name="$web_search",
            available=False,
            errors=("Native web search is temporarily unavailable.",),
            retriable=True,
        )


@pytest.mark.asyncio
async def test_exhausted_structured_search_failure_persists_and_finalizes_audio() -> None:
    provider = ExhaustedStructuredTransientProvider()

    runner, result = await run_pipeline(provider, [segment(0)])

    assert result.cards
    assert provider.attempts == 2
    assert runner.saved[-1]["search_rounds"][0]["errors"] == [
        "Native web search is temporarily unavailable."
    ]
    assert runner.saved[-1]["search_phase"]["status"] == "finalized"


@pytest.mark.asyncio
async def test_resume_executes_the_exact_staged_pending_round() -> None:
    first_source = source(1)
    staged = {
        "day_map": day_map(decision("search", "ignored initial")).model_dump(mode="json"),
        "search_rounds": [
            {
                "round_number": 1,
                "decision": decision("search", "round one"),
                "results": [
                    {
                        "provider_result_id": first_source.provider_result_id,
                        "title": first_source.title,
                        "url": first_source.url,
                        "publisher": first_source.publisher,
                        "published_at": first_source.published_at,
                        "snippet": first_source.support_statement,
                    }
                ],
                "sources": [first_source.model_dump(mode="json")],
                "errors": [],
            },
            {
                "round_number": 2,
                "decision": decision("search", "resume exact query"),
                "results": [],
                "sources": [],
                "errors": [],
            },
        ],
        "external_sources": [first_source.model_dump(mode="json")],
    }
    provider = RecordingProvider(followup="finalize")

    _, _ = await run_pipeline(provider, [segment(0)], staged)

    assert "autonomous-day-map" not in provider.calls
    assert provider.native_calls[0] == (2, ["resume exact query"])
    assert all(round_number != 1 for round_number, _ in provider.native_calls)


class FinalFailureProvider(RecordingProvider):
    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        self.calls.append(request.scene_id)
        raise ProviderAnalysisError("final call failed", code="provider_unavailable")


class InvalidEvidenceThenValidProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_requests = []

    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        self.calls.append(request.scene_id)
        self.final_requests.append(request)
        if len(self.final_requests) == 1:
            result = final_result()
            result.cards[0].evidence_segment_ids = ["unknown-segment"]
            for section in result.cards[0].content:
                section.evidence_segment_ids = ["unknown-segment"]
            return result
        return final_result()


class MixedInvalidEvidenceThenValidProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_requests = []

    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        self.calls.append(request.scene_id)
        self.final_requests.append(request)
        result = final_result()
        if len(self.final_requests) == 1:
            result.cards[0].content[1].evidence_segment_ids = ["unknown-segment"]
        return result


@pytest.mark.asyncio
async def test_final_evidence_failure_gets_one_semantic_retry() -> None:
    provider = InvalidEvidenceThenValidProvider()

    runner, result = await run_pipeline(provider, [segment(0, "录" * 1_000)])

    assert result.cards
    assert provider.calls == [
        "autonomous-day-map",
        "autonomous-final-analysis",
        "autonomous-final-analysis",
    ]
    assert len(provider.final_requests) == 2
    assert "上一轮 JSON 或证据未通过校验" not in provider.final_requests[0].common_rules
    assert "上一轮 JSON 或证据未通过校验" in provider.final_requests[1].common_rules


@pytest.mark.asyncio
async def test_mixed_invalid_final_evidence_preserves_context_for_one_retry() -> None:
    provider = MixedInvalidEvidenceThenValidProvider()

    _, result = await run_pipeline(provider, [segment(0, "录" * 1_000)])

    assert result.cards
    assert len(provider.final_requests) == 2
    assert provider.final_requests[1].user_data == provider.final_requests[0].user_data
    assert "上一轮 JSON 或证据未通过校验" in provider.final_requests[1].common_rules


class AlwaysInvalidEvidenceProvider(RecordingProvider):
    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        result = final_result()
        result.cards[0].evidence_segment_ids = ["unknown-segment"]
        for section in result.cards[0].content:
            section.evidence_segment_ids = ["unknown-segment"]
        return result


@pytest.mark.asyncio
async def test_exhausted_final_evidence_retry_is_typed() -> None:
    with pytest.raises(ProviderAnalysisError) as raised:
        await run_pipeline(
            AlwaysInvalidEvidenceProvider(), [segment(0, "录" * 1_000)]
        )

    assert raised.value.code == "autonomous_final_evidence_invalid"


class EmptyEvidenceCardProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.final_requests = []

    async def analyze_autonomous_final_analysis(
        self, request, provider_snapshot, *, persisted_sources
    ):
        self.calls.append(request.scene_id)
        self.final_requests.append(request)
        result = final_result()
        result.cards[0].evidence_segment_ids = []
        for section in result.cards[0].content:
            section.evidence_segment_ids = []
        return result


@pytest.mark.asyncio
async def test_fresh_and_resumed_final_results_share_canonical_empty_evidence_output() -> None:
    staged: dict[str, object] = {}
    fresh_provider = EmptyEvidenceCardProvider()

    _, fresh = await run_pipeline(fresh_provider, [segment(0)], staged)

    resumed_provider = ResumeFinalOnlyProvider()
    _, resumed = await run_pipeline(resumed_provider, [segment(0)], staged)

    assert fresh.model_dump(mode="json") == {"cards": []}
    assert resumed.model_dump(mode="json") == fresh.model_dump(mode="json")
    assert staged["autonomous"] == fresh.model_dump(mode="json")
    assert fresh_provider.calls == [
        "autonomous-day-map",
        "autonomous-final-analysis",
    ]
    assert resumed_provider.calls == []


@pytest.mark.asyncio
async def test_large_canonical_empty_final_result_retries_consistently_fresh_and_resumed() -> None:
    transcript = [segment(0, "录" * 1_000)]
    staged: dict[str, object] = {}
    fresh_provider = EmptyEvidenceCardProvider()

    with pytest.raises(ProviderAnalysisError) as fresh_error:
        await run_pipeline(fresh_provider, transcript, staged)

    assert fresh_error.value.code == "autonomous_final_evidence_invalid"
    assert "autonomous" not in staged
    assert len(fresh_provider.final_requests) == 2
    assert fresh_provider.final_requests[1].user_data == (
        fresh_provider.final_requests[0].user_data
    )
    assert "上一轮 JSON 或证据未通过校验" in (
        fresh_provider.final_requests[1].common_rules
    )

    # Simulate a checkpoint made by the previous fresh-path behavior.
    staged["autonomous"] = {"cards": []}
    resumed_provider = EmptyEvidenceCardProvider()

    with pytest.raises(ProviderAnalysisError) as resumed_error:
        await run_pipeline(resumed_provider, transcript, staged)

    assert resumed_error.value.code == fresh_error.value.code
    assert "autonomous" not in staged
    assert len(resumed_provider.final_requests) == 1
    assert "上一轮 JSON 或证据未通过校验" in (
        resumed_provider.final_requests[0].common_rules
    )


class DirectInvalidEvidenceThenValidProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.direct_requests = []

    async def analyze_autonomous(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        self.direct_requests.append(request)
        result = final_result()
        if len(self.direct_requests) == 1:
            result.cards[0].evidence_segment_ids = ["unknown-segment"]
            for section in result.cards[0].content:
                section.evidence_segment_ids = ["unknown-segment"]
        return result


@pytest.mark.asyncio
async def test_direct_route_retries_raw_invalid_evidence_before_sanitization() -> None:
    provider = DirectInvalidEvidenceThenValidProvider()
    runner = IsolatedRunner(provider)
    version = type("Version", (), {"id": "version-1"})()

    result = await runner._direct_autonomous(
        version,
        [segment(0)],
        [],
        {"provider_id": "kimi", "model_id": "kimi-k2.5"},
        {},
        None,
    )

    assert result.cards
    assert len(provider.direct_requests) == 2
    assert "上一轮 JSON 或证据未通过校验" in provider.direct_requests[1].common_rules


class DirectCanonicalEmptyEvidenceProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.direct_requests = []

    async def analyze_autonomous(self, request, provider_snapshot):
        self.calls.append(request.scene_id)
        self.direct_requests.append(request)
        result = final_result()
        result.cards[0].evidence_segment_ids = []
        for section in result.cards[0].content:
            section.evidence_segment_ids = []
        return result


@pytest.mark.asyncio
async def test_direct_large_canonical_empty_result_retries_then_raises_typed_error() -> None:
    provider = DirectCanonicalEmptyEvidenceProvider()
    runner = IsolatedRunner(provider)
    version = type("Version", (), {"id": "version-1"})()
    staged: dict[str, object] = {}

    with pytest.raises(ProviderAnalysisError) as raised:
        await runner._direct_autonomous(
            version,
            [segment(0, "录" * 1_000)],
            [],
            {"provider_id": "kimi", "model_id": "kimi-k2.5"},
            staged,
            None,
        )

    assert raised.value.code == "autonomous_evidence_invalid"
    assert "autonomous" not in staged
    assert len(provider.direct_requests) == 2
    assert provider.direct_requests[1].user_data == provider.direct_requests[0].user_data
    assert "上一轮 JSON 或证据未通过校验" in provider.direct_requests[1].common_rules


class ResumeFinalOnlyProvider(RecordingProvider):
    async def analyze_autonomous_day_map(self, *args, **kwargs):
        raise AssertionError("resume must use staged Day Map")

    async def analyze_autonomous_search_loop(self, *args, **kwargs):
        raise AssertionError("resume must use staged terminal search phase")

    async def native_search(self, *args, **kwargs):
        raise AssertionError("resume must not repeat completed native search")


@pytest.mark.asyncio
async def test_terminal_decision_is_checkpointed_before_final_and_resumed_exactly() -> None:
    staged: dict[str, object] = {}
    first = FinalFailureProvider(initial="search", followup="finalize")

    with pytest.raises(ProviderAnalysisError, match="final call failed"):
        await run_pipeline(first, [segment(0)], staged)

    assert staged["search_phase"] == {
        "status": "finalized",
        "decision": decision(),
        "completed_rounds": 1,
    }
    resumed = ResumeFinalOnlyProvider()
    _, result = await run_pipeline(resumed, [segment(0)], staged)

    assert result.cards
    assert resumed.calls == ["autonomous-final-analysis"]
    assert resumed.native_calls == []


class FallbackRunner(IsolatedRunner):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.fallback_calls = 0

    async def _long_autonomous(self, *args):
        self.fallback_calls += 1
        return final_result(), args[2]


class RejectingProvider(RecordingProvider):
    def __init__(self, code: str):
        super().__init__()
        self.code = code

    async def analyze_autonomous_day_map(self, request, provider_snapshot):
        raise ProviderAnalysisError("rejected", code=self.code)


@pytest.mark.asyncio
async def test_only_explicit_provider_input_rejection_uses_compact_route() -> None:
    transcript = [segment(index, "录" * 6_000) for index in range(6)]
    version = type("Version", (), {"id": "version-1"})()

    ordinary = FallbackRunner(RejectingProvider("content_rejected"))
    with pytest.raises(ProviderAnalysisError):
        await ordinary._autonomous_with_fallback(
            version, transcript, [], {"provider_id": "kimi"}, {}, None
        )
    assert ordinary.fallback_calls == 0

    explicit = FallbackRunner(RejectingProvider("provider_input_rejected"))
    result, profile_transcript = await explicit._autonomous_with_fallback(
        version, transcript, [], {"provider_id": "kimi"}, {}, None
    )
    assert result.cards
    assert profile_transcript == transcript
    assert explicit.fallback_calls == 1


class ConfiguredKeychain:
    def read(self, provider_id: str) -> KeychainReadResult:
        return KeychainReadResult(KeychainStatus.CONFIGURED, b"test-only-secret")


@pytest.mark.asyncio
async def test_provider_413_is_typed_as_explicit_input_rejection() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": {"message": "request too large"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ProviderAnalysisClient(ConfiguredKeychain(), client)
        with pytest.raises(ProviderAnalysisError) as raised:
            await provider.generate(
                "deepseek", system="rules", user="large input", scene_id="day-map"
            )

    assert raised.value.code == "provider_input_rejected"


class RecordingProfileExtractor:
    def __init__(self) -> None:
        self.cards = None

    async def extract(self, transcript, cards, existing, provider_snapshot):
        self.cards = cards
        return [
            {
                "subject_id": "user",
                "dimension": "safe",
                "value": {"statement": "原录音里的长期偏好"},
                "confidence": 0.9,
                "explicit": True,
                "evidence_segment_ids": ["seg_0"],
            },
            {
                "subject_id": "user",
                "dimension": "polluted",
                "value": {"source": "https://example.test/1"},
                "confidence": 0.9,
                "explicit": True,
                "evidence_segment_ids": ["seg_0"],
            },
            {
                "subject_id": "user",
                "dimension": "external-text",
                "value": {"statement": "External support text 1"},
                "confidence": 0.9,
                "explicit": True,
                "evidence_segment_ids": ["seg_0"],
            },
        ]


class FullRunHarness(AnalysisRunner):
    def __init__(self, provider, extractor):
        self.provider = provider
        self.profile_extractor = extractor
        self.publisher = self
        self.generation_source = self
        from audio_memory.prompts.composer import PromptComposer

        self.composer = PromptComposer()
        self.version = type(
            "Version",
            (),
            {
                "id": "version-1",
                "source_job_id": "job-1",
                "provider_id": "kimi",
                "model_id": "kimi-k2.5",
                "credential_generation": 1,
                "profile_snapshot_json": "[]",
                "staged_results_json": "{}",
            },
        )()
        self.saved_candidates = None

    async def _version(self, *args):
        return self.version

    async def _require_fixed_rules(self, *args):
        return None

    async def _require_ownership(self, *args):
        return None

    async def _require_generation(self, *args):
        return None

    async def _transcript(self, *args):
        return [segment(0)]

    async def _save_staged(self, version_id, staged, worker_owner_id):
        self.version.staged_results_json = json.dumps(staged)

    async def _save_profile_candidates(
        self, version_id, raw_candidates, segment_ids, worker_owner_id
    ):
        self.saved_candidates = raw_candidates
        return raw_candidates

    @asynccontextmanager
    async def publication_guard(self, provider_id):
        yield 1

    async def credential_generation(self, provider_id):
        return 1

    async def publish(self, *args, **kwargs):
        return AnalysisOutcome("batch-1", 2, 0)


class EmptyProfileExtractor:
    async def extract(self, transcript, cards, existing, provider_snapshot):
        return []


class FullFallbackPublicationHarness(FullRunHarness):
    def __init__(self, provider):
        super().__init__(provider, EmptyProfileExtractor())
        self.published_overview = None

    async def _transcript(self, *args):
        return [segment(index, "录" * 6_000) for index in range(6)]

    async def _long_autonomous(self, *args):
        return final_result(), args[2]

    async def publish(self, version_id, results, profile_delta, **kwargs):
        publication = VersionPublisher._day_map_publication(
            self.version, results.cards
        )
        assert publication is not None
        self.published_overview = publication.overview
        return AnalysisOutcome("batch-1", len(results.cards) + 1, 0)


@pytest.mark.asyncio
async def test_full_fallback_run_stages_and_publishes_one_compatible_overview() -> None:
    runner = FullFallbackPublicationHarness(
        RejectingProvider("provider_input_rejected")
    )

    outcome = await runner.run("version-1")

    staged = json.loads(runner.version.staged_results_json)
    assert outcome.card_count == 2
    assert runner.published_overview is not None
    assert runner.published_overview.title == "本次概览"
    assert staged["fallback"] == {
        "route": "compact",
        "reason": "provider_input_rejected",
    }
    assert staged["day_map"]["overview"]["title"] == "本次概览"


@pytest.mark.asyncio
async def test_profile_extraction_receives_only_transcript_based_evidence() -> None:
    provider = RecordingProvider(initial="search", followup="finalize")
    extractor = RecordingProfileExtractor()
    runner = FullRunHarness(provider, extractor)

    await runner.run("version-1")

    assert extractor.cards == []
    assert runner.saved_candidates is not None
    assert [item["dimension"] for item in runner.saved_candidates] == ["safe"]
