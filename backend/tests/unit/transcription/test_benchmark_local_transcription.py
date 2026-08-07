import stat

from audio_memory.transcription.benchmark import BenchmarkReport, select_plan


def test_select_plan_switches_to_streaming_when_preprocessing_exceeds_gate() -> None:
    assert select_plan(vad_seconds=9, diarization_seconds=3, whisper_seconds=40) == "plan_a"
    assert select_plan(vad_seconds=9, diarization_seconds=4, whisper_seconds=40) == "plan_b"


def test_benchmark_report_excludes_audio_and_transcript_data() -> None:
    report = BenchmarkReport(
        vad_seconds=1.0,
        diarization_seconds=2.0,
        whisper_seconds=10.0,
        preprocessing_ratio=0.3,
        selected_plan="plan_a",
    ).to_json()

    assert report == {
        "vad_seconds": 1.0,
        "diarization_seconds": 2.0,
        "whisper_seconds": 10.0,
        "preprocessing_ratio": 0.3,
        "selected_plan": "plan_a",
    }


def test_write_report_uses_private_permissions(tmp_path) -> None:
    from audio_memory.transcription.benchmark import write_report

    target = write_report(
        tmp_path,
        BenchmarkReport(1.0, 2.0, 10.0, 0.3, "plan_a"),
    )

    assert target.name == "phase0-benchmark.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert '"audio_path"' not in target.read_text(encoding="utf-8")
