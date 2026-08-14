from audio_memory.analysis.full_transcript import build_full_transcript_markdown


def test_full_transcript_preserves_file_boundaries_speaker_and_evidence_ids() -> None:
    markdown = build_full_transcript_markdown(
        [
            {
                "segment_id": "seg_0_0",
                "file_id": "file-a",
                "file_name": "上午.mp3",
                "recording_started_at": "2026-07-29T08:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "start_ms": 1_000,
                "end_ms": 3_500,
                "speaker_id": "speaker_1",
                "text": "先确认今天的交付。",
            },
            {
                "segment_id": "seg_1_0",
                "file_id": "file-b",
                "file_name": "晚上.aac",
                "recording_started_at": None,
                "timezone": None,
                "start_ms": 61_000,
                "end_ms": 62_000,
                "speaker_id": "unknown",
                "text": "我们开始读题。",
            },
        ]
    )

    assert "## 文件 1：上午.mp3" in markdown
    assert "## 文件 2：晚上.aac" in markdown
    assert "seg_0_0｜00:00:01–00:00:03｜speaker_1" in markdown
    assert "seg_1_0｜00:01:01–00:01:02｜unknown" in markdown
    assert "说话人标签只用于区分声音" in markdown


def test_full_transcript_rejects_empty_reliable_input() -> None:
    try:
        build_full_transcript_markdown([])
    except ValueError as exc:
        assert "completed transcript" in str(exc)
    else:
        raise AssertionError("empty transcript must be rejected")
