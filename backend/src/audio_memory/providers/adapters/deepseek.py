from audio_memory.providers.adapters.base import ChatCompletionsAdapter


class DeepSeekAdapter(ChatCompletionsAdapter):
    def analysis_payload(self, payload: dict[str, object]) -> dict[str, object]:
        return {
            **payload,
            "thinking": payload.get("thinking", {"type": "enabled"}),
        }

    def validation_payload(self) -> dict[str, object]:
        payload = super().validation_payload()
        payload["thinking"] = {"type": "disabled"}
        payload["max_tokens"] = 8
        return payload
