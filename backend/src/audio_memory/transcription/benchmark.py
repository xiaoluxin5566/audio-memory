from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    vad_seconds: float
    diarization_seconds: float
    whisper_seconds: float
    preprocessing_ratio: float
    selected_plan: str

    def to_json(self) -> dict[str, float | str]:
        return asdict(self)


def select_plan(
    *, vad_seconds: float, diarization_seconds: float, whisper_seconds: float
) -> str:
    if whisper_seconds <= 0:
        raise ValueError("Whisper benchmark must be positive")
    preprocessing_ratio = (vad_seconds + diarization_seconds) / whisper_seconds
    return "plan_b" if preprocessing_ratio > 0.30 else "plan_a"


def write_report(runtime_root: Path, report: BenchmarkReport) -> Path:
    diagnostics = runtime_root / "diagnostics"
    diagnostics.mkdir(mode=0o700, parents=True, exist_ok=True)
    diagnostics.chmod(0o700)
    target = diagnostics / "phase0-benchmark.json"
    target.write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    target.chmod(0o600)
    return target
