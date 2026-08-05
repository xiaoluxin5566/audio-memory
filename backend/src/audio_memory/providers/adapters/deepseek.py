from audio_memory.providers.adapters.base import ChatCompletionsAdapter


class DeepSeekAdapter(ChatCompletionsAdapter):
    def validation_payload(self) -> dict[str, object]:
        payload = super().validation_payload()
        payload["thinking"] = {"type": "disabled"}
        payload["max_tokens"] = 8
        return payload
