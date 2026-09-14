#!/usr/bin/env python3
"""Validate or explicitly run the hash-bound Beta 8 indexed-V1 acceptance."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from audio_memory.analysis.beta8_evaluation import (
    EvaluationPaths,
    load_json_object,
    mark_paid_execution,
    parse_merged_transcript,
    prepare_dry_run,
    require_paid_confirmation,
    evaluate_event_index_acceptance,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "backend" / "tests" / "fixtures" / "beta8"
DEFAULT_MANIFEST = FIXTURE_ROOT / "doubao-long-audio-2-manifest.json"
DEFAULT_GROUND_TRUTH = FIXTURE_ROOT / "doubao-long-audio-2-ground-truth.json"
DEFAULT_TRANSCRIPT = Path(
    "/Users/liujinxin/Documents/音频Always on Demo/outputs/"
    "cloud-asr-july31/volcano/正式报告链路输入逐字稿.md"
)
DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "beta8-indexed-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run-paid", action="store_true")
    parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--transcript", type=Path, default=DEFAULT_TRANSCRIPT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--resume-output",
        type=Path,
        help="Resume one hash-bound paid run that already has an event-index checkpoint",
    )
    parser.add_argument("--report-provider", default="deepseek")
    parser.add_argument("--report-model", default="deepseek-v4-pro")
    parser.add_argument("--search-provider", default="kimi")
    parser.add_argument("--search-model", default="kimi-k2.6")
    parser.add_argument(
        "--validation-tier",
        choices=("final", "mechanical-smoke"),
        default="final",
        help="Final quality acceptance or lower-cost full-chain mechanical smoke",
    )
    checkpoint = parser.add_mutually_exclusive_group()
    checkpoint.add_argument(
        "--stop-after-index",
        action="store_true",
        help="Persist the event-index checkpoint, then pause before unified V1",
    )
    checkpoint.add_argument(
        "--stop-after-v1",
        action="store_true",
        help="Persist the unified V1 checkpoint, then pause before audits and search",
    )
    return parser.parse_args()


def evaluation_paths(args: argparse.Namespace) -> EvaluationPaths:
    return EvaluationPaths(
        transcript=args.transcript.expanduser().resolve(),
        manifest=args.manifest.expanduser().resolve(),
        ground_truth=args.ground_truth.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
    )


def save_index_acceptance(index, ground_truth, output: Path):
    report = evaluate_event_index_acceptance(index, ground_truth)
    ResponseQuarantine._write_private(
        output / f"index-acceptance-{uuid4()}.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    if report["blockers"]:
        raise ValueError("; ".join(report["blockers"]))
    return report


class FixedGenerationSource:
    async def credential_generation(self, _provider_id: str) -> int:
        return 1


class ResponseQuarantine:
    def __init__(self, output: Path) -> None:
        self.directory = output / "quarantine"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    def capture(self, stage: str, raw: str) -> dict[str, str]:
        capture_id = str(uuid4())
        response_path = self.directory / f"{stage}-{capture_id}.provider-response.json"
        metadata_path = self.directory / f"{stage}-{capture_id}.metadata.json"
        encoded = raw.encode("utf-8")
        metadata = {
            "capture_id": capture_id,
            "stage": stage,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "validation_status": "unvalidated",
            "response_sha256": sha256(encoded).hexdigest(),
            "response_bytes": len(encoded),
        }
        self._write_private(response_path, raw)
        self._write_private(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        return {
            "response_path": str(response_path),
            "metadata_path": str(metadata_path),
        }


class ArtifactPublisher:
    def __init__(self, output: Path) -> None:
        self.output = output

    async def publish_beta8(
        self, _version_id, bundle, *, worker_owner_id=None
    ):
        from audio_memory.analysis.publisher import AnalysisOutcome

        payload = bundle.model_dump(mode="json")
        (self.output / "publication-bundle.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cards_dir = self.output / "cards"
        cards_dir.mkdir()
        for card in bundle.cards:
            (cards_dir / f"{card.position + 1:02d}-{card.scene_id}.md").write_text(
                card.markdown, encoding="utf-8"
            )
        return AnalysisOutcome(
            batch_id="evaluation-only",
            card_count=len(bundle.cards),
            todo_count=len(bundle.todo_candidates),
        )


def validate_execution_binding(args: argparse.Namespace) -> dict[str, object]:
    validation_tier = getattr(args, "validation_tier", "final")
    expected_report_model = {
        "final": "deepseek-v4-pro",
        "mechanical-smoke": "deepseek-v4-flash",
    }.get(validation_tier)
    if expected_report_model is None:
        raise ValueError(f"Unknown validation tier: {validation_tier}")
    if (args.report_provider, args.report_model) != (
        "deepseek",
        expected_report_model,
    ):
        if validation_tier == "final":
            raise ValueError(
                "Final acceptance is bound to deepseek/deepseek-v4-pro"
            )
        raise ValueError(
            "Mechanical smoke is bound to deepseek/deepseek-v4-flash"
        )
    if (args.search_provider, args.search_model) != ("kimi", "kimi-k2.6"):
        raise ValueError("This evaluation is bound to kimi/kimi-k2.6 search")
    return {
        "validation_tier": validation_tier,
        "qualifies_as_final_acceptance": validation_tier == "final",
        "report_provider": args.report_provider,
        "report_model": args.report_model,
        "search_provider": args.search_provider,
        "search_model": args.search_model,
    }


async def seed_paid_run(
    database,
    rows: list[dict[str, object]],
    *,
    report_provider: str = "deepseek",
    report_model: str = "deepseek-v4-pro",
    search_provider: str = "kimi",
    search_model: str = "kimi-k2.6",
) -> str:
    from audio_memory.analysis.pipeline_identity import (
        BETA8_INDEXED_PIPELINE_KIND,
        build_pipeline_parameters,
    )
    from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript

    job_id = str(uuid4())
    version_id = str(uuid4())
    _, parameters_json, parameters_fingerprint = build_pipeline_parameters(
        pipeline_kind=BETA8_INDEXED_PIPELINE_KIND,
        provider_id=report_provider,
        model_id=report_model,
        credential_generation=1,
        search_provider_id=search_provider,
        search_model_id=search_model,
    )
    by_position: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        by_position.setdefault(int(row["file_position"]), []).append(row)
    async with database.session() as session:
        session.add(AnalysisJob(id=job_id, stage="analyzing"))
        for position, file_rows in sorted(by_position.items()):
            file_id = str(uuid4())
            file_name = str(file_rows[0]["file_name"])
            session.add(JobFile(
                id=file_id,
                job_id=job_id,
                original_name=file_name,
                extension=Path(file_name).suffix.lower(),
                size_bytes=0,
                sha256=sha256(f"beta8-acceptance:{position}".encode()).hexdigest(),
                duration_ms=max(int(row["end_ms"]) for row in file_rows),
                recording_started_at=None,
                recording_time_source="unknown",
                timezone="Asia/Shanghai",
                position=position,
                temporary_path=f"/evaluation/{file_name}",
            ))
            for row in file_rows:
                session.add(Transcript(
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
                ))
        session.add(AnalysisVersion(
            id=version_id,
            source_job_id=job_id,
            provider_id=report_provider,
            model_id=report_model,
            credential_generation=1,
            prompt_snapshot_json="{}",
            profile_snapshot_json="[]",
            fixed_rules_hash=json.loads(parameters_json)["fixed_rules_hash"],
            staged_results_json="{}",
            pipeline_parameters_json=parameters_json,
            pipeline_parameters_fingerprint=parameters_fingerprint,
            status="running",
            worker_owner_id="beta8-indexed-v1-acceptance",
        ))
        await session.commit()
    return version_id


async def record_failed_paid_run(
    database,
    *,
    output: Path,
    plan: dict[str, object],
    version_id: str,
    error: Exception,
) -> dict[str, object]:
    from audio_memory.models import AnalysisJob, AnalysisVersion

    error_code = str(getattr(error, "code", None) or "model_analysis_failed")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
        if version is None:
            raise ValueError("Paid evaluation version disappeared")
        job = await session.get(AnalysisJob, version.source_job_id)
        version.status = "failed"
        version.error_code = error_code
        version.worker_owner_id = None
        version.lease_expires_at = None
        version.completed_at = datetime.now(timezone.utc).isoformat()
        if job is not None:
            job.stage = "failed"
            job.error_code = error_code
        metrics = json.loads(version.pipeline_metrics_json or "{}")
        await session.commit()
    result = {
        **mark_paid_execution(plan),
        "status": "failed",
        "version_id": version_id,
        "error": {
            "type": type(error).__name__,
            "code": error_code,
            "message": str(error),
        },
        "pipeline_metrics": metrics,
    }
    (output / "evaluation-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


async def record_checkpoint_pause(
    database,
    *,
    output: Path,
    plan: dict[str, object],
    version_id: str,
    pause: Exception,
) -> dict[str, object]:
    result = await record_failed_paid_run(
        database,
        output=output,
        plan=plan,
        version_id=version_id,
        error=pause,
    )
    result.update({
        "status": "paused",
        "checkpoint_stage": getattr(
            pause, "checkpoint_stage", "all_scenes_v1"
        ),
        "resumable": True,
    })
    (output / "evaluation-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


async def prepare_resume_paid_run(
    database,
    *,
    output: Path,
    current_plan: dict[str, object],
    report_provider: str,
    report_model: str,
    search_provider: str,
    search_model: str,
) -> tuple[dict[str, object], str]:
    from sqlalchemy import select

    from audio_memory.analysis.pipeline_identity import (
        BETA8_INDEXED_PIPELINE_KIND,
        validate_version_pipeline_identity,
    )
    from audio_memory.models import AnalysisJob, AnalysisVersion

    output = output.resolve()
    plan_path = output / "acceptance-plan.json"
    database_path = output / "evaluation.sqlite3"
    if not plan_path.is_file() or not database_path.is_file():
        raise ValueError("Resume output must contain its acceptance plan and database")
    saved_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(saved_plan, dict):
        raise ValueError("Saved acceptance plan must be a JSON object")
    if Path(str(saved_plan.get("planned_output_directory", ""))).resolve() != output:
        raise ValueError("Saved acceptance output directory changed")
    if saved_plan.get("run_id") != output.name:
        raise ValueError("Saved acceptance run ID does not match its directory")
    for binding in ("input", "ground_truth", "prompt_binding", "expected_stages"):
        if saved_plan.get(binding) != current_plan.get(binding):
            raise ValueError(f"Resume {binding.replace('_', ' ')} binding changed")

    async with database.session() as session:
        versions = list((await session.scalars(select(AnalysisVersion))).all())
    if len(versions) != 1:
        raise ValueError("Resume database must contain exactly one analysis version")
    version = versions[0]
    identity = validate_version_pipeline_identity(version)
    if identity.pipeline_kind != BETA8_INDEXED_PIPELINE_KIND:
        raise ValueError("Resume version is not the indexed Beta 8 pipeline")
    if (version.provider_id, version.model_id) != (report_provider, report_model):
        raise ValueError("Resume report provider binding changed")
    if (identity.search_provider_id, identity.search_model_id) != (
        search_provider,
        search_model,
    ):
        raise ValueError("Resume search provider binding changed")
    if version.fixed_rules_hash != saved_plan["prompt_binding"]["fixed_rules_hash"]:
        raise ValueError("Resume fixed-rules binding changed")
    if version.status not in {"running", "failed"} or version.worker_owner_id not in {
        None,
        "beta8-indexed-v1-acceptance",
    }:
        raise ValueError("Resume version is not safely reclaimable")
    try:
        staged = json.loads(version.staged_results_json or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Resume checkpoints must be valid JSON") from error
    if not isinstance(staged, dict) or "beta8_event_index" not in staged:
        raise ValueError("Resume run has no event-index checkpoint")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, version.id)
        job = await session.get(AnalysisJob, version.source_job_id)
        version.status = "running"
        version.error_code = None
        version.worker_owner_id = "beta8-indexed-v1-acceptance"
        version.lease_expires_at = None
        version.completed_at = None
        if job is not None:
            job.stage = "analyzing"
            job.error_code = None
        await session.commit()
    return saved_plan, str(version.id)


async def run_paid(args: argparse.Namespace, paths: EvaluationPaths) -> dict[str, object]:
    if not getattr(args, "run_paid", False) or not getattr(args, "confirmed", False):
        raise PermissionError(
            "Paid evaluation requires --run-paid and explicit confirmation"
        )

    import httpx

    from audio_memory.analysis.beta8_runner import (
        Beta8CheckpointPause,
        Beta8ReportRunner,
    )
    from audio_memory.analysis.beta8_search import Beta8SearchExecutor
    from audio_memory.analysis.provider import ProviderAnalysisClient
    from audio_memory.db import Database
    from audio_memory.models import AnalysisVersion
    from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
    from audio_memory.providers.types import PROVIDER_CONFIGS

    for provider_id, model_id, role in (
        (args.report_provider, args.report_model, "report"),
        (args.search_provider, args.search_model, "search"),
    ):
        config = PROVIDER_CONFIGS.get(provider_id)
        if config is None or not config.supports_model(model_id):
            raise ValueError(f"Unsupported {role} provider/model: {provider_id}/{model_id}")
    execution_binding = validate_execution_binding(args)

    current_plan = prepare_dry_run(paths)
    current_plan["execution_binding"] = execution_binding
    execution_control = {
        "stop_after_event_index_checkpoint": bool(
            getattr(args, "stop_after_index", False)
        ),
        "stop_after_v1_checkpoint": bool(
            getattr(args, "stop_after_v1", False)
        ),
        "provider_transient_total_attempts": 1,
    }
    current_plan["execution_control"] = execution_control
    resume_output = getattr(args, "resume_output", None)
    if resume_output is not None:
        output = Path(resume_output).expanduser().resolve()
        if output.parent != paths.output_root.expanduser().resolve():
            raise ValueError("Resume output must be a direct child of the output root")
        database = Database(output / "evaluation.sqlite3")
        try:
            plan, version_id = await prepare_resume_paid_run(
                database,
                output=output,
                current_plan=current_plan,
                report_provider=args.report_provider,
                report_model=args.report_model,
                search_provider=args.search_provider,
                search_model=args.search_model,
            )
        except BaseException:
            await database.dispose()
            raise
        plan = {**plan, "execution_control": execution_control}
    else:
        plan = current_plan
        output = Path(str(plan["planned_output_directory"]))
        output.mkdir(parents=True, exist_ok=False)
        (output / "acceptance-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rows = parse_merged_transcript(paths.transcript)
        database = Database(output / "evaluation.sqlite3")
        await database.create_schema()
        version_id = await seed_paid_run(
            database,
            rows,
            report_provider=args.report_provider,
            report_model=args.report_model,
            search_provider=args.search_provider,
            search_model=args.search_model,
        )
    ground_truth = load_json_object(paths.ground_truth)
    try:
        async with httpx.AsyncClient() as http:
            provider = ProviderAnalysisClient(
                KeychainRepository(MacSecurityClient()),
                http,
                transient_total_attempts=1,
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
                stop_after_event_index_checkpoint=execution_control[
                    "stop_after_event_index_checkpoint"
                ],
                stop_after_v1_checkpoint=execution_control[
                    "stop_after_v1_checkpoint"
                ],
                response_quarantine=ResponseQuarantine(output).capture,
                event_index_gate=lambda index: (
                    save_index_acceptance(index, ground_truth, output)
                ),
            )
            try:
                outcome = await runner.run(
                    version_id, "beta8-indexed-v1-acceptance"
                )
            except Beta8CheckpointPause as pause:
                return await record_checkpoint_pause(
                    database,
                    output=output,
                    plan=plan,
                    version_id=version_id,
                    pause=pause,
                )
            except Exception as error:
                await record_failed_paid_run(
                    database,
                    output=output,
                    plan=plan,
                    version_id=version_id,
                    error=error,
                )
                raise
        async with database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
        result = {
            **mark_paid_execution(plan),
            "version_id": version_id,
            "card_count": outcome.card_count,
            "todo_count": outcome.todo_count,
            "pipeline_metrics": json.loads(version.pipeline_metrics_json or "{}"),
        }
        (output / "evaluation-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        await database.dispose()


def main() -> None:
    args = parse_args()
    paths = evaluation_paths(args)
    if args.dry_run:
        if args.confirmed:
            raise ValueError("--confirmed is valid only with --run-paid")
        plan = prepare_dry_run(paths)
        plan["execution_binding"] = validate_execution_binding(args)
        plan["execution_control"] = {
            "stop_after_event_index_checkpoint": bool(args.stop_after_index),
            "stop_after_v1_checkpoint": bool(args.stop_after_v1),
            "provider_transient_total_attempts": 1,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    require_paid_confirmation(args.confirmed)
    print(json.dumps(asyncio.run(run_paid(args, paths)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
