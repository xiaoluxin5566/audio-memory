from __future__ import annotations

from dataclasses import dataclass

from audio_memory.providers.types import ProviderConfig


@dataclass(frozen=True, slots=True)
class ChatCompletionResult:
    text: str
    finish_reason: str | None


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
