from __future__ import annotations

from dataclasses import dataclass

from audio_memory.providers.types import ProviderConfig


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

    def extract_text(self, body: object) -> str:
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
        return content

