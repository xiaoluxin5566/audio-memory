#!/usr/bin/env python3
"""Evaluate risk-gate thresholds from de-identified, labeled JSONL only.

This script is intentionally offline: it neither invokes transcription models nor
imports the production risk gate, and it never writes production configuration.
Its JSON report contains aggregate counts and metrics only.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


SIMILARITY_THRESHOLDS = (0.85, 0.90, 0.95)
CHARACTERS_PER_SECOND_THRESHOLDS = (12, 14, 16)
MIN_STABLE_SAMPLES_PER_LABEL = 10
_ANONYMOUS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ALLOWED_RECORD_FIELDS = frozenset({"segment_id", "expected_risk", "features"})
_ALLOWED_FEATURE_FIELDS = frozenset({"similarity", "characters_per_second"})


class InputSchemaError(ValueError):
    """A generic error that deliberately avoids echoing untrusted input."""


@dataclass(frozen=True, slots=True)
class LabeledFeatures:
    expected_risk: bool
    similarity: float
    characters_per_second: float


@dataclass(frozen=True, slots=True)
class ConfusionMatrix:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        denominator = self.tp + self.fp
        return None if denominator == 0 else self.tp / denominator

    @property
    def recall(self) -> float | None:
        denominator = self.tp + self.fn
        return None if denominator == 0 else self.tp / denominator

    @property
    def fpr(self) -> float | None:
        denominator = self.fp + self.tn
        return None if denominator == 0 else self.fp / denominator

    @property
    def tpr(self) -> float | None:
        return self.recall


def _schema_error() -> InputSchemaError:
    return InputSchemaError("input schema violation")


def _load_record(value: object) -> LabeledFeatures:
    if not isinstance(value, Mapping) or set(value) != _ALLOWED_RECORD_FIELDS:
        raise _schema_error()

    segment_id = value.get("segment_id")
    expected_risk = value.get("expected_risk")
    features = value.get("features")
    if (
        not isinstance(segment_id, str)
        or _ANONYMOUS_ID.fullmatch(segment_id) is None
        or expected_risk not in {"risk", "normal"}
        or not isinstance(features, Mapping)
        or set(features) != _ALLOWED_FEATURE_FIELDS
    ):
        raise _schema_error()

    similarity = features.get("similarity")
    characters_per_second = features.get("characters_per_second")
    if (
        isinstance(similarity, bool)
        or not isinstance(similarity, (int, float))
        or not 0.0 <= float(similarity) <= 1.0
        or isinstance(characters_per_second, bool)
        or not isinstance(characters_per_second, (int, float))
        or float(characters_per_second) < 0.0
    ):
        raise _schema_error()

    return LabeledFeatures(
        expected_risk=expected_risk == "risk",
        similarity=float(similarity),
        characters_per_second=float(characters_per_second),
    )


def load_labeled_features(path: Path) -> list[LabeledFeatures]:
    """Load one strictly de-identified record per non-empty JSONL line."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise _schema_error() from error

    records: list[LabeledFeatures] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(_load_record(json.loads(line)))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _schema_error() from error
    if not records:
        raise _schema_error()
    return records


def _confusion_matrix(
    records: Iterable[LabeledFeatures],
    similarity_threshold: float,
    characters_per_second_threshold: int,
) -> ConfusionMatrix:
    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for record in records:
        predicted_risk = (
            record.similarity >= similarity_threshold
            and record.characters_per_second >= characters_per_second_threshold
        )
        if record.expected_risk and predicted_risk:
            counts["tp"] += 1
        elif not record.expected_risk and predicted_risk:
            counts["fp"] += 1
        elif record.expected_risk:
            counts["fn"] += 1
        else:
            counts["tn"] += 1
    return ConfusionMatrix(**counts)


def _metric(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def build_report(records: list[LabeledFeatures]) -> dict[str, object]:
    """Return aggregate threshold evidence without any identifier or content."""
    positives = sum(record.expected_risk for record in records)
    negatives = len(records) - positives
    stable = (
        positives >= MIN_STABLE_SAMPLES_PER_LABEL
        and negatives >= MIN_STABLE_SAMPLES_PER_LABEL
    )
    thresholds: list[dict[str, object]] = []
    for similarity_threshold in SIMILARITY_THRESHOLDS:
        for characters_per_second_threshold in CHARACTERS_PER_SECOND_THRESHOLDS:
            matrix = _confusion_matrix(
                records, similarity_threshold, characters_per_second_threshold
            )
            thresholds.append(
                {
                    "similarity_threshold": similarity_threshold,
                    "characters_per_second_threshold": characters_per_second_threshold,
                    "confusion_matrix": asdict(matrix),
                    "precision": _metric(matrix.precision),
                    "recall": _metric(matrix.recall),
                    "fpr": _metric(matrix.fpr),
                    "tpr": _metric(matrix.tpr),
                    "eligible_for_candidate": stable,
                }
            )
    return {
        "schema": "transcription-risk-gate-evaluation/v1",
        "sample_counts": {"risk": positives, "normal": negatives},
        "minimum_stable_samples_per_label": MIN_STABLE_SAMPLES_PER_LABEL,
        "candidate_evidence_is_stable": stable,
        "thresholds": thresholds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate de-identified transcription risk-gate features."
    )
    parser.add_argument("input", type=Path, help="de-identified labeled JSONL")
    args = parser.parse_args(argv)
    try:
        report = build_report(load_labeled_features(args.input))
    except InputSchemaError:
        print("input schema violation", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
