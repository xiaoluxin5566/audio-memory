import json
from hashlib import sha256
from pathlib import Path

import pytest

from audio_memory.analysis.beta8_search import (
    Beta8SearchExecutor,
    canonicalize_source_url,
    project_search_policy,
    search_packets_for_revision_task,
    stable_source_id,
)
from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8OrchestrationResult,
    Beta8SearchPacket,
    Beta8SearchTask,
)
from audio_memory.prompts.day_map_schema import ExternalSource
from audio_memory.providers.adapters.base import NativeSearchCallResult
from audio_memory.providers.adapters.kimi import KimiAdapter
from audio_memory.providers.types import PROVIDER_CONFIGS


_APPROVED_SEARCH_POLICY_FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "beta8"
    / "search-policy-approved-pre-task8-v2.json"
)


def task(key: str, question: str = "query") -> Beta8SearchTask:
    return Beta8SearchTask.model_validate({
        "search_task_key": key, "source_candidate_ids": [f"candidate-{key}"],
        "question": question, "purpose": "核验", "target_decision_keys": ["decision-1"],
        "related_segment_ids": ["seg-1"], "source_requirements": ["官方文档"],
        "jurisdiction": None, "freshness_requirement": None,
    })


def source(
    result_id: str = "result-1", *, url: str = "https://example.com/doc"
) -> ExternalSource:
    return ExternalSource(
        source_id=f"source-{result_id}", provider_id="search-provider",
        provider_result_id=result_id, title="Official", url=url,
        publisher="Example", published_at=None, support_statement="支持 OAuth 2.0",
        search_round=1,
    )


class FakeProvider:
    def __init__(self) -> None:
        self.calls = []

    async def native_search(self, provider_id, *, queries, round_number, model_id, timeout_seconds=60):
        self.calls.append((provider_id, model_id, queries))
        question = queries[0]
        if question == "raise":
            raise RuntimeError("temporary failure")
        if question == "failed":
            return NativeSearchCallResult(provider_id, model_id, None, False, errors=("unavailable",))
        errors = ("one source unavailable",) if question == "partial" else ()
        return NativeSearchCallResult(
            provider_id, model_id, "$web_search", True,
            sources=(source(question),), errors=errors,
        )


class CollidingProvider(FakeProvider):
    async def native_search(self, provider_id, *, queries, round_number, model_id, timeout_seconds=60):
        self.calls.append((provider_id, model_id, queries))
        url = {
            "first": "https://a.example/one",
            "second": "https://b.example/two",
        }[queries[0]]
        return NativeSearchCallResult(
            provider_id, model_id, "$web_search", True,
            sources=(source("same-provider-local-id", url=url),), errors=(),
        )


@pytest.mark.asyncio
async def test_search_task_count_and_adoption_policy_are_unchanged() -> None:
    approved = json.loads(_APPROVED_SEARCH_POLICY_FIXTURE.read_text(encoding="utf-8"))
    fixture_root = _APPROVED_SEARCH_POLICY_FIXTURE.parent
    for source in approved["provenance"]["durable_sources"].values():
        source_path = fixture_root / source["path"]
        assert sha256(source_path.read_bytes()).hexdigest() == source["sha256"]
    kimi_source = (
        Path(__file__).parents[3]
        / "src" / "audio_memory" / "providers" / "adapters" / "kimi.py"
    )
    assert sha256(kimi_source.read_bytes()).hexdigest() == (
        approved["provenance"]["kimi_source_sha256"]
    )
    prompt = (
        Path(__file__).parents[3]
        / "src" / "audio_memory" / "prompts" / "beta8"
        / "cross-card-orchestration.md"
    ).read_text(encoding="utf-8")
    search_section = prompt.split("## 第四项任务：统筹搜索", 1)[1].split(
        "## 第五项任务：统筹待办", 1
    )[0]
    approved_section_hash = approved["provenance"]["search_orchestration_section_sha256"]
    assert sha256(
        ("## 第四项任务：统筹搜索" + search_section).encode("utf-8")
    ).hexdigest() == approved_section_hash

    projected = {}
    for case in approved["cases"]:
        orchestration = Beta8OrchestrationResult.model_validate(case["orchestration"])
        for pipeline_kind in ("beta8_multi_scene_v1", "beta8_indexed_scene_v2"):
            projected[(case["name"], pipeline_kind)] = project_search_policy(
                search_candidates=case["search_candidates"],
                orchestration=orchestration,
            )
            assert projected[(case["name"], pipeline_kind)] == case["expected"]

    zero_provider = FakeProvider()
    zero_packets = await Beta8SearchExecutor(
        zero_provider, provider_id="kimi", model_id="kimi-k2.6"
    ).execute([], completed={}, persist=lambda packet: _persist(packet, []))
    assert zero_packets == ()
    assert zero_provider.calls == []

    multi = next(case for case in approved["cases"] if case["name"] == "multi_tasks")
    orchestration = Beta8OrchestrationResult.model_validate(multi["orchestration"])
    tasks = orchestration.search_tasks

    payload = KimiAdapter(PROVIDER_CONFIGS["kimi"]).native_search_payload(
        model_id="kimi-k2.6", messages=[],
        queries=[task.question for task in tasks],
    )
    assert payload == approved["kimi_k2_6_request_shape"]

    class ApprovedPolicyProvider:
        def __init__(self) -> None:
            self.calls = []

        async def native_search(
            self, provider_id, *, queries, round_number, model_id,
            timeout_seconds=60,
        ):
            self.calls.append((provider_id, model_id, queries, round_number))
            return NativeSearchCallResult(
                provider_id, model_id, "$web_search", True,
                sources=(ExternalSource(
                    source_id=f"source-{len(self.calls)}",
                    provider_id=provider_id,
                    provider_result_id=f"result-{len(self.calls)}",
                    title="Official",
                    url=f"https://example.com/doc-{len(self.calls)}",
                    publisher="Example",
                    published_at=None,
                    support_statement="支持",
                    search_round=round_number,
                ),),
                errors=(),
            )

    provider = ApprovedPolicyProvider()
    packets = await Beta8SearchExecutor(
        provider, provider_id="kimi", model_id="kimi-k2.6"
    ).execute(tasks, completed={}, persist=lambda packet: _persist(packet, []))
    assert len(packets) == 2
    assert [packet.search_task_id for packet in packets] == ["search-1", "search-2"]
    assert [packet.status for packet in packets] == ["success", "success"]
    assert provider.calls == [
        ("kimi", "kimi-k2.6", ["问题 A"], 1),
        ("kimi", "kimi-k2.6", ["问题 B"], 1),
    ]

    packet_by_id = {packet.search_task_id: packet for packet in packets}
    delivered = {
        task.revision_task_key: [
            packet.search_task_id
            for packet in search_packets_for_revision_task(task, packet_by_id)
        ]
        for task in orchestration.revision_tasks
    }
    assert delivered == {
        "revision-1": ["search-1"],
        "revision-2": ["search-2"],
        "revision-3": ["search-2"],
    }


def test_search_policy_projection_does_not_add_pre_task8_rejections() -> None:
    approved = json.loads(_APPROVED_SEARCH_POLICY_FIXTURE.read_text(encoding="utf-8"))
    multi = next(case for case in approved["cases"] if case["name"] == "multi_tasks")
    data = json.loads(json.dumps(multi["orchestration"], ensure_ascii=False))
    data["search_tasks"][1]["search_task_key"] = "search-1"
    data["search_tasks"][1]["question"] = "  问题 a  "
    data["search_tasks"][1]["target_decision_keys"] = ["legacy-unknown"]
    data["revision_tasks"][0]["search_task_keys"] = []
    data["revision_tasks"][1]["search_task_keys"] = ["search-1"]
    data["revision_tasks"][2]["search_task_keys"] = []
    orchestration = Beta8OrchestrationResult.model_validate(data)

    projection = project_search_policy(
        search_candidates=multi["search_candidates"],
        orchestration=orchestration,
    )

    assert projection["task_order"] == ["search-1", "search-1"]
    assert projection["tasks"][1]["question"] == "  问题 a  "
    assert projection["tasks"][1]["target_decision_keys"] == ["legacy-unknown"]
    assert projection["adoption"][0]["search_task_keys"] == []


def test_canonicalize_source_url_removes_tracking_and_preserves_other_query_items() -> None:
    assert canonicalize_source_url(
        "HTTPS://Example.COM:443?b=2&utm_source=campaign&a=1&gclid=click#section"
    ) == "https://example.com/?b=2&a=1"


def test_canonicalize_source_url_is_lossless_for_percent_encoding_and_query_shape() -> None:
    assert canonicalize_source_url("https://example.com/?x=%ff&a&a=") == (
        "https://example.com/?x=%FF&a&a="
    )
    assert canonicalize_source_url("https://example.com/?x=%FE") == "https://example.com/?x=%FE"
    assert stable_source_id("https://example.com/?x=%FF") != stable_source_id(
        "https://example.com/?x=%FE"
    )
    assert canonicalize_source_url("https://[2001:DB8::1]:443/%7e") == (
        "https://[2001:db8::1]/~"
    )
    assert canonicalize_source_url("https://[2001:DB8::1]:8443/a") == (
        "https://[2001:db8::1]:8443/a"
    )
    assert stable_source_id("https://example.com/?a=1&a=2") != stable_source_id(
        "https://example.com/?a=2&a=1"
    )
    assert stable_source_id("https://example.com/?a") != stable_source_id(
        "https://example.com/?a="
    )


@pytest.mark.parametrize("unicode_port", ["１２", "١٢"])
def test_bracketed_ipv6_unicode_decimal_port_cannot_alias_ascii_port(
    unicode_port: str,
) -> None:
    ascii_url = "https://[2001:db8::1]:12/a"
    assert canonicalize_source_url(ascii_url) == ascii_url

    invalid_url = f"https://[2001:db8::1]:{unicode_port}/a"
    with pytest.raises(ValueError, match="invalid port suffix"):
        canonicalize_source_url(invalid_url)
    with pytest.raises(ValueError, match="invalid port suffix"):
        stable_source_id(invalid_url)


@pytest.mark.parametrize("url", [
    "https://user@example.com/a", "https://exa%6dple.com/a",
    "https://example.com/%", "https://example.com/?x=%Q0",
    "https://example.com\\evil/a", "https://example.com^evil/a",
    "https://example.com\x7f/a", "https://[v1.fe80]/a",
    "https://[2001:db8::1]junk/a", "https://[2001:db8::1]abc:443/a",
    "https://[2001:db8::1]:/a", "https://[2001:db8::1]:abc/a",
])
def test_canonicalize_source_url_rejects_unsafe_authorities_and_percent_triplets(url: str) -> None:
    with pytest.raises(ValueError, match="URL|percent"):
        canonicalize_source_url(url)


def test_same_canonical_url_across_tasks_gets_one_source_id() -> None:
    assert stable_source_id("HTTPS://Example.com/a?utm_source=x") == (
        "source_2dce0a4c50441bfccfa9caf4b58c3cba6e06c420505dd829f0436de1aa44baac"
    )


def test_different_canonical_urls_get_different_stable_source_ids() -> None:
    assert stable_source_id("https://a.example/one") == (
        "source_06aeb7f757afefef8b9dfe269b55ea6f24b7ac1bdadc9916971824b18e5b7384"
    )
    assert stable_source_id("https://b.example/two") == (
        "source_42e00a8fab3b687b3a3b4a069b48212218cfc559b72a8012c5bd61d0e30196d2"
    )


@pytest.mark.asyncio
async def test_same_provider_local_id_with_different_urls_remains_distinct_across_tasks() -> None:
    provider = CollidingProvider()
    persisted = []
    packets = await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute(
        [task("first", "first"), task("second", "second")],
        completed={}, persist=lambda packet: _persist(packet, persisted),
    )

    source_ids = [packet.sources[0].source_id for packet in packets]
    assert source_ids == [
        "source_06aeb7f757afefef8b9dfe269b55ea6f24b7ac1bdadc9916971824b18e5b7384",
        "source_42e00a8fab3b687b3a3b4a069b48212218cfc559b72a8012c5bd61d0e30196d2",
    ]
    assert packets[0].supported_points[0].source_ids == [source_ids[0]]
    assert packets[1].supported_points[0].source_ids == [source_ids[1]]
    assert persisted == list(packets)
    assert provider.calls == [
        ("search-provider", "search-model", ["first"]),
        ("search-provider", "search-model", ["second"]),
    ]


@pytest.mark.asyncio
async def test_same_canonical_source_from_two_executed_tasks_dedupes_in_strict_registry() -> None:
    class SameSourceProvider(FakeProvider):
        async def native_search(self, provider_id, *, queries, round_number, model_id, timeout_seconds=60):
            self.calls.append((provider_id, model_id, queries))
            url = "HTTPS://Example.com:443/doc?utm_source=" + queries[0]
            return NativeSearchCallResult(
                provider_id, model_id, "$web_search", True,
                sources=(source(queries[0], url=url),), errors=(),
            )

    from audio_memory.prompts.beta8_pipeline_schema import build_strict_source_registry

    provider = SameSourceProvider()
    packets = await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute(
        [task("one", "one"), task("two", "two")],
        completed={}, persist=lambda value: _persist(value, []),
    )
    registry = build_strict_source_registry(tuple(packets))

    assert len(registry) == 1
    assert registry[0].url == "https://example.com/doc"
    assert provider.calls == [
        ("search-provider", "search-model", ["one"]),
        ("search-provider", "search-model", ["two"]),
    ]


@pytest.mark.asyncio
async def test_invalid_provider_source_is_filtered_without_discarding_valid_sources() -> None:
    class MixedProvider(FakeProvider):
        async def native_search(self, provider_id, *, queries, round_number, model_id, timeout_seconds=60):
            self.calls.append((provider_id, model_id, queries))
            return NativeSearchCallResult(
                provider_id, model_id, "$web_search", True,
                sources=(source("valid"), source("invalid", url="https://example.com/%")), errors=(),
            )

    provider = MixedProvider()
    packet = (await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute([task("one")], completed={}, persist=lambda value: _persist(value, [])))[0]

    assert packet.status == "partial"
    assert [item.url for item in packet.sources] == ["https://example.com/doc"]
    assert packet.supported_points[0].source_ids == [packet.sources[0].source_id]
    assert packet.unresolved_points and "invalid" in packet.unresolved_points[0]
    assert provider.calls == [("search-provider", "search-model", ["query"])]


@pytest.mark.asyncio
async def test_completed_checkpoint_is_normalized_before_reuse() -> None:
    checkpoint = {
        "search_task_id": "done", "status": "success", "answer": "支持 OAuth 2.0",
        "supported_points": [{"point": "支持 OAuth 2.0", "source_ids": ["provider-local"]}],
        "unresolved_points": [], "error_summary": None,
        "sources": [{
            "source_id": "provider-local", "title": "Official",
            "url": "HTTPS://Example.com:443/doc?utm_source=x", "publisher": "Example",
            "published_at": None, "retrieved_at": "2026-09-02T12:00:00+08:00",
            "source_type": "provider_native_search", "supports": [0],
        }],
    }
    from audio_memory.prompts.beta8_pipeline_schema import Beta8SearchPacket

    provider = FakeProvider()
    packet = (await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute(
        [task("done")], completed={"done": Beta8SearchPacket.model_validate(checkpoint)},
        persist=lambda value: _persist(value, []),
    ))[0]

    assert packet.sources[0].url == "https://example.com/doc"
    assert packet.sources[0].source_id == stable_source_id("https://example.com/doc")
    assert packet.supported_points[0].source_ids == [packet.sources[0].source_id]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_completed_checkpoint_with_one_local_id_for_two_urls_fails_before_provider_call() -> None:
    from audio_memory.prompts.beta8_pipeline_schema import Beta8SearchPacket

    conflict = Beta8SearchPacket.model_validate({
        "search_task_id": "done", "status": "success", "answer": "支持",
        "supported_points": [{"point": "支持", "source_ids": ["provider-local"]}],
        "unresolved_points": [], "error_summary": None,
        "sources": [
            {"source_id": "provider-local", "title": "One", "url": "https://a.example/one",
             "publisher": "Example", "published_at": None, "retrieved_at": "2026-09-02T12:00:00+08:00",
             "source_type": "provider_native_search", "supports": [0]},
            {"source_id": "provider-local", "title": "Two", "url": "https://b.example/two",
             "publisher": "Example", "published_at": None, "retrieved_at": "2026-09-02T12:00:00+08:00",
             "source_type": "provider_native_search", "supports": [0]},
        ],
    })
    provider = FakeProvider()

    with pytest.raises(ValueError, match="multiple canonical URLs"):
        await Beta8SearchExecutor(
            provider, provider_id="search-provider", model_id="search-model"
        ).execute([task("done")], completed={"done": conflict}, persist=lambda value: _persist(value, []))
    assert provider.calls == []


@pytest.mark.asyncio
async def test_search_executor_skips_network_for_zero_tasks() -> None:
    provider = FakeProvider()
    packets = await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute([], completed={}, persist=lambda packet: _persist(packet, []))
    assert packets == ()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_search_executor_uses_independent_provider_and_model() -> None:
    provider = FakeProvider()
    await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute([task("one")], completed={}, persist=lambda packet: _persist(packet, []))
    assert provider.calls == [("search-provider", "search-model", ["query"])]


@pytest.mark.asyncio
async def test_search_executor_persists_success_partial_and_failed_packets() -> None:
    provider = FakeProvider()
    persisted = []
    packets = await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute(
        [task("success"), task("partial", "partial"), task("failed", "failed")],
        completed={}, persist=lambda packet: _persist(packet, persisted),
    )
    assert [packet.status for packet in packets] == ["success", "partial", "failed"]
    assert persisted == list(packets)


@pytest.mark.asyncio
async def test_one_failed_task_does_not_discard_successful_tasks() -> None:
    provider = FakeProvider()
    packets = await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute(
        [task("bad", "raise"), task("good")], completed={},
        persist=lambda packet: _persist(packet, []),
    )
    assert [packet.status for packet in packets] == ["failed", "success"]


@pytest.mark.asyncio
async def test_search_executor_never_retries_completed_task() -> None:
    provider = FakeProvider()
    completed_packet = await _make_completed_packet()
    packets = await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute(
        [task("done"), task("new")], completed={"done": completed_packet},
        persist=lambda packet: _persist(packet, []),
    )
    assert packets[0] is completed_packet
    assert provider.calls == [("search-provider", "search-model", ["query"])]


@pytest.mark.asyncio
async def test_missing_search_provider_degrades_without_using_report_provider() -> None:
    provider = FakeProvider()
    packets = await Beta8SearchExecutor(provider, provider_id=None, model_id=None).execute(
        [task("one")], completed={}, persist=lambda packet: _persist(packet, []),
    )
    assert packets[0].status == "failed"
    assert provider.calls == []


async def _persist(packet, target):
    target.append(packet)


async def _make_completed_packet():
    persisted = []
    provider = FakeProvider()
    return (await Beta8SearchExecutor(
        provider, provider_id="search-provider", model_id="search-model"
    ).execute([task("done")], completed={}, persist=lambda packet: _persist(packet, persisted)))[0]
