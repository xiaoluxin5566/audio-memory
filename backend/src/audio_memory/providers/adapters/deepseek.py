from audio_memory.providers.adapters.base import (
    ChatCompletionsAdapter,
    NativeSearchCapability,
)


class DeepSeekAdapter(ChatCompletionsAdapter):
    def native_search_capability(self, *, model_id: str) -> NativeSearchCapability:
        # The configured chat-completions endpoint has no documented native search
        # tool contract. Do not route this request to a third-party search provider.
        return NativeSearchCapability(
            provider_id=str(self.config.provider_id),
            model_id=model_id,
            available=False,
            tool_name=None,
            reason="Native web search is not available for this configured provider.",
        )

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
