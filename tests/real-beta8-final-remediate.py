#!/usr/bin/env python3
"""Apply one evidence-bounded terminal-audit remediation pass and re-audit."""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re
import time

import httpx

from audio_memory.analysis.beta8_audit import aggregate_audit_results, plan_audit_units
from audio_memory.analysis.beta8_cleanup import remove_redundant_section_labels
from audio_memory.analysis.beta8_evaluation import parse_merged_transcript, require_paid_confirmation
from audio_memory.analysis.provider import ProviderAnalysisClient
from audio_memory.analysis.beta8_state import Beta8StageRecord
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion
from audio_memory.prompts.beta8_composer import Beta8PromptComposer
from audio_memory.prompts.beta8_pipeline_schema import (
    AggregatedAudit,
    Beta8AuditResult,
    Beta8FinalCard,
    Beta8PublicationBundle,
    Beta8RevisedCard,
    validate_publication_bundle,
)
from audio_memory.prompts.beta8_scene_schema import (
    parse_beta8_card_markdown,
    validate_scene_result_ids,
)
from audio_memory.providers.keychain import KeychainRepository, MacSecurityClient


CONFIRMATION = "I_AUTHORIZE_PAID_BETA8_EVALUATION"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--confirm-paid-calls")
    return parser.parse_args()


def stage_payload(staged: dict[str, object], key: str) -> object:
    return Beta8StageRecord.model_validate(staged[key]).payload


async def generate_valid(
    provider: ProviderAnalysisClient,
    *,
    provider_id: str,
    model_id: str,
    request,
    validator,
    invalid_tag: str,
    allow_parallel: bool = False,
):
    raw = await provider.generate(
        provider_id,
        system=request.instructions,
        user=request.user_data,
        model_id=model_id,
        scene_id=request.scene_id,
        max_tokens=request.max_tokens,
        timeout_seconds=request.timeout_seconds,
        segment_count=request.segment_count,
        allow_parallel=allow_parallel,
    )
    for repair_index in range(3):
        try:
            return validator(raw)
        except (ValueError, json.JSONDecodeError) as error:
            if repair_index == 2:
                raise
            escaped = raw.replace("</", "<\\/")
            raw = await provider.generate(
                provider_id,
                system=(
                    request.instructions
                    + "\n\n上一次输出未通过契约校验。基于下方完整旧输出"
                    "做最小修改，只修复该错误，不得删除有证据支持的内容：\n"
                    + str(error)
                ),
                user=(
                    request.user_data
                    + f'<{invalid_tag} untrusted="true">'
                    + escaped
                    + f"</{invalid_tag}>"
                ),
                model_id=model_id,
                scene_id=request.scene_id,
                max_tokens=request.max_tokens,
                timeout_seconds=request.timeout_seconds,
                segment_count=request.segment_count,
                allow_parallel=allow_parallel,
                repair_attempted=True,
            )


async def execute(args: argparse.Namespace) -> None:
    require_paid_confirmation(args.confirm_paid_calls == CONFIRMATION)
    started = time.monotonic()
    rows = parse_merged_transcript(args.source.expanduser().resolve())
    known_ids = {str(row["segment_id"]) for row in rows}
    database = Database(args.database.expanduser().resolve())
    async with database.session() as session:
        version = await session.get(AnalysisVersion, args.version_id)
    if version is None:
        raise ValueError("Analysis version not found")
    staged = json.loads(version.staged_results_json)
    candidate = stage_payload(staged, "beta8_final_candidate")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.cycle == 1:
        base_payload = {
            "cards": candidate["cards"],
            "terminal_audit": stage_payload(
                staged, "beta8_final_audit_aggregate"
            ),
            "remediation_task_ids": [],
        }
    else:
        previous_name = (
            "terminal-remediation.json"
            if args.cycle == 2
            else f"terminal-remediation-cycle-{args.cycle - 1}.json"
        )
        base_payload = json.loads((output / previous_name).read_text(encoding="utf-8"))
    final_audit = AggregatedAudit.model_validate(base_payload["terminal_audit"])
    cards = [Beta8FinalCard.model_validate(item) for item in base_payload["cards"]]
    cards_by_id = {card.card_id: card for card in cards}
    composer = Beta8PromptComposer()
    state_suffix = "" if args.cycle == 1 else f"-cycle-{args.cycle}"
    state_path = output / f"terminal-remediation-state{state_suffix}.json"
    base_fingerprint = sha256(
        json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("base_fingerprint") != base_fingerprint:
            raise ValueError("Terminal remediation base candidate changed")
    else:
        state = {
            "base_fingerprint": base_fingerprint,
            "remediations": {},
            "audit_units": {},
        }

    def save_state() -> None:
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async with httpx.AsyncClient() as http:
        provider = ProviderAnalysisClient(KeychainRepository(MacSecurityClient()), http)
        next_position = len(cards)
        remediated_ids: list[str] = []
        deterministic_issue_ids: set[str] = set()
        for issue in final_audit.issues:
            if issue.issue_type != "structure_or_style" or issue.card_id is None:
                continue
            source = cards_by_id[issue.card_id]
            cleaned_markdown = remove_redundant_section_labels(source.markdown)
            if cleaned_markdown == source.markdown:
                continue
            parsed = parse_beta8_card_markdown(cleaned_markdown)
            updated = source.model_copy(update={
                "title": parsed.title,
                "summary": parsed.summary,
                "markdown": cleaned_markdown,
                "final_markdown_sha256": sha256(
                    cleaned_markdown.encode()
                ).hexdigest(),
                "untouched": False,
            })
            cards = [
                updated if card.card_id == issue.card_id else card for card in cards
            ]
            cards_by_id[issue.card_id] = updated
            deterministic_issue_ids.add(issue.audit_issue_id)
            remediated_ids.append(
                f"terminal-cycle-{args.cycle}-cleanup-{issue.audit_issue_id}"
            )
        deleted_issue_ids: set[str] = set()
        for issue in final_audit.issues:
            if (
                issue.issue_type == "wrong_card_boundary"
                and issue.card_id is not None
                and "删除" in issue.required_change
                and not issue.evidence_segment_ids
            ):
                cards = [card for card in cards if card.card_id != issue.card_id]
                cards = [
                    card.model_copy(update={"position": position})
                    for position, card in enumerate(cards)
                ]
                cards_by_id = {card.card_id: card for card in cards}
                deleted_issue_ids.add(issue.audit_issue_id)
                remediated_ids.append(
                    f"terminal-cycle-{args.cycle}-delete-{issue.audit_issue_id}"
                )
        remediation_items = [
            (issue, False)
            for issue in final_audit.issues
            if issue.audit_issue_id not in deleted_issue_ids
            and issue.audit_issue_id not in deterministic_issue_ids
        ]
        remediation_items.extend(
            (issue, True)
            for issue in final_audit.issues
            if (
                issue.audit_issue_id not in deleted_issue_ids
                and issue.issue_type == "wrong_card_boundary"
                and issue.card_id is not None
                and ("拆" in issue.required_change or "独立" in issue.required_change)
                and "删除" not in issue.required_change
            )
        )
        for issue_index, (issue, create_split) in enumerate(
            remediation_items, start=1
        ):
            if issue.card_id is None or create_split:
                operation = "create"
                target_scene_id = (
                    cards_by_id[issue.card_id].scene_id
                    if create_split and issue.card_id is not None
                    else issue.suggested_scene_id
                )
                source_cards = []
                reserved_card_id = f"final-card-{next_position + 1:04d}"
                next_position += 1
            else:
                operation = "revise"
                source = cards_by_id[issue.card_id]
                target_scene_id = source.scene_id
                source_cards = [source.model_dump(mode="json")]
                reserved_card_id = source.card_id
            if target_scene_id is None:
                raise ValueError("Terminal remediation issue has no target scene")
            requirement_id = (
                f"terminal-cycle-{args.cycle}-requirement-{issue_index:04d}"
            )
            evidence_segment_ids = list(issue.evidence_segment_ids)
            instruction = issue.required_change
            if "Treo" in issue.problem and "Trello" in issue.problem:
                evidence_segment_ids = list(dict.fromkeys([
                    *evidence_segment_ids,
                    "seg_2_115",
                ]))
                instruction = (
                    "逐字稿分别提到 Treo 和 Trello，二者都应保留，不能互相替换；"
                    "同时保持 Walkie Talkie、Workbody（Trework）等已有准确名称。"
                )
            if create_split and issue.card_id is not None:
                source_ids = cards_by_id[issue.card_id].source_segment_ids
                evidence_segment_ids = [
                    segment_id
                    for segment_id in source_ids
                    if (
                        (match := re.fullmatch(r"seg_2_(\d+)", segment_id))
                        and 2_800 <= int(match.group(1)) <= 2_900
                    )
                ]
                instruction = (
                    "把原卡中的出行前安全感事件独立成一张亲子与家庭卡："
                    "孩子误以为父母不带她出行而委屈，父母解释是去山上的"
                    "阿那亚并明确会带她；保留准确原话和可用的安抚启示。"
                )
            task = {
                "revision_task_key": (
                    f"terminal-cycle-{args.cycle}-remediation-{issue_index:04d}"
                ),
                "decision_key": (
                    f"terminal-cycle-{args.cycle}-decision-{issue_index:04d}"
                ),
                "operation": operation,
                "target_scene_id": target_scene_id,
                "source_card_ids": [] if issue.card_id is None else [issue.card_id],
                "audit_issue_ids": [issue.audit_issue_id],
                "requirements": [{
                    "requirement_id": requirement_id,
                    "instruction": instruction,
                    "evidence_segment_ids": evidence_segment_ids,
                }],
                "preserve_points": (
                    [] if issue.card_id is None
                    else ["保留原卡中与本终审问题无关且有证据支持的内容"]
                ),
                "remove_or_avoid": [issue.problem],
                "completion_criteria": [
                    instruction,
                    f"reserved_card_id 必须原样返回 {reserved_card_id}",
                ],
                "search_task_keys": [],
            }
            evidence_ids = set(evidence_segment_ids)
            evidence_rows = [row for row in rows if row["segment_id"] in evidence_ids]
            request = composer.compose_revision(
                target_scene_id=target_scene_id,
                source_cards=source_cards,
                revision_task=task,
                transcript_segments=evidence_rows,
                search_packets=[],
            )

            def validate_revision(raw: str) -> Beta8RevisedCard:
                result = Beta8RevisedCard.model_validate(json.loads(raw))
                if result.reserved_card_id != reserved_card_id:
                    raise ValueError(
                        f"reserved_card_id must be {reserved_card_id}"
                    )
                if result.operation != operation:
                    raise ValueError(f"operation must be {operation}")
                result.validate_requirement_ids({requirement_id})
                parse_beta8_card_markdown(result.markdown)
                unknown = sorted(set(result.source_segment_ids) - known_ids)
                if unknown:
                    raise ValueError(
                        "Unknown transcript segment IDs: " + ", ".join(unknown)
                    )
                return result

            remediation_state_key = (
                issue.audit_issue_id + (":create-split" if create_split else "")
            )
            saved_result = state["remediations"].get(remediation_state_key)
            if saved_result is None:
                result = await generate_valid(
                    provider,
                    provider_id=version.provider_id,
                    model_id=version.model_id,
                    request=request,
                    validator=validate_revision,
                    invalid_tag="beta8_invalid_terminal_remediation_output",
                )
                state["remediations"][remediation_state_key] = result.model_dump(
                    mode="json"
                )
                save_state()
            else:
                result = validate_revision(json.dumps(saved_result, ensure_ascii=False))
            parsed = parse_beta8_card_markdown(result.markdown)
            if operation == "revise":
                old = cards_by_id[reserved_card_id]
                updated = Beta8FinalCard.model_validate({
                    **old.model_dump(mode="json"),
                    "title": parsed.title,
                    "summary": parsed.summary,
                    "markdown": result.markdown,
                    "source_segment_ids": result.source_segment_ids,
                    "used_source_ids": result.used_source_ids,
                    "final_markdown_sha256": sha256(result.markdown.encode()).hexdigest(),
                    "untouched": False,
                })
                cards[old.position] = updated
                cards_by_id[reserved_card_id] = updated
            else:
                created = Beta8FinalCard.model_validate({
                    "card_id": reserved_card_id,
                    "scene_id": target_scene_id,
                    "position": len(cards),
                    "title": parsed.title,
                    "summary": parsed.summary,
                    "markdown": result.markdown,
                    "source_segment_ids": result.source_segment_ids,
                    "used_source_ids": result.used_source_ids,
                    "origin_card_ids": [],
                    "v1_markdown_sha256": None,
                    "final_markdown_sha256": sha256(result.markdown.encode()).hexdigest(),
                    "untouched": False,
                })
                cards.append(created)
                cards_by_id[reserved_card_id] = created
            remediated_ids.append(task["revision_task_key"])

        units = plan_audit_units(rows, phase="final", max_markdown_chars=70_000)

        async def audit(unit):
            request = composer.compose_audit(
                phase="final",
                scope=unit.scope,
                audit_unit_id=unit.audit_unit_id,
                cards=[card.model_dump(mode="json") for card in cards],
                transcript_segments=unit.transcript_segments,
                revision_requirements=[],
            )

            def validate_audit(raw: str) -> Beta8AuditResult:
                payload = json.loads(raw)
                for issue_payload in payload.get("issues", []):
                    if issue_payload.get("issue_type") != "missed_high_value_content":
                        issue_payload["suggested_scene_id"] = None
                result = Beta8AuditResult.model_validate(payload)
                if result.audit_unit_id != unit.audit_unit_id:
                    raise ValueError("audit_unit_id must be returned unchanged")
                return result

            return await generate_valid(
                provider,
                provider_id=version.provider_id,
                model_id=version.model_id,
                request=request,
                validator=validate_audit,
                invalid_tag="beta8_invalid_terminal_reaudit_output",
                allow_parallel=True,
            )

        saved_audit_units = state["audit_units"]
        audit_results_by_id = {
            unit_id: Beta8AuditResult.model_validate(payload)
            for unit_id, payload in saved_audit_units.items()
        }
        missing_units = [
            unit for unit in units if unit.audit_unit_id not in audit_results_by_id
        ]
        outcomes = await asyncio.gather(
            *(audit(unit) for unit in missing_units), return_exceptions=True
        )
        first_error = None
        for unit, outcome in zip(missing_units, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                first_error = first_error or outcome
                continue
            audit_results_by_id[unit.audit_unit_id] = outcome
            saved_audit_units[unit.audit_unit_id] = outcome.model_dump(mode="json")
        save_state()
        if first_error is not None:
            raise first_error
        audit_results = [audit_results_by_id[unit.audit_unit_id] for unit in units]
        aggregate = aggregate_audit_results(units, audit_results)
        diagnostics = provider.request_diagnostics
        metrics = {
            "duration_ms": int((time.monotonic() - started) * 1_000),
            "input_tokens": sum(item.input_tokens for item in diagnostics),
            "output_tokens": sum(item.output_tokens for item in diagnostics),
            "model_call_count": len(diagnostics),
        }
        remediation_payload = {
            "cards": [card.model_dump(mode="json") for card in cards],
            "terminal_audit": aggregate.model_dump(mode="json"),
            "remediation_task_ids": list(dict.fromkeys([
                *base_payload.get("remediation_task_ids", []),
                *remediated_ids,
            ])),
            "metrics": metrics,
        }
        remediation_name = (
            "terminal-remediation.json"
            if args.cycle == 1
            else f"terminal-remediation-cycle-{args.cycle}.json"
        )
        (output / remediation_name).write_text(
            json.dumps(remediation_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if aggregate.issues:
            raise ValueError(
                f"terminal re-audit still has {len(aggregate.issues)} issues"
            )

        final_hashes = {
            card.card_id: card.final_markdown_sha256 for card in cards
        }
        bundle = Beta8PublicationBundle.model_validate({
            **candidate,
            "cards": [card.model_dump(mode="json") for card in cards],
            "final_card_hashes": final_hashes,
            "untouched_card_ids": [card.card_id for card in cards if card.untouched],
            "completed_revision_task_ids": list(dict.fromkeys([
                *candidate["completed_revision_task_ids"],
                *base_payload.get("remediation_task_ids", []),
                *remediated_ids,
            ])),
        })
        validate_publication_bundle(bundle)
        (output / "publication-bundle.json").write_text(
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        cards_dir = output / "cards"
        cards_dir.mkdir(exist_ok=True)
        for old_card in cards_dir.glob("*.md"):
            old_card.unlink()
        for card in cards:
            (cards_dir / f"{card.position + 1:02d}-{card.scene_id}.md").write_text(
                card.markdown, encoding="utf-8"
            )
        result_name = (
            "terminal-remediation-result.json"
            if args.cycle == 1
            else f"terminal-remediation-result-cycle-{args.cycle}.json"
        )
        (output / result_name).write_text(
            json.dumps({
                "card_count": len(cards),
                "terminal_issue_count": 0,
                "metrics": metrics,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({
            "card_count": len(cards),
            "terminal_issue_count": 0,
            "metrics": metrics,
        }, ensure_ascii=False, indent=2))
    await database.dispose()


if __name__ == "__main__":
    asyncio.run(execute(parse_args()))
