from audio_memory.analysis.events import _safe_validation_shape


def test_safe_validation_shape_excludes_private_input_values() -> None:
    error = ValueError(
        "1 validation error\nmeeting.cards.0.detail.arguments\n"
        "Field required [type=missing, input_value='私密转写内容', input_type=str]"
    )

    diagnostic = _safe_validation_shape(error)

    assert "meeting.cards.0.detail.arguments" in diagnostic
    assert "types=missing" in diagnostic
    assert "私密转写内容" not in diagnostic
