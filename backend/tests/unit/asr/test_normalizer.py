from __future__ import annotations

import pytest

from audio_memory.asr.normalizer import AsrResultError, normalize_volcano_result


def test_normalizes_utterances_with_stable_indexes_and_speakers() -> None:
    segments = normalize_volcano_result(
        file_id="file-1",
        duration_ms=3000,
        payload={
            "result": {
                "utterances": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "text": " 你好。 ",
                        "speaker_id": "1",
                        "words": [
                            {"start_time": 0, "end_time": 300, "text": "你"}
                        ],
                    },
                    {
                        "start_time": 1200,
                        "end_time": 2600,
                        "text": "今天开会。",
                        "speaker_id": "2",
                    },
                ]
            }
        },
    )

    assert [item.index for item in segments] == [0, 1]
    assert [item.speaker_id for item in segments] == ["speaker-1", "speaker-2"]
    assert segments[0].text == "你好。"
    assert segments[0].words == [
        {"start_ms": 0, "end_ms": 300, "text": "你"}
    ]


@pytest.mark.parametrize(
    "utterances",
    [
        [{"start_time": 1000, "end_time": 900, "text": "bad"}],
        [{"start_time": 0, "end_time": 4000, "text": "too long"}],
        [
            {"start_time": 1000, "end_time": 2000, "text": "second"},
            {"start_time": 0, "end_time": 900, "text": "first"},
        ],
        [{"start_time": 0, "end_time": 1000, "text": "  "}],
    ],
)
def test_invalid_provider_timeline_is_rejected(utterances) -> None:
    with pytest.raises(AsrResultError):
        normalize_volcano_result(
            file_id="file-1",
            duration_ms=3000,
            payload={"result": {"utterances": utterances}},
        )

