from __future__ import annotations

from audio_memory.prompts.day_map_schema import (
    MAX_SEARCH_ROUNDS,
    ExternalSource,
    NativeSearchDecision,
    SearchResultItem,
    SearchRound,
)


class SearchStateError(ValueError):
    """Raised when provider/model search state cannot be safely persisted."""


def force_final_decision(
    decision: NativeSearchDecision, *, completed_rounds: int
) -> NativeSearchDecision:
    """Return a final decision after the bounded search budget is exhausted."""
    if completed_rounds < 0 or completed_rounds > MAX_SEARCH_ROUNDS:
        raise SearchStateError("completed search rounds must be between zero and five")
    if completed_rounds < MAX_SEARCH_ROUNDS:
        return decision
    return NativeSearchDecision(
        action="finalize",
        rationale="Search round limit reached; finalize with the available sources.",
    )


def validate_search_round(search_round: SearchRound) -> SearchRound:
    """Reject state that would execute or persist a sixth provider search round."""
    if search_round.round_number > MAX_SEARCH_ROUNDS:
        raise SearchStateError("at most five native search rounds are allowed")
    if search_round.decision.action == "finalize" and search_round.results:
        raise SearchStateError("a final search decision cannot have new tool results")
    return search_round


def normalize_search_results(
    *, provider_id: str, round_number: int, results: list[SearchResultItem]
) -> list[ExternalSource]:
    """Convert actual provider results to stable sources without inventing IDs."""
    if not provider_id.strip():
        raise SearchStateError("provider_id is required")
    if not 1 <= round_number <= MAX_SEARCH_ROUNDS:
        raise SearchStateError("search round must be between one and five")

    provider_result_ids = [result.provider_result_id for result in results]
    if len(provider_result_ids) != len(set(provider_result_ids)):
        raise SearchStateError("provider result IDs must be unique within a search round")

    return [
        ExternalSource(
            source_id=f"source_{provider_id}_{result.provider_result_id}",
            provider_id=provider_id,
            provider_result_id=result.provider_result_id,
            title=result.title,
            url=result.url,
            publisher=result.publisher,
            published_at=result.published_at,
            support_statement=result.snippet,
            search_round=round_number,
        )
        for result in results
    ]
