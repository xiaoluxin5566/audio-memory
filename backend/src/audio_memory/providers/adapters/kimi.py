from __future__ import annotations

from typing import Sequence

from audio_memory.providers.adapters.base import (
    ChatCompletionsAdapter,
    NativeSearchCapability,
)


class KimiAdapter(ChatCompletionsAdapter):
    _WEB_SEARCH_TOOL = {
        "type": "builtin_function",
        "function": {"name": "$web_search"},
    }

    def native_search_capability(self, *, model_id: str) -> NativeSearchCapability:
        return NativeSearchCapability(
            provider_id=str(self.config.provider_id),
            model_id=model_id,
            available=True,
            tool_name="$web_search",
        )

    def validation_payload(self, *, model_id: str | None = None) -> dict[str, object]:
        payload = super().validation_payload(model_id=model_id)
        payload.pop("temperature", None)
        payload.pop("max_tokens", None)
        if (model_id or self.config.model_id) == self.config.model_id:
            payload["reasoning_effort"] = "low"
            payload["max_completion_tokens"] = 64
        else:
            payload["max_tokens"] = 64
        return payload

    def analysis_payload(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("model") != self.config.model_id:
            return payload
        normalized = {
            key: value
            for key, value in payload.items()
            if key not in {"temperature", "thinking", "max_tokens"}
        }
        max_tokens = payload.get("max_tokens")
        if isinstance(max_tokens, int):
            normalized["max_completion_tokens"] = max_tokens
        normalized["reasoning_effort"] = "low"
        return normalized

    def native_search_payload(
        self,
        *,
        model_id: str,
        messages: Sequence[dict[str, object]],
        queries: Sequence[str],
    ) -> dict[str, object]:
        if not messages:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Use the built-in $web_search tool to find primary sources for "
                        "the requested queries. Return citations with their original IDs, "
                        "titles, and URLs."
                    ),
                },
                {
                    "role": "user",
                    "content": "\n".join(f"- {query}" for query in queries),
                },
            ]
        payload: dict[str, object] = {
            "model": model_id,
            "messages": list(messages),
            "stream": False,
            "tools": [self._WEB_SEARCH_TOOL],
        }
        if model_id == self.config.model_id:
            payload["reasoning_effort"] = "low"
        return payload

    def native_search_tool_messages(self, body: object) -> list[dict[str, object]] | None:
        choice = self._choice(body)
        finish_reason = choice.get("finish_reason")
        if finish_reason != "tool_calls":
            return None
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("Kimi native search response has no assistant message.")
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            raise ValueError("Kimi native search response has no tool calls.")

        messages: list[dict[str, object]] = [dict(message)]
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                raise ValueError("Kimi native search returned an invalid tool call.")
            function = tool_call.get("function")
            if not isinstance(function, dict) or function.get("name") != "$web_search":
                raise ValueError("Kimi native search returned an unexpected tool call.")
            call_id = tool_call.get("id")
            arguments = function.get("arguments")
            if not isinstance(call_id, str) or not isinstance(arguments, str):
                raise ValueError("Kimi native search returned an invalid tool call.")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": "$web_search",
                    # Moonshot requires the original generated arguments verbatim.
                    "content": arguments,
                }
            )
        return messages

    def native_search_citations(self, body: object) -> list[object]:
        choice = self._choice(body)
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("Kimi native search response has no assistant message.")
        citations = message.get("citations", [])
        if not isinstance(citations, list):
            raise ValueError("Kimi native search citations are invalid.")
        return citations

    def native_search_completed(self, body: object) -> bool:
        return self._choice(body).get("finish_reason") == "stop"

    @staticmethod
    def _choice(body: object) -> dict[str, object]:
        if not isinstance(body, dict):
            raise ValueError("Kimi native search response is not an object.")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Kimi native search response has no choices.")
        return choices[0]
