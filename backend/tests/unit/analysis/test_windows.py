from __future__ import annotations

import pytest
from types import SimpleNamespace

from audio_memory.analysis.windows import (
    AnalysisWindowError,
    AnalysisQualityError,
    build_analysis_windows,
    complete_window_event_map,
    merge_window_event_maps,
    validate_analysis_quality,
)
from audio_memory.prompts.event_schema import EventMap


def segment(
    segment_id: str,
    file_id: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "file_id": file_id,
        "file_name": f"{file_id}.mp3",
        "recording_started_at": None,
        "local_date": None,
        "timezone": None,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker_id": "unknown",
        "text": f"synthetic {segment_id}",
        "reliability_weight": 1.0,
    }


def local_event_map(
    *,
    evidence_ids: list[str],
    event_id: str = "event_001",
    start_ms: int = 0,
    end_ms: int = 1_000,
    parent_event_id: str | None = None,
    user_speaker_id: str | None = None,
    user_confidence: float = 0,
    user_evidence_ids: list[str] | None = None,
) -> EventMap:
    return EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": user_speaker_id,
                "confidence": user_confidence,
                "reasoning": "synthetic identity evidence",
                "evidence_segment_ids": user_evidence_ids or [],
            },
            "events": [
                {
                    "event_id": event_id,
                    "parent_event_id": parent_event_id,
                    "event_type": "discussion",
                    "title": "Synthetic discussion",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker_ids": ["unknown"],
                    "user_role": None,
                    "user_role_confidence": 0,
                    "factual_summary": "A synthetic discussion occurred.",
                    "topics": ["synthetic"],
                    "candidate_scenes": ["meeting"],
                    "evidence_segment_ids": evidence_ids,
                    "boundary_confidence": 0.9,
                    "local_date": None,
                    "timezone": None,
                }
            ],
            "unassigned_segment_ids": [],
        }
    )


def test_build_analysis_windows_splits_on_file_and_frozen_gap_boundaries() -> None:
    transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 45_999, 46_999),
        segment("seg_0_2", "file-a", 91_999, 92_999),
        segment("seg_1_0", "file-b", 0, 1_000),
    ]

    windows = build_analysis_windows(transcript)

    assert [
        [str(item["segment_id"]) for item in window.segments]
        for window in windows
    ] == [["seg_0_0", "seg_0_1"], ["seg_0_2"], ["seg_1_0"]]
    assert [window.window_id for window in windows] == [
        "window_0000",
        "window_0001",
        "window_0002",
    ]


def test_build_analysis_windows_splits_before_span_limit_is_exceeded() -> None:
    transcript = [
        segment(
            f"seg_0_{index}",
            "file-a",
            index * 40_000,
            index * 40_000 + (1 if index == 30 else 1_000),
        )
        for index in range(31)
    ]

    windows = build_analysis_windows(transcript)

    assert [len(window.segments) for window in windows] == [30, 1]
    assert [(window.start_ms, window.end_ms) for window in windows] == [
        (0, 1_161_000),
        (1_200_000, 1_200_001),
    ]


def test_build_analysis_windows_splits_before_401st_segment() -> None:
    transcript = [
        segment(f"seg_0_{index}", "file-a", index * 2_000, index * 2_000 + 1_000)
        for index in range(401)
    ]

    windows = build_analysis_windows(transcript)

    assert [len(window.segments) for window in windows] == [400, 1]
    assert windows[1].segments[0]["segment_id"] == "seg_0_400"


def test_build_analysis_windows_stably_sorts_and_covers_every_segment_once() -> None:
    transcript = [
        segment("seg_b_1", "file-b", 2_000, 3_000),
        segment("seg_a_1", "file-a", 2_000, 3_000),
        segment("seg_b_0", "file-b", 0, 1_000),
        segment("seg_a_0", "file-a", 0, 1_000),
    ]

    windows = build_analysis_windows(transcript)

    flattened = [
        str(item["segment_id"])
        for window in windows
        for item in window.segments
    ]
    assert flattened == ["seg_b_0", "seg_b_1", "seg_a_0", "seg_a_1"]
    assert len(flattened) == len(set(flattened)) == len(transcript)
    assert [(window.file_id, window.start_ms, window.end_ms) for window in windows] == [
        ("file-b", 0, 3_000),
        ("file-a", 0, 3_000),
    ]


@pytest.mark.parametrize(
    "transcript",
    [
        [
            segment("seg_duplicate", "file-a", 0, 1_000),
            segment("seg_duplicate", "file-a", 2_000, 3_000),
        ],
        [segment("seg_invalid", "file-a", 1_000, 1_000)],
        [segment("seg_negative", "file-a", -1, 1_000)],
    ],
)
def test_build_analysis_windows_rejects_invalid_segment_structure(
    transcript: list[dict[str, object]],
) -> None:
    with pytest.raises(AnalysisWindowError):
        build_analysis_windows(transcript)


def test_complete_window_event_map_fills_server_owned_unassigned_ids() -> None:
    window = build_analysis_windows(
        [
            segment("seg_0_0", "file-a", 0, 1_000),
            segment("seg_0_1", "file-a", 2_000, 3_000),
        ]
    )[0]

    completed = complete_window_event_map(
        window,
        local_event_map(evidence_ids=["seg_0_0"]),
    )

    assert completed.events[0].event_id == "event_w0000_001"
    assert completed.unassigned_segment_ids == ["seg_0_1"]


@pytest.mark.parametrize(
    ("event_map", "message"),
    [
        (local_event_map(evidence_ids=["seg_missing"]), "unknown evidence"),
        (
            local_event_map(
                evidence_ids=["seg_0_0"],
                start_ms=100,
                end_ms=900,
            ),
            "contain its evidence",
        ),
    ],
)
def test_complete_window_event_map_rejects_invalid_local_evidence(
    event_map: EventMap,
    message: str,
) -> None:
    window = build_analysis_windows(
        [segment("seg_0_0", "file-a", 0, 1_000)]
    )[0]

    with pytest.raises(AnalysisWindowError, match=message) as captured:
        complete_window_event_map(window, event_map)

    assert "seg_missing" not in str(captured.value)
    assert "seg_0_0" not in str(captured.value)


def test_merge_window_event_maps_namespaces_duplicate_model_event_ids() -> None:
    windows = build_analysis_windows(
        [
            segment("seg_0_0", "file-a", 0, 1_000),
            segment("seg_0_1", "file-a", 50_000, 51_000),
        ]
    )
    completed = [
        complete_window_event_map(
            windows[0],
            local_event_map(evidence_ids=["seg_0_0"]),
        ),
        complete_window_event_map(
            windows[1],
            local_event_map(
                evidence_ids=["seg_0_1"],
                start_ms=50_000,
                end_ms=51_000,
            ),
        ),
    ]

    merged = merge_window_event_maps(windows, completed)

    assert [event.event_id for event in merged.events] == [
        "event_w0000_001",
        "event_w0001_001",
    ]
    assert merged.unassigned_segment_ids == []
    assert {
        segment_id
        for event in merged.events
        for segment_id in event.evidence_segment_ids
    } == {"seg_0_0", "seg_0_1"}


def test_complete_window_event_map_rewrites_parent_reference_in_namespace() -> None:
    window = build_analysis_windows(
        [
            segment("seg_0_0", "file-a", 0, 1_000),
            segment("seg_0_1", "file-a", 2_000, 3_000),
        ]
    )[0]
    payload = local_event_map(
        evidence_ids=["seg_0_0"],
        start_ms=0,
        end_ms=3_000,
    ).model_dump(mode="python")
    child = payload["events"][0].copy()
    child.update(
        {
            "event_id": "event_002",
            "parent_event_id": "event_001",
            "start_ms": 2_000,
            "end_ms": 3_000,
            "evidence_segment_ids": ["seg_0_1"],
        }
    )
    payload["events"].append(child)

    completed = complete_window_event_map(window, EventMap.model_validate(payload))

    assert completed.events[1].event_id == "event_w0000_002"
    assert completed.events[1].parent_event_id == "event_w0000_001"


def test_merge_window_event_maps_requires_two_window_identity_consensus() -> None:
    windows = build_analysis_windows(
        [
            segment("seg_0_0", "file-a", 0, 1_000),
            segment("seg_0_1", "file-a", 50_000, 51_000),
        ]
    )
    first_only = [
        complete_window_event_map(
            windows[0],
            local_event_map(
                evidence_ids=["seg_0_0"],
                user_speaker_id="speaker_A",
                user_confidence=0.90,
                user_evidence_ids=["seg_0_0"],
            ),
        ),
        complete_window_event_map(
            windows[1],
            local_event_map(
                evidence_ids=["seg_0_1"],
                start_ms=50_000,
                end_ms=51_000,
            ),
        ),
    ]

    assert merge_window_event_maps(windows, first_only).user_speaker.speaker_id is None

    consensus = [
        first_only[0],
        complete_window_event_map(
            windows[1],
            local_event_map(
                evidence_ids=["seg_0_1"],
                start_ms=50_000,
                end_ms=51_000,
                user_speaker_id="speaker_A",
                user_confidence=0.88,
                user_evidence_ids=["seg_0_1"],
            ),
        ),
    ]
    merged = merge_window_event_maps(windows, consensus)

    assert merged.user_speaker.speaker_id == "speaker_A"
    assert merged.user_speaker.confidence == 0.88
    assert merged.user_speaker.evidence_segment_ids == ["seg_0_0", "seg_0_1"]


def test_merge_window_event_maps_rejects_conflicting_or_subthreshold_identity() -> None:
    windows = build_analysis_windows(
        [
            segment("seg_0_0", "file-a", 0, 1_000),
            segment("seg_0_1", "file-a", 50_000, 51_000),
        ]
    )
    maps = [
        complete_window_event_map(
            windows[0],
            local_event_map(
                evidence_ids=["seg_0_0"],
                user_speaker_id="speaker_A",
                user_confidence=0.84,
                user_evidence_ids=["seg_0_0"],
            ),
        ),
        complete_window_event_map(
            windows[1],
            local_event_map(
                evidence_ids=["seg_0_1"],
                start_ms=50_000,
                end_ms=51_000,
                user_speaker_id="speaker_B",
                user_confidence=0.95,
                user_evidence_ids=["seg_0_1"],
            ),
        ),
    ]

    merged = merge_window_event_maps(windows, maps)

    assert merged.user_speaker.speaker_id is None
    assert merged.user_speaker.confidence == 0
    assert merged.user_speaker.evidence_segment_ids == []


def scene_results(*, generated: bool = False) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            scene_id=scene_id,
            should_generate=generated and scene_id == "meeting",
            cards=[object()] if generated and scene_id == "meeting" else [],
            todos=[],
        )
        for scene_id in (
            "todo",
            "meeting",
            "parenting",
            "content",
            "growth",
            "inspiration",
        )
    ]


def empty_event_map(segment_ids: list[str]) -> EventMap:
    return EventMap.model_validate(
        {
            "user_speaker": {
                "speaker_id": None,
                "confidence": 0,
                "reasoning": "synthetic unknown identity",
                "evidence_segment_ids": [],
            },
            "events": [],
            "unassigned_segment_ids": segment_ids,
        }
    )


def test_quality_gate_rejects_one_event_for_two_hour_transcript() -> None:
    transcript = [segment("seg_0_0", "file-a", 0, 7_200_000)]
    event_map = local_event_map(
        evidence_ids=["seg_0_0"],
        start_ms=0,
        end_ms=7_200_000,
    )

    with pytest.raises(AnalysisQualityError) as captured:
        validate_analysis_quality(transcript, event_map, scene_results())

    assert captured.value.reason == "long_audio_undersegmented"


def test_quality_gate_rejects_empty_scenes_with_valuable_events() -> None:
    transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 2_000, 3_000),
    ]
    payload = local_event_map(evidence_ids=["seg_0_0"]).model_dump(mode="python")
    second = payload["events"][0].copy()
    second.update(
        {
            "event_id": "event_002",
            "event_type": "interview",
            "start_ms": 2_000,
            "end_ms": 3_000,
            "evidence_segment_ids": ["seg_0_1"],
        }
    )
    payload["events"].append(second)
    event_map = EventMap.model_validate(payload)

    with pytest.raises(AnalysisQualityError) as captured:
        validate_analysis_quality(transcript, event_map, scene_results())

    assert captured.value.reason == "valuable_events_all_empty"


def test_quality_gate_rejects_large_reliable_text_with_all_empty_scenes() -> None:
    transcript = [segment("seg_0_0", "file-a", 0, 1_000)]
    transcript[0]["text"] = "x" * 10_000

    with pytest.raises(AnalysisQualityError) as captured:
        validate_analysis_quality(
            transcript,
            empty_event_map(["seg_0_0"]),
            scene_results(),
        )

    assert captured.value.reason == "large_transcript_all_empty"


def test_quality_gate_allows_short_empty_and_visible_multi_event_results() -> None:
    short = [segment("seg_0_0", "file-a", 0, 1_000)]
    validate_analysis_quality(
        short,
        empty_event_map(["seg_0_0"]),
        scene_results(),
    )

    long_transcript = [
        segment("seg_0_0", "file-a", 0, 1_000),
        segment("seg_0_1", "file-a", 7_199_000, 7_200_000),
    ]
    payload = local_event_map(evidence_ids=["seg_0_0"]).model_dump(mode="python")
    second = payload["events"][0].copy()
    second.update(
        {
            "event_id": "event_002",
            "start_ms": 7_199_000,
            "end_ms": 7_200_000,
            "evidence_segment_ids": ["seg_0_1"],
        }
    )
    payload["events"].append(second)
    validate_analysis_quality(
        long_transcript,
        EventMap.model_validate(payload),
        scene_results(generated=True),
    )
