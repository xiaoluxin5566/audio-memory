from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProfileDelta:
    subject_id: str
    dimension: str
    value: dict[str, object]
    confidence: float
    explicit: bool


def validate_profile_delta(raw: list[dict[str, object]]) -> list[ProfileDelta]:
    accepted: list[ProfileDelta] = []
    for item in raw:
        if item.get("subject_id") != "user":
            continue
        dimension = item.get("dimension")
        value = item.get("value")
        confidence = item.get("confidence")
        if not isinstance(dimension, str) or not isinstance(value, dict):
            continue
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            continue
        accepted.append(
            ProfileDelta(
                subject_id="user",
                dimension=dimension,
                value=value,
                confidence=float(confidence),
                explicit=bool(item.get("explicit", False)),
            )
        )
    return accepted

