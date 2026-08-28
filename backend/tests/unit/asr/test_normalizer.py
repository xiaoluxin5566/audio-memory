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


def test_ignores_provider_tokens_without_timestamps() -> None:
    segments = normalize_volcano_result(
        file_id="file-1",
        duration_ms=3000,
        payload={
            "result": {
                "utterances": [{
                    "start_time": 100,
                    "end_time": 900,
                    "text": "你好。",
                    "words": [
                        {"start_time": 100, "end_time": 300, "text": "你"},
                        {"start_time": -1, "end_time": -1, "text": "。"},
                        {"start_time": 300, "end_time": 600, "text": "好"},
                    ],
                }]
            }
        },
    )

    assert segments[0].words == [
        {"start_ms": 100, "end_ms": 300, "text": "你"},
        {"start_ms": 300, "end_ms": 600, "text": "好"},
    ]


def test_accepts_utterance_within_nearby_provider_audio_duration() -> None:
    segments = normalize_volcano_result(
        file_id="file-aac",
        duration_ms=10_495_560,
        payload={
            "audio_info": {"duration": 10_518_336},
            "result": {
                "utterances": [
                    {
                        "start_time": 10_513_300,
                        "end_time": 10_517_100,
                        "text": "最后一句。",
                    }
                ]
            },
        },
    )

    assert [(item.start_ms, item.end_ms) for item in segments] == [
        (10_513_300, 10_517_100)
    ]


def test_accepts_provider_duration_drift_within_two_percent() -> None:
    segments = normalize_volcano_result(
        file_id="file-aac",
        duration_ms=10_350_000,
        payload={
            "audio_info": {"duration": 10_518_080},
            "result": {
                "utterances": [
                    {
                        "start_time": 10_513_000,
                        "end_time": 10_516_870,
                        "text": "最后一句。",
                    }
                ]
            },
        },
    )

    assert segments[0].end_ms == 10_516_870


def test_rejects_utterance_using_implausibly_long_provider_duration() -> None:
    with pytest.raises(AsrResultError, match="invalid provider audio duration"):
        normalize_volcano_result(
            file_id="file-aac",
            duration_ms=10_495_560,
            payload={
                "audio_info": {"duration": 10_720_000},
                "result": {
                    "utterances": [
                        {
                            "start_time": 10_715_000,
                            "end_time": 10_719_000,
                            "text": "越界内容。",
                        }
                    ]
                },
            },
        )


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
