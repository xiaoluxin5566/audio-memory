#!/usr/bin/env python3
"""Run the explicitly authorized, isolated Beta 8 V1 architecture experiment."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import time

import httpx

from audio_memory.analysis.beta8_evaluation import parse_merged_transcript
from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.experiments.beta8_v1_ab import Beta8V1ABRunner, ExperimentPromptSet
from audio_memory.providers.keychain import (
    KeychainRepository,
    KeychainStatus,
    MacSecurityClient,
)
from audio_memory.providers.types import PROVIDER_CONFIGS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(
    "/Users/liujinxin/Documents/音频Always on Demo/outputs/"
    "2026-07-29-four-audio-transcripts/2026-07-29_六段音频_合并逐字稿.md"
)
CONFIRMATION = "I_AUTHORIZE_PAID_BETA8_V1_AB"


class ProgressProvider:
    def __init__(self, provider: ProviderAnalysisClient) -> None:
        self.provider = provider

    @property
    def request_diagnostics(self):
        return self.provider.request_diagnostics

    async def generate(self, provider_id: str, **kwargs) -> str:
        stage = str(kwargs.get("scene_id"))
        started = time.monotonic()
        print(
            json.dumps({"event": "started", "stage": stage}, ensure_ascii=False),
            flush=True,
        )
        try:
            return await self.provider.generate(provider_id, **kwargs)
        finally:
            print(
                json.dumps(
                    {
                        "event": "finished",
                        "stage": stage,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume-variant-a", action="store_true")
    parser.add_argument("--resume-variant-b", action="store_true")
    parser.add_argument("--resumed-variant-a-elapsed-seconds", type=float)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm-paid-calls")
    return parser.parse_args()


def source_summary(source: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source": str(source),
        "source_sha256": sha256(source.read_bytes()).hexdigest(),
        "source_bytes": source.stat().st_size,
        "file_count": len({int(row["file_position"]) for row in rows}),
        "segment_count": len(rows),
        "transcript_characters": sum(len(str(row["text"])) for row in rows),
    }


async def execute(args: argparse.Namespace) -> None:
    source = args.source.expanduser().resolve()
    rows = parse_merged_transcript(source)
    keychain = KeychainRepository(MacSecurityClient())
    credential_status = keychain.read("deepseek").status
    summary = {
        **source_summary(source, rows),
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "model_supported": PROVIDER_CONFIGS["deepseek"].supports_model(
            "deepseek-v4-pro"
        ),
        "credential_status": credential_status.value,
        "prompt_hashes": ExperimentPromptSet().manifest(),
    }
    if args.preflight:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if args.confirm_paid_calls != CONFIRMATION:
        raise PermissionError(
            f"Paid calls require --confirm-paid-calls {CONFIRMATION}"
        )
    if (
        not (args.resume_variant_a and args.resume_variant_b)
        and credential_status is not KeychainStatus.CONFIGURED
    ):
        raise RuntimeError(f"DeepSeek credential is {credential_status.value}")

    output = (
        args.output
        or ROOT
        / "outputs"
        / "beta8-v1-architecture-ab"
        / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    ).expanduser().resolve()
    if output.exists() and not args.resume_variant_a:
        raise FileExistsError(f"Output already exists: {output}")
    output.mkdir(parents=True, exist_ok=args.resume_variant_a)
    (output / "input-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    async with httpx.AsyncClient() as http:
        provider = ProgressProvider(ProviderAnalysisClient(keychain, http))
        result = await Beta8V1ABRunner(provider).run(
            rows=rows,
            output=output,
            source=source,
            resume_variant_a=args.resume_variant_a,
            resumed_variant_a_elapsed_seconds=(
                args.resumed_variant_a_elapsed_seconds
            ),
            resume_variant_b=args.resume_variant_b,
        )
    print(
        json.dumps(
            {"event": "complete", "output": str(output), "metrics": result},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(execute(parse_args()))
