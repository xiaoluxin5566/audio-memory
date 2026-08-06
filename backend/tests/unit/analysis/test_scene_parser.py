import pytest

from audio_memory.analysis.parser import SceneOutputError, parse_scene_output
from audio_memory.analysis.provider import RemoteSceneAnalyzer
from audio_memory.prompts.composer import ModelRequest


def test_parser_accepts_valid_scene_json() -> None:
    result = parse_scene_output(
        """{"scene_id":"growth","should_generate":false,"generation_reason":"没有足够证据","cards":[],"todos":[],"confidence":0.0}""",
        expected_scene="growth",
    )

    assert result.should_generate is False


def test_parser_rejects_wrong_scene_and_markdown_wrapper() -> None:
    with pytest.raises(SceneOutputError):
        parse_scene_output(
            '{"scene_id":"meeting","should_generate":false,"generation_reason":"没有足够证据","cards":[],"todos":[],"confidence":0.0}',
            expected_scene="todo",
        )
    with pytest.raises(SceneOutputError):
        parse_scene_output("```json\n{}\n```", expected_scene="todo")


@pytest.mark.asyncio
async def test_schema_failure_gets_exactly_one_repair_attempt() -> None:
    class FakeClient:
        def __init__(self):
            self.responses = [
                "not-json",
                '{"scene_id":"meeting","should_generate":false,"generation_reason":"没有足够证据","cards":[],"todos":[],"confidence":0.0}',
            ]
            self.calls = 0

        async def generate(self, *args, **kwargs):
            self.calls += 1
            return self.responses.pop(0)

    client = FakeClient()
    analyzer = RemoteSceneAnalyzer(client)
    request = ModelRequest("meeting", 1, 1, "rules", "prompt", "data", "{}")

    result = await analyzer.analyze_scene(
        "meeting",
        request,
        {"provider_id": "kimi", "model_id": "kimi-k2.5"},
    )

    assert result.should_generate is False
    assert client.calls == 2
