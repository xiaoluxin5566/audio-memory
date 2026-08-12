from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from pydantic import ValidationError

from audio_memory.analysis.native_search import normalize_search_results
from audio_memory.prompts.day_map_schema import ExternalSource, SearchResultItem
from audio_memory.providers.types import ProviderConfig


@dataclass(frozen=True, slots=True)
class ChatCompletionResult:
    text: str
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class NativeSearchCapability:
    """The search support advertised by one configured provider and model."""

    provider_id: str
    model_id: str
    available: bool
    tool_name: str | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class NativeSearchCallResult:
    """A safe, persistable outcome of one provider-native search round."""

    provider_id: str
    model_id: str
    tool_name: str | None
    available: bool
    sources: tuple[ExternalSource, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatCompletionsAdapter:
    config: ProviderConfig

    def validation_payload(self) -> dict[str, object]:
        return {
            "model": self.config.model_id,
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
            "temperature": 0,
            "max_tokens": 4,
            "stream": False,
        }

    def analysis_payload(self, payload: dict[str, object]) -> dict[str, object]:
        return payload

    def extract_result(self, body: object) -> ChatCompletionResult:
        if not isinstance(body, dict):
            raise ValueError("Response envelope is not an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Response has no choices")
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ValueError("Response has no message")
        content = choice["message"].get("content")
        if not isinstance(content, str):
            raise ValueError("Response has no text content")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ValueError("Response finish reason is invalid")
        return ChatCompletionResult(content, finish_reason)

    def extract_text(self, body: object) -> str:
        return self.extract_result(body).text

    def native_search_capability(self, *, model_id: str) -> NativeSearchCapability:
        """Report a safe fallback unless this adapter proves native support."""
        return NativeSearchCapability(
            provider_id=str(self.config.provider_id),
            model_id=model_id,
            available=False,
            tool_name=None,
            reason="Native web search is not available for this configured provider.",
        )

    def native_search_payload(
        self,
        *,
        model_id: str,
        messages: Sequence[dict[str, object]],
        queries: Sequence[str],
    ) -> dict[str, object]:
        """Build a provider-native request after a positive capability probe."""
        raise ValueError("Native web search is not available for this configured provider.")

    def native_search_tool_messages(self, body: object) -> list[dict[str, object]] | None:
        """Return tool responses for a continuation, or None when a turn is final."""
        return None

    def native_search_completed(self, body: object) -> bool:
        """Confirm that a terminal response completed the native search normally."""
        return True

    def native_search_citations(self, body: object) -> list[object]:
        """Extract provider citations without treating model prose as a source."""
        return []

    def normalize_native_search_citations(
        self,
        *,
        citations: Sequence[object],
        round_number: int,
    ) -> tuple[tuple[ExternalSource, ...], tuple[str, ...]]:
        """Validate returned citations and retain malformed input as call errors."""
        results: list[SearchResultItem] = []
        errors: list[str] = []
        for index, citation in enumerate(citations):
            if not isinstance(citation, dict):
                errors.append(f"Citation {index} is invalid: citation is not an object")
                continue
            try:
                results.append(
                    SearchResultItem(
                        provider_result_id=citation.get("id"),
                        title=citation.get("title"),
                        url=citation.get("url"),
                        publisher=citation.get("publisher"),
                        published_at=citation.get("published_at"),
                        snippet=citation.get("snippet"),
                    )
                )
            except ValidationError as exc:
                detail = exc.errors()[0].get("msg", "invalid citation")
                if isinstance(detail, str) and detail.startswith("Value error, "):
                    detail = detail.removeprefix("Value error, ")
                errors.append(f"Citation {index} is invalid: {detail}")

        try:
            sources = tuple(
                normalize_search_results(
                    provider_id=str(self.config.provider_id),
                    round_number=round_number,
                    results=results,
                )
            )
        except ValueError as exc:
            return (), tuple([*errors, f"Native search results are invalid: {exc}"])
        return sources, tuple(errors)
