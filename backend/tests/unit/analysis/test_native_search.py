from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_memory.analysis.native_search import (
    MAX_SEARCH_ROUNDS,
    SearchStateError,
    force_final_decision,
    normalize_search_results,
    validate_search_round,
)
from audio_memory.prompts.day_map_schema import (
    NativeSearchDecision,
    NativeSearchQuery,
    SearchResultItem,
    SearchRound,
)


def search_decision() -> NativeSearchDecision:
    return NativeSearchDecision(
        action="search",
        rationale="还需要交叉核验。",
        queries=[NativeSearchQuery(query="可靠资料", purpose="交叉核验")],
    )


def test_search_rounds_are_limited_to_five() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 5"):
        SearchRound(
            round_number=MAX_SEARCH_ROUNDS + 1,
            decision=search_decision(),
        )


def test_search_results_require_stable_provider_identifiers_and_urls() -> None:
    with pytest.raises(ValidationError):
        SearchResultItem(provider_result_id="", title="来源", url="https://example.com")
    with pytest.raises(ValidationError):
        SearchResultItem(provider_result_id="kimi-42", title="来源", url="not a url")


def test_normalization_preserves_provider_and_source_identifiers() -> None:
    sources = normalize_search_results(
        provider_id="kimi",
        round_number=1,
        results=[SearchResultItem(provider_result_id="result_42", title="官方资料", url="https://example.com/a")],
    )

    assert sources[0].source_id == "source_kimi_result_42"
    assert sources[0].provider_id == "kimi"
    assert sources[0].provider_result_id == "result_42"


def test_fifth_round_forces_finalization() -> None:
    final = force_final_decision(search_decision(), completed_rounds=MAX_SEARCH_ROUNDS)

    assert final.action == "finalize"
    assert final.queries == []


def test_search_round_rejects_a_sixth_round() -> None:
    with pytest.raises(SearchStateError):
        validate_search_round(
            SearchRound.model_construct(round_number=6, decision=search_decision())
        )
