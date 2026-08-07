from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "evaluate-transcription-risk-gate.py"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _run_evaluator(input_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(input_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_evaluator_reports_hand_checked_confusion_matrix_without_segment_data(
    tmp_path: Path,
) -> None:
    # Predicting either condition alone, or leaking a segment identifier into the
    # report, would turn this exact, hand-checked 3/1/2/4 matrix into bad output.
    input_path = tmp_path / "labeled-features.jsonl"
    _write_jsonl(
        input_path,
        [
            {"segment_id": "anon-01", "expected_risk": "risk", "features": {"similarity": 0.90, "characters_per_second": 15}},
            {"segment_id": "anon-02", "expected_risk": "risk", "features": {"similarity": 0.91, "characters_per_second": 16}},
            {"segment_id": "anon-03", "expected_risk": "risk", "features": {"similarity": 0.95, "characters_per_second": 14}},
            {"segment_id": "anon-04", "expected_risk": "risk", "features": {"similarity": 0.89, "characters_per_second": 16}},
            {"segment_id": "anon-05", "expected_risk": "risk", "features": {"similarity": 0.95, "characters_per_second": 13}},
            {"segment_id": "anon-06", "expected_risk": "normal", "features": {"similarity": 0.95, "characters_per_second": 16}},
            {"segment_id": "anon-07", "expected_risk": "normal", "features": {"similarity": 0.84, "characters_per_second": 16}},
            {"segment_id": "anon-08", "expected_risk": "normal", "features": {"similarity": 0.95, "characters_per_second": 12}},
            {"segment_id": "anon-09", "expected_risk": "normal", "features": {"similarity": 0.80, "characters_per_second": 11}},
            {"segment_id": "anon-10", "expected_risk": "normal", "features": {"similarity": 0.89, "characters_per_second": 13}},
        ],
    )

    completed = _run_evaluator(input_path)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    baseline = next(
        item
        for item in report["thresholds"]
        if item["similarity_threshold"] == 0.90
        and item["characters_per_second_threshold"] == 14
    )
    assert baseline["confusion_matrix"] == {"tp": 3, "fp": 1, "fn": 2, "tn": 4}
    assert baseline["precision"] == 0.75
    assert baseline["recall"] == 0.60
    assert baseline["fpr"] == 0.20
    assert baseline["tpr"] == 0.60
    assert baseline["eligible_for_candidate"] is False
    assert report["candidate_evidence_is_stable"] is False
    assert len(report["thresholds"]) == 9
    assert "anon-01" not in completed.stdout


@pytest.mark.parametrize("forbidden_field", ["text", "audio_path"])
def test_evaluator_rejects_content_or_audio_path_fields(
    tmp_path: Path, forbidden_field: str
) -> None:
    # Accepting either field lets untrusted content bypass the offline evaluator's
    # de-identified-data boundary.
    secret = "must-not-appear-outside-input"
    input_path = tmp_path / "invalid-features.jsonl"
    record: dict[str, object] = {
        "segment_id": "anon-01",
        "expected_risk": "risk",
        "features": {"similarity": 0.95, "characters_per_second": 16},
        forbidden_field: secret,
    }
    _write_jsonl(input_path, [record])

    completed = _run_evaluator(input_path)

    assert completed.returncode != 0
    assert "input schema violation" in completed.stderr
    assert secret not in completed.stdout
    assert secret not in completed.stderr


@pytest.mark.parametrize("feature", ["similarity", "characters_per_second"])
@pytest.mark.parametrize("non_finite_value", [float("nan"), float("inf"), float("-inf")])
def test_evaluator_rejects_non_finite_feature_values_without_echoing_input(
    tmp_path: Path, feature: str, non_finite_value: float
) -> None:
    # Removing finite-number validation would let NaN or infinity distort the
    # aggregate metrics while the rejected anonymous identifier must stay private.
    secret = "must-not-appear-in-validation-error"
    input_path = tmp_path / "non-finite-features.jsonl"
    features: dict[str, object] = {
        "similarity": 0.95,
        "characters_per_second": 16,
    }
    features[feature] = non_finite_value
    _write_jsonl(
        input_path,
        [
            {
                "segment_id": secret,
                "expected_risk": "risk",
                "features": features,
            }
        ],
    )

    completed = _run_evaluator(input_path)

    assert completed.returncode != 0
    assert "input schema violation" in completed.stderr
    assert secret not in completed.stdout
    assert secret not in completed.stderr
