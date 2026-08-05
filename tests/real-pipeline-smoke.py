#!/usr/bin/env python3
"""Opt-in real local pipeline smoke using synthetic audio and the saved DeepSeek key."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from audio_memory.config import AppPaths
from audio_memory.main import create_app


def create_synthetic_audio(root: Path) -> Path:
    spoken = root / "synthetic.aiff"
    audio = root / "synthetic.mp3"
    subprocess.run(
        [
            "say", "-v", "Tingting",
            "这是一次产品测试会议。我们决定明天下午三点完成演示，负责人是小李。请记录这个待办。",
            "-o", str(spoken),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-i", str(spoken),
            "-c:a", "libmp3lame", "-y", str(audio),
        ],
        check=True,
    )
    return audio


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="audio-memory-real-smoke-") as temp:
        root = Path(temp)
        audio = create_synthetic_audio(root)
        app = create_app(paths=AppPaths.from_home(root / "home"), frontend_dir=root / "none")
        with TestClient(app) as client:
            deadline = time.monotonic() + 30
            deepseek = None
            while time.monotonic() < deadline:
                providers = client.get("/api/providers").json()["providers"]
                deepseek = next(item for item in providers if item["provider_id"] == "deepseek")
                if deepseek["state"] not in {"initializing", "validating"}:
                    break
                time.sleep(0.25)
            assert deepseek and deepseek["state"] == "available", deepseek
            activated = client.post("/api/providers/deepseek/activate")
            assert activated.status_code == 200, activated.text

            job_id = client.post("/api/jobs").json()["id"]
            with audio.open("rb") as handle:
                uploaded = client.post(
                    f"/api/jobs/{job_id}/files",
                    files={"file": (audio.name, handle, "audio/mpeg")},
                )
            assert uploaded.status_code == 201, uploaded.text
            started = client.post(f"/api/jobs/{job_id}/start")
            assert started.status_code == 200, started.text

            stages: list[str] = []
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()
                if not stages or stages[-1] != job["stage"]:
                    stages.append(job["stage"])
                    print(f"stage={job['stage']} progress={job.get('progress_percent', 0)}")
                if job["stage"] in {"completed", "failed", "interrupted", "cancelled"}:
                    break
                time.sleep(0.5)
            assert job["stage"] == "completed", job
            feed = client.get("/api/feed").json()
            history = client.get("/api/history").json()
            assert feed["days"] or feed["todos"], feed
            assert history["days"], history
            print(f"real pipeline smoke: ok; stages={stages}")


if __name__ == "__main__":
    main()
