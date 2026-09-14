from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from audio_memory.analysis.beta8_p1_p5_evidence import P1P5EvidenceCatalog
from audio_memory.analysis.beta8_p1_p5_pipeline import P1P5Pipeline
from audio_memory.analysis.beta8_writing_scoring import pending_score, RUBRIC_VERSION
from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStopped, WritingStore, digest
from audio_memory.analysis.beta8_writing_transport import WritingTransport
from audio_memory.config import DEVELOPMENT_KEYCHAIN_SERVICE
from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient


def read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path}")
    return value


async def rerun(source: Path, output: Path, *, execute_paid: bool) -> dict:
    source = source.resolve()
    output = output.resolve()
    source_input = read(source / "input.json")
    plan = read(source / "card-plan.json")
    registry = read(source / "content-registry.json")
    research = read(source / "research-packets.json")
    source_identity = read(source / "identity.json")
    segments = source_input.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Source run has no complete transcript segments")
    if plan.get("status") != "complete" or not isinstance(plan.get("cards"), list) or not plan["cards"]:
        raise ValueError("Source run has no complete P2 card plan")

    limits = WritingLimits(
        allow_paid=execute_paid,
        max_requests=len(plan["cards"]) * 2,
        max_input_tokens=2_500_000,
        max_total_input_tokens=30_000_000,
        max_output_tokens=96_000,
        max_search_output_tokens=24_000,
        max_total_output_tokens=400_000,
        core_budget_bytes=80_000,
        max_research_tasks=0,
        source_limit=5,
    )
    identity = {
        "pipeline": "beta8_p4_p5_rerun_v1",
        "source_artifacts": str(source),
        "source_input_sha256": digest(segments),
        "source_plan_sha256": digest(plan),
        "source_research_sha256": digest(research),
        "prompt_hash": P1P5Prompts.fixed_rules_hash(),
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "credential_generation": source_identity.get("credential_generation", 0),
        "max_requests": limits.max_requests,
        "scope": "reuse_P1_P2_P3_and_rerun_only_P4_P5",
    }
    if not execute_paid:
        return {
            "status": "prepared",
            "identity": identity,
            "card_count": len(plan["cards"]),
            "P4_requests": len(plan["cards"]),
            "P5_requests": len(plan["cards"]),
            "total_requests": len(plan["cards"]) * 2,
        }

    async with httpx.AsyncClient(trust_env=False) as client:
        transport = WritingTransport(
            KeychainRepository(MacSecurityClient(), service=DEVELOPMENT_KEYCHAIN_SERVICE),
            client,
        )
        pipeline = P1P5Pipeline(
            output_dir=output,
            transport=transport,
            limits=limits,
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
        )
        pipeline.store = WritingStore(output, identity)
        catalog = P1P5EvidenceCatalog(segments)
        initial_count = pipeline.store.metrics()["model_request_count"]
        cards: list[dict] = []
        scoring_inputs: list[dict] = []
        scores: list[dict] = []
        result = {
            "status": "stopped",
            "cards": cards,
            "scores": scores,
            "audit_performed": False,
            "revision_performed": False,
            "published": False,
            "rubric_version": RUBRIC_VERSION,
            "scoring_status": "pending",
            "failure": None,
            "reused_stages": ["P1", "P2", "P3"],
        }
        with pipeline.store.locked():
            try:
                pipeline.store.write("input.json", source_input)
                pipeline.store.write("content-registry.json", registry)
                pipeline.store.write("card-plan.json", plan)
                pipeline.store.write("research-packets.json", research)
                for brief in plan["cards"]:
                    card_id = "card-" + digest(brief["draft_key"])[:16]
                    packet = read(source / "cards" / f"{card_id}.input.json")
                    if packet.get("card_id") != card_id or packet.get("card_brief") != brief:
                        raise ValueError(f"Source P4 packet does not match P2 plan: {card_id}")
                    pipeline.store.write(f"cards/{card_id}.input.json", packet)
                    pipeline._check_input_capacity("P4", packet, brief["scene_id"])
                    card = await pipeline._stage(
                        "P4",
                        packet,
                        scene_id=brief["scene_id"],
                        validate=lambda value, material=packet: catalog.validate_card(value, material),
                    )
                    pipeline.store.write(f"cards/{card_id}.json", card)
                    pipeline.store.write_text(f"cards/{card_id}.md", card["markdown"])
                    cards.append(card)
                    scoring_inputs.append({"card": card, "input": packet, "score": pending_score(card, packet)})
                    pipeline._finish(result, scoring_inputs, initial_count)

                result["status"] = "draft_ready" if all(card["status"] == "written" for card in cards) else "draft_incomplete"
                for item in scoring_inputs:
                    scoring_payload = pipeline._scoring_payload(item)
                    pipeline._check_input_capacity("P5", scoring_payload)
                    assessment = await pipeline._stage(
                        "P5",
                        scoring_payload,
                        validate=lambda value, card=item["card"]: pipeline._validate_assessment(value, card),
                    )
                    scores.append(assessment["card"])
                    pipeline.store.write("scores.json", {
                        "status": "complete" if all(score["status"] == "scored" for score in scores) else "insufficient_input",
                        "cards": scores,
                    })
                    pipeline._finish(result, scoring_inputs, initial_count)

                result["scoring_status"] = "scored" if all(score["status"] == "scored" for score in scores) else "pending"
                result["status"] = "scored_v1" if result["scoring_status"] == "scored" else "draft_ready_scoring_incomplete"
            except Exception as error:
                if cards and result["status"] in {"draft_ready", "draft_incomplete"}:
                    result["status"] = "draft_ready_scoring_failed"
                    result["scoring_status"] = "failed"
                result["failure"] = {
                    "type": type(error).__name__,
                    "code": getattr(error, "code", None),
                    "reason": str(error) if isinstance(error, (WritingStopped, ValueError)) else "stage_failed_see_saved_response",
                }
                if getattr(error, "transport_error_type", None):
                    result["failure"]["transport_error_type"] = error.transport_error_type
                pipeline.store.write("failure.json", result["failure"])
            return pipeline._finish(result, scoring_inputs, initial_count)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(rerun(args.source, args.output, execute_paid=args.execute_paid))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"prepared", "scored_v1", "draft_ready_scoring_incomplete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
