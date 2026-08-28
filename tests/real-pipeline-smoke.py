#!/usr/bin/env python3
"""Opt-in isolated real pipeline smoke using saved provider credentials."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import audio_memory
from audio_memory import __version__
from audio_memory.config import AppPaths
from audio_memory.main import create_app
from audio_memory.models import AnalysisVersion, JobFile, Transcript


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated, paid end-to-end Audio Memory release smoke."
    )
    parser.add_argument(
        "audio",
        nargs="*",
        type=Path,
        help="Real MP3/AAC files to upload in one batch; synthetic MP3 when omitted.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--target-version")
    parser.add_argument("--main-commit")
    parser.add_argument("--archive-sha256")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def isolated_paths(home: Path) -> AppPaths:
    paths = AppPaths.from_home(home)
    installed_models = AppPaths.from_home(Path.home()).models
    return AppPaths(
        root=paths.root,
        database=paths.database,
        runtime=paths.runtime,
        lock=paths.lock,
        feedback=paths.feedback,
        staging=paths.staging,
        audio=paths.audio,
        models=installed_models,
        prompts=paths.prompts,
    )


def main() -> None:
    args = parse_args()
    requested_audio = [path.expanduser().resolve() for path in args.audio]
    for path in requested_audio:
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.suffix.lower() not in {".mp3", ".aac"}:
            raise ValueError(f"Unsupported release-smoke audio: {path}")

    with tempfile.TemporaryDirectory(prefix="audio-memory-real-smoke-") as temp:
        root = Path(temp)
        audio_files = requested_audio or [create_synthetic_audio(root)]
        app = create_app(
            paths=isolated_paths(root / "home"), frontend_dir=root / "none"
        )
        origin = "http://127.0.0.1:8765"
        with TestClient(app, base_url=origin) as client:
            token = client.get(
                "/api/session", headers={"Origin": origin}
            ).json()["token"]
            mutation_index = 0

            def post(path: str, **kwargs):
                nonlocal mutation_index
                mutation_index += 1
                headers = dict(kwargs.pop("headers", {}))
                headers.update(
                    {
                        "Origin": origin,
                        "X-Audio-Memory-Session": token,
                        "Idempotency-Key": f"real-smoke-{mutation_index}",
                    }
                )
                return client.post(path, headers=headers, **kwargs)

            deadline = time.monotonic() + 30
            deepseek = None
            while time.monotonic() < deadline:
                providers = client.get("/api/providers").json()["providers"]
                deepseek = next(
                    item for item in providers if item["provider_id"] == "deepseek"
                )
                if deepseek["state"] not in {"initializing", "validating"}:
                    break
                time.sleep(0.25)
            assert deepseek and deepseek["state"] == "available", deepseek
            activated = post("/api/providers/deepseek/activate")
            assert activated.status_code == 200, activated.text

            job_id = post("/api/jobs").json()["id"]
            uploaded_names: list[str] = []
            for audio in audio_files:
                mime_type = (
                    "audio/aac" if audio.suffix.lower() == ".aac" else "audio/mpeg"
                )
                with audio.open("rb") as handle:
                    uploaded = post(
                        f"/api/jobs/{job_id}/files",
                        files={"file": (audio.name, handle, mime_type)},
                    )
                assert uploaded.status_code == 201, uploaded.text
                uploaded_names.append(audio.name)

            pipeline_started = time.monotonic()
            pipeline_started_at = datetime.now(timezone.utc).isoformat()
            started = post(f"/api/jobs/{job_id}/start")
            assert started.status_code == 200, started.text

            stages: list[str] = []
            deadline = time.monotonic() + args.timeout_seconds
            while time.monotonic() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()
                if not stages or stages[-1] != job["stage"]:
                    stages.append(job["stage"])
                    print(
                        f"stage={job['stage']} "
                        f"progress={job.get('progress_percent', 0)}"
                    )
                if job["stage"] in {
                    "completed", "failed", "interrupted", "cancelled"
                }:
                    break
                time.sleep(0.5)
            assert job["stage"] == "completed", job

            feed = client.get("/api/feed").json()
            history = client.get("/api/history").json()
            assert feed["days"] or feed["todos"], feed
            cards = [card for day in feed["days"] for card in day["cards"]]
            assert len(cards) == 1, cards
            assert cards[0]["scene_id"] == "analysis", cards[0]
            assert history["days"], history
            elapsed = time.monotonic() - pipeline_started

            async def audit_result():
                async with app.state.database.session() as session:
                    version = await session.scalar(
                        select(AnalysisVersion).where(
                            AnalysisVersion.source_job_id == job_id
                        )
                    )
                    assert version is not None
                    rounds = json.loads(version.search_rounds_json or "[]")
                    sources = json.loads(version.external_sources_json or "[]")
                    file_rows = list(
                        (
                            await session.scalars(
                                select(JobFile).where(JobFile.job_id == job_id)
                            )
                        ).all()
                    )
                    transcript_count = await session.scalar(
                        select(func.count(Transcript.id)).where(
                            Transcript.job_file_id.in_([row.id for row in file_rows])
                        )
                    )
                    return (
                        rounds,
                        sources,
                        version.model_id,
                        sorted(row.original_name for row in file_rows),
                        int(transcript_count or 0),
                        version.status,
                        version.published_card_count,
                    )

            (
                rounds,
                sources,
                model_id,
                stored_names,
                transcript_count,
                version_status,
                published_card_count,
            ) = asyncio.run(audit_result())
            assert model_id == "deepseek-v4-pro", model_id
            assert stored_names == sorted(uploaded_names), stored_names
            assert transcript_count > 0, transcript_count
            assert version_status == "completed", version_status
            assert published_card_count == 1, published_card_count
            searched = bool(sources)
            attempted = bool(rounds)
            errors = [
                error for item in rounds for error in item.get("errors", [])
            ]
            print(
                "real pipeline smoke: ok; "
                f"model={model_id}; search_attempted={attempted}; "
                f"web_search_completed={searched}; search_errors={errors}; "
                f"elapsed_seconds={elapsed:.2f}; stages={stages}; "
                f"files={stored_names}; transcript_segments={transcript_count}; "
                f"published_cards={published_card_count}"
            )
            if args.evidence_output:
                if not (
                    args.target_version
                    and args.main_commit
                    and args.archive_sha256
                ):
                    raise ValueError(
                        "Evidence output requires target version, main commit, "
                        "and archive SHA-256."
                    )
                evidence = {
                    "schema_version": 1,
                    "target_version": args.target_version,
                    "main_commit": args.main_commit,
                    "archive_sha256": args.archive_sha256,
                    "runtime_version": __version__,
                    "module_path": inspect.getfile(audio_memory),
                    "fixtures": [
                        [path.name, sha256_file(path)] for path in audio_files
                    ],
                    "stages": stages,
                    "model_id": model_id,
                    "transcript_segments": transcript_count,
                    "version_status": version_status,
                    "published_card_count": published_card_count,
                    "elapsed_seconds": round(elapsed, 2),
                    "started_at": pipeline_started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                output = args.evidence_output.expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                temporary = output.with_suffix(output.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(output)
                print(f"evidence={output}")


if __name__ == "__main__":
    main()
