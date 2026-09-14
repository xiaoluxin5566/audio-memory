#!/usr/bin/env python3
"""Opt-in paid evaluation of the Beta 8 report pipeline from a saved transcript."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import httpx

from audio_memory.analysis.beta8_evaluation import (
    parse_merged_transcript,
    require_paid_confirmation,
)
from audio_memory.analysis.beta8_runner import Beta8ReportRunner
from audio_memory.analysis.beta8_search import Beta8SearchExecutor
from audio_memory.analysis.publisher import AnalysisOutcome
from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.beta8_composer import Beta8PromptComposer
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.providers.types import PROVIDER_CONFIGS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(
    "/Users/liujinxin/Documents/音频Always on Demo/outputs/"
    "2026-07-29-four-audio-transcripts/2026-07-29_六段音频_合并逐字稿.md"
)
CONFIRMATION = "I_AUTHORIZE_PAID_BETA8_EVALUATION"


class FixedGenerationSource:
    async def credential_generation(self, provider_id: str) -> int:
        return 1


class ArtifactPublisher:
    def __init__(self, output: Path) -> None:
        self.output = output

    async def publish_beta8(self, version_id, bundle, *, worker_owner_id=None):
        payload = bundle.model_dump(mode="json")
        (self.output / "publication-bundle.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cards_dir = self.output / "cards"
        cards_dir.mkdir(exist_ok=True)
        for card in bundle.cards:
            (cards_dir / f"{card.position + 1:02d}-{card.scene_id}.md").write_text(
                card.markdown, encoding="utf-8"
            )
        return AnalysisOutcome(
            batch_id="evaluation-only",
            card_count=len(bundle.cards),
            todo_count=len(bundle.todo_candidates),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-provider", default="deepseek")
    parser.add_argument("--report-model", default="deepseek-v4-pro")
    parser.add_argument("--search-provider")
    parser.add_argument("--search-model")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm-paid-calls")
    return parser.parse_args()


def input_summary(source: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source": str(source),
        "source_sha256": sha256(source.read_bytes()).hexdigest(),
        "source_bytes": source.stat().st_size,
        "file_count": len({int(row["file_position"]) for row in rows}),
        "segment_count": len(rows),
        "transcript_characters": sum(len(str(row["text"])) for row in rows),
    }


async def seed(
    database: Database,
    rows: list[dict[str, object]],
    *,
    provider_id: str,
    model_id: str,
    search_provider_id: str | None,
    search_model_id: str | None,
    source_hash: str,
) -> tuple[str, str]:
    job_id = str(uuid4())
    version_id = str(uuid4())
    by_position: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        by_position.setdefault(int(row["file_position"]), []).append(row)
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="analyzing"))
        for position, file_rows in sorted(by_position.items()):
            file_id = str(uuid4())
            file_name = str(file_rows[0]["file_name"])
            session.add(
                JobFile(
                    id=file_id,
                    job_id=job_id,
                    original_name=file_name,
                    extension=Path(file_name).suffix.lower(),
                    size_bytes=0,
                    sha256=sha256(f"{source_hash}:{position}".encode()).hexdigest(),
                    duration_ms=max(int(row["end_ms"]) for row in file_rows),
                    recording_started_at=None,
                    recording_time_source="unknown",
                    timezone="Asia/Shanghai",
                    position=position,
                    temporary_path=f"/evaluation/{file_name}",
                )
            )
            for row in file_rows:
                session.add(
                    Transcript(
                        id=str(uuid4()),
                        job_file_id=file_id,
                        segment_index=int(row["segment_index"]),
                        speaker_id="unknown",
                        start_ms=int(row["start_ms"]),
                        end_ms=int(row["end_ms"]),
                        text=str(row["text"]),
                        words_json="[]",
                        risk_classified=True,
                        is_reliable=True,
                    )
                )
        parameters = {
            "pipeline_kind": "beta8_multi_scene_v1",
            "provider_id": provider_id,
            "model_id": model_id,
            "search_provider_id": search_provider_id,
            "search_model_id": search_model_id,
            "credential_generation": 1,
            "fixed_rules_hash": Beta8PromptComposer.fixed_rules_hash(),
            "evaluation_source_sha256": source_hash,
        }
        session.add(
            AnalysisVersion(
                id=version_id,
                source_job_id=job_id,
                provider_id=provider_id,
                model_id=model_id,
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash=Beta8PromptComposer.fixed_rules_hash(),
                staged_results_json="{}",
                pipeline_parameters_json=json.dumps(parameters, ensure_ascii=False),
                status="running",
                worker_owner_id="beta8-real-evaluation",
            )
        )
        await session.commit()
    return job_id, version_id


async def execute(args: argparse.Namespace) -> None:
    source = args.source.expanduser().resolve()
    rows = parse_merged_transcript(source)
    summary = input_summary(source, rows)
    if args.preflight:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    require_paid_confirmation(args.confirm_paid_calls == CONFIRMATION)
    if args.report_provider not in PROVIDER_CONFIGS:
        raise ValueError(f"Unknown report provider: {args.report_provider}")
    if not PROVIDER_CONFIGS[args.report_provider].supports_model(args.report_model):
        raise ValueError(f"Unsupported report model: {args.report_model}")
    if args.search_provider and args.search_provider not in PROVIDER_CONFIGS:
        raise ValueError(f"Unknown search provider: {args.search_provider}")

    output = (args.output or (
        ROOT / "outputs" / "beta8-real-evaluation" /
        f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )).expanduser().resolve()
    if output.exists() and not args.resume:
        raise FileExistsError(f"Output already exists; use --resume: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "input-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    database = Database(output / "evaluation.sqlite3")
    await database.create_schema()
    state_path = output / "evaluation-state.json"
    if args.resume:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["source_sha256"] != summary["source_sha256"]:
            raise ValueError("Resume source hash does not match")
        version_id = state["version_id"]
    else:
        _, version_id = await seed(
            database,
            rows,
            provider_id=args.report_provider,
            model_id=args.report_model,
            search_provider_id=args.search_provider,
            search_model_id=args.search_model,
            source_hash=str(summary["source_sha256"]),
        )
        state_path.write_text(
            json.dumps(
                {"version_id": version_id, "source_sha256": summary["source_sha256"]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    try:
        async with httpx.AsyncClient() as http:
            provider = ProviderAnalysisClient(
                KeychainRepository(MacSecurityClient()), http
            )
            runner = Beta8ReportRunner(
                database=database,
                provider=provider,
                publisher=ArtifactPublisher(output),
                generation_source=FixedGenerationSource(),
                search_executor=Beta8SearchExecutor(
                    provider,
                    provider_id=args.search_provider,
                    model_id=args.search_model,
                ),
            )
            outcome = await runner.run(version_id, "beta8-real-evaluation")
        async with database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        result = {
            **summary,
            "version_id": version_id,
            "report_provider": args.report_provider,
            "report_model": args.report_model,
            "search_provider": args.search_provider,
            "search_model": args.search_model,
            "card_count": outcome.card_count,
            "todo_count": outcome.todo_count,
            "pipeline_metrics": json.loads(version.pipeline_metrics_json or "{}"),
        }
        (output / "evaluation-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(execute(parse_args()))
