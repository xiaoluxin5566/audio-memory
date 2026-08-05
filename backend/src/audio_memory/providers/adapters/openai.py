from audio_memory.providers.adapters.base import ChatCompletionsAdapter


class OpenAIAdapter(ChatCompletionsAdapter):
    def validation_payload(self) -> dict[str, object]:
        return {
            "model": self.config.model_id,
            "input": "Reply exactly: OK",
            "max_output_tokens": 4,
            "store": False,
        }

    def extract_text(self, body: object) -> str:
        if not isinstance(body, dict):
            raise ValueError("Response envelope is not an object")
        direct = body.get("output_text")
        if isinstance(direct, str):
            return direct
        output = body.get("output")
        if not isinstance(output, list):
            raise ValueError("Response has no output")
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    return part["text"]
        raise ValueError("Response has no text output")
