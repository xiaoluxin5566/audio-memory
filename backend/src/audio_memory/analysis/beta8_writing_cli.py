"""Local preparation, explicitly authorized draft execution, and score import."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import httpx

from audio_memory.analysis.beta8_writing_evidence import EvidenceCatalog
from audio_memory.analysis.beta8_writing_pipeline import WritingPipeline
from audio_memory.analysis.beta8_writing_scoring import validate_score
from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStore, digest
from audio_memory.analysis.beta8_writing_transport import WritingTransport
from audio_memory.config import DEVELOPMENT_KEYCHAIN_SERVICE
from audio_memory.prompts.beta8_writing_composer import WritingPrompts
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient


def load_segments(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data["segments"]
    if not isinstance(data, list):
        raise ValueError("Input must be a complete segment list")
    result = []
    for item in data:
        row = dict(item)
        # Existing Beta 8 transcript exports use file_id; preserve the original too.
        if "source_file" not in row:
            row["source_file"] = row.get("file_id") or row.get("file_name")
        result.append(row)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = commands.add_parser(name)
        command.add_argument("--input", required=True)
        command.add_argument("--output", required=True)
        if name == "run":
            command.add_argument("--authorization", required=True)
            command.add_argument("--execute-paid", action="store_true", help="Requires a separately authorized, bound execution plan")
    score = commands.add_parser("import-scores")
    score.add_argument("--output", required=True)
    score.add_argument("--scores", required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(args.output)
        if args.command == "import-scores":
            identity = json.loads((root / "identity.json").read_text())
            store = WritingStore(root, identity)
            with store.locked():
                packet = store.read("scoring-packet.json")
                cards = {item["card"]["card_id"]: item["card"] for item in packet["cards"]}
                scores = json.loads(Path(args.scores).read_text())
                if not isinstance(scores, list) or len(scores) != len(cards) or {row["card_id"] for row in scores} != set(cards):
                    raise ValueError("Provide exactly one score for every stored card")
                validated = [validate_score(row, cards[row["card_id"]]) for row in scores]
                # Bind assessment to the actual saved card files, not just the packet.
                for row in validated:
                    validate_score(row, store.read("cards/" + WritingStore._safe_key(row["card_id"]) + ".json"))
                name = "scores-" + digest(validated)[:16] + ".json"
                store.write(name, {"cards": validated, "scope": "per_card", "generation_changed": False, "project_scoring_api_requests": 0})
            print(json.dumps({"scores": str(root / name), "cards": len(validated)}, ensure_ascii=False))
            return 0
        segments = load_segments(args.input)
        binding = {"input_sha256": digest(segments), "prompt_hash": WritingPrompts.fixed_rules_hash()}
        if args.command == "prepare":
            limits = WritingLimits()
            windows = EvidenceCatalog(segments).windows(limits.core_budget_bytes)
            capacities = [{"window_id": window["window_id"], "core_segment_count": len(window["core_segments"]),
                           "input_byte_upper_bound": len(json.dumps({"model": "deepseek-v4-pro", "messages": [
                               {"role": "system", "content": WritingPrompts.system("P1")},
                               {"role": "user", "content": WritingPrompts.user(dict(window, user_corrections=[]))}],
                               "max_tokens": limits.max_output_tokens, "thinking": {"type": "enabled"},
                               "response_format": {"type": "json_object"}, "temperature": 0, "stream": False}, ensure_ascii=False).encode())}
                          for window in windows]
            plan = dict(binding, pipeline="beta8_writing_v1", provider_id="deepseek", model_id="deepseek-v4-pro",
                search_provider_id="kimi", search_model_id="kimi-k2.6", credential_generation=0,
                search_credential_generation=0, user_corrections=[], report_period="", limits=asdict(limits),
                counts={"P1_windows": len(windows), "P2_planning": 1 if windows else 0, "P3_requests": None, "P4_cards": None, "score_requests": 0, "total_formula": "W + 1 + H + N"},
                input_capacity=capacities, requires_input_budget_adjustment=any(row["input_byte_upper_bound"] > limits.max_input_tokens for row in capacities),
                capacity_note="UTF-8 bytes provide a conservative token bound; no silent input truncation. Model token usage is recorded after responses.")
            root.mkdir(parents=True, exist_ok=True)
            with (root / "execution-plan.json").open("x") as stream:
                json.dump(plan, stream, ensure_ascii=False, indent=2)
            print(json.dumps({"plan": str(root / "execution-plan.json"), "windows": len(windows), "paid_calls": 0}, ensure_ascii=False))
            return 0
        if not args.execute_paid:
            raise ValueError("run requires an explicitly approved plan and --execute-paid")
        grant = json.loads(Path(args.authorization).read_text())
        if any(grant.get(key) != value for key, value in binding.items()) or grant.get("pipeline") != "beta8_writing_v1":
            raise ValueError("Execution plan input/Prompt binding mismatch")
        limits = WritingLimits(**grant["limits"])
        if not limits.allow_paid or limits.max_requests < 1:
            raise ValueError("Execution budget has not been enabled")
        async def execute():
            # Only the explicit run branch can construct a credentialed transport.
            async with httpx.AsyncClient(trust_env=False) as client:
                transport = WritingTransport(KeychainRepository(MacSecurityClient(), service=DEVELOPMENT_KEYCHAIN_SERVICE), client)
                pipeline = WritingPipeline(output_dir=root / "artifacts", transport=transport, limits=limits,
                    provider_id=grant["provider_id"], model_id=grant["model_id"],
                    search_provider_id=grant["search_provider_id"], search_model_id=grant["search_model_id"],
                    credential_generation=grant["credential_generation"], search_credential_generation=grant["search_credential_generation"],
                    retry_invalid_p1_windows=grant.get("retry_invalid_p1_windows", []))
                return await pipeline.run(segments, user_corrections=grant["user_corrections"], report_period=grant["report_period"])
        result = asyncio.run(execute())
        print(json.dumps({"status": result["status"], "metrics": result["metrics"], "output": str(root / "artifacts")}, ensure_ascii=False))
        return 0 if result["status"] in {"draft_ready", "draft_incomplete", "no_cards_planned"} else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "stopped", "reason": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
