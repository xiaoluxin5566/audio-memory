#!/usr/bin/env python3
"""Opt-in real report acceptance for DeepSeek Flash/Pro and Kimi."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time

import httpx

from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.analysis.single_report_runner import SingleReportRunner
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, JobFile, Transcript
from audio_memory.prompts.composer import PromptComposer
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient
from audio_memory.config import WHISPER_MODEL_ID
from audio_memory.transcription.engine import _transcribe_worker


CASES = (
    ("deepseek", "deepseek-v4-flash"),
    ("deepseek", "deepseek-v4-pro"),
    ("kimi", "kimi-k3"),
)

BASE_SEGMENTS = (
    "今天讨论语音记忆产品的测试计划，目标是确认报告链路稳定，不涉及真实用户数据。",
    "团队决定先验证模型凭证，然后用相同输入比较三个模型。",
    "小李负责整理测试结果，截止时间是明天下午三点。",
    "小王负责检查安装包和校验和，截止时间是明天中午十二点。",
    "风险一是模型输出被截断，需要保留初稿并递归拆分审计。",
    "风险二是模型返回的分项分数与总分算术不一致，应由程序重算总分。",
    "风险三是最终合并不是合法结构，程序只能重试一次，不能发布未经审计的报告。",
    "验收要求报告必须区分事实、判断和建议，不能虚构负责人或截止时间。",
    "所有模型使用同一份转写、同一份提示词和同一套质量门槛。",
    "测试输出只保留耗时、Token、请求场景和最终分数，不保存模型原文。",
    "如果某个模型失败，需要记录稳定错误类型和失败阶段，不能只说模型不可用。",
    "最终结论需要分别说明 Flash、Pro 和 Kimi 是否通过，不用平均结果掩盖单模型失败。",
)


def build_segments(segment_count: int) -> tuple[str, ...]:
    if segment_count < 1:
        raise ValueError("segment_count must be positive")
    return tuple(
        f"{BASE_SEGMENTS[index % len(BASE_SEGMENTS)]}（测试轮次 {index // len(BASE_SEGMENTS) + 1}）"
        for index in range(segment_count)
    )


def transcribe_audio(audio_path: Path) -> tuple[str, ...]:
    result = _transcribe_worker(
        str(audio_path),
        WHISPER_MODEL_ID,
        language="zh",
    )
    segments = tuple(
        str(item.get("text", "")).strip()
        for item in result
        if str(item.get("text", "")).strip()
    )
    if not segments:
        raise ValueError("audio transcription returned no text")
    return segments


class GenerationSource:
    async def credential_generation(self, provider_id: str) -> int:
        return 1


class Publisher:
    def __init__(self) -> None:
        self.reports = []

    async def publish(self, version_id, result, profile_candidates, **kwargs):
        self.reports.append(result)
        return {"version_id": version_id}


async def seed(
    database: Database,
    provider_id: str,
    model_id: str,
    segments: tuple[str, ...],
) -> None:
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(JobFile(
            id="file-1", job_id="job-1", original_name="fixed-test.txt",
            extension=".txt", size_bytes=1, sha256="a" * 64,
            duration_ms=len(segments) * 10_000, recording_started_at=None,
            recording_time_source="unknown", timezone="Asia/Shanghai",
            position=0, temporary_path="/private/tmp/fixed-test.txt",
        ))
        for index, text in enumerate(segments):
            session.add(Transcript(
                id=f"transcript-{index}", job_file_id="file-1",
                segment_index=index, speaker_id="speaker_1",
                start_ms=index * 10_000, end_ms=(index + 1) * 10_000,
                text=text, words_json="[]", risk_classified=True,
                is_reliable=True,
            ))
        session.add(AnalysisVersion(
            id="version-1", source_job_id="job-1", provider_id=provider_id,
            model_id=model_id, credential_generation=1,
            prompt_snapshot_json=json.dumps({
                "user-analysis-goal": {
                    "content": "整理事实、决定、风险、负责人和下一步，不得补写转写中不存在的信息。",
                    "version": 1,
                }
            }, ensure_ascii=False),
            profile_snapshot_json="[]",
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            staged_results_json="{}", status="running",
            worker_owner_id="worker-1",
        ))
        await session.commit()


async def run_case(
    root: Path,
    provider_id: str,
    model_id: str,
    segments: tuple[str, ...],
) -> dict[str, object]:
    database = Database(root / f"{provider_id}-{model_id}.sqlite3")
    await database.create_schema()
    await seed(database, provider_id, model_id, segments)
    publisher = Publisher()
    started = time.perf_counter()
    async with httpx.AsyncClient() as http:
        client = ProviderAnalysisClient(
            KeychainRepository(MacSecurityClient()), http
        )
        runner = SingleReportRunner(
            database=database, provider=client, publisher=publisher,
            generation_source=GenerationSource(),
        )
        try:
            await asyncio.wait_for(
                runner.run("version-1", "worker-1"), timeout=1_800
            )
        except Exception as exc:
            return {
                "provider_id": provider_id,
                "model_id": model_id,
                "segment_count": len(segments),
                "passed": False,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "error_type": type(exc).__name__,
                "diagnostics": [asdict(item) for item in client.request_diagnostics],
            }
        finally:
            await database.dispose()
    report = publisher.reports[0]
    metadata = report.quality_metadata
    diagnostics = [asdict(item) for item in client.request_diagnostics]
    observed_models = {item["model_id"] for item in diagnostics}
    observed_scenes = {item["scene_id"] for item in diagnostics}
    passed = (
        bool(report.report_markdown.strip())
        and metadata is not None
        and metadata.audit_status.startswith("completed")
        and observed_models == {model_id}
        and "direct-report" in observed_scenes
        and "direct-report-audit-chunk" in observed_scenes
    )
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "segment_count": len(segments),
        "passed": passed,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "report_version": metadata.report_version,
        "audit_status": metadata.audit_status,
        "quality_score": metadata.quality_score,
        "request_count": len(diagnostics),
        "usage": client.usage_totals,
        "diagnostics": diagnostics,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--segment-count", type=int, default=len(BASE_SEGMENTS))
    parser.add_argument("--audio", type=Path)
    parser.add_argument(
        "--model-id",
        action="append",
        choices=[model_id for _, model_id in CASES],
        help="Run only selected model IDs; may be repeated.",
    )
    args = parser.parse_args()
    segments = (
        transcribe_audio(args.audio)
        if args.audio is not None
        else build_segments(args.segment_count)
    )
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="audio-memory-three-model-") as temp:
        results = []
        selected_cases = tuple(
            item for item in CASES
            if not args.model_id or item[1] in set(args.model_id)
        )
        for provider_id, model_id in selected_cases:
            result = await run_case(Path(temp), provider_id, model_id, segments)
            results.append(result)
            print(json.dumps({
                key: value for key, value in result.items()
                if key != "diagnostics"
            }, ensure_ascii=False), flush=True)
    summary = {"passed": all(item["passed"] for item in results), "results": results}
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.output.chmod(0o600)
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
