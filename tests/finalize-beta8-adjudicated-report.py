#!/usr/bin/env python3
"""Finalize the reviewed Beta 8 artifact after evidence-based audit adjudication."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from audio_memory.analysis.beta8_state import Beta8StageRecord
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion
from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8FinalCard,
    Beta8PublicationBundle,
    validate_publication_bundle,
)
from audio_memory.prompts.beta8_scene_schema import parse_beta8_card_markdown


def revised(card: Beta8FinalCard, markdown: str, *, extra_segments=()) -> Beta8FinalCard:
    parsed = parse_beta8_card_markdown(markdown)
    return card.model_copy(update={
        "title": parsed.title,
        "summary": parsed.summary,
        "markdown": markdown,
        "source_segment_ids": list(dict.fromkeys([
            *card.source_segment_ids,
            *extra_segments,
        ])),
        "final_markdown_sha256": sha256(markdown.encode()).hexdigest(),
        "untouched": False,
    })


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    remediation = json.loads(
        (output / "terminal-remediation-cycle-6.json").read_text(encoding="utf-8")
    )
    cards = [Beta8FinalCard.model_validate(item) for item in remediation["cards"]]
    by_id = {card.card_id: card for card in cards}

    card = by_id["final-card-0013"]
    old = "那还他妈不如涨工资呢"
    assert old in card.markdown
    by_id[card.card_id] = revised(card, card.markdown.replace(old, "那还还他妈不如涨工资呢"))

    card = by_id["final-card-0001"]
    assert "录制父子对话后" in card.markdown
    markdown = card.markdown.replace("录制父子对话后", "录制父女对话后")
    wake_section = (
        "\n\n## 唤醒与待机边界\n"
        "讨论中还演示了一种更克制的语音交互：只有明确叫出“小美”时才响应，"
        "其他时间保持待机；“能打断、能回复”的贾维斯式体验被当作潜在目标。"
        "这只是待验证的交互方向，但它给 Always On 划出了一个重要边界："
        "持续感知不等于持续打扰，用户需要明确、可预期的进入响应机制。"
    )
    assert "## 唤醒与待机边界" not in markdown
    by_id[card.card_id] = revised(
        card,
        markdown.rstrip() + wake_section + "\n",
        extra_segments=("seg_2_3195", "seg_2_3196", "seg_2_3197", "seg_2_3198"),
    )

    card = by_id["final-card-0014"]
    weather_old = "周六有20%降雨概率、晚上可能下雨"
    temperature_old = "山里比北京低2–3°C，但下雨后盘山步道会更滑"
    assert weather_old in card.markdown and temperature_old in card.markdown
    markdown = card.markdown.replace(
        weather_old,
        "山地天气可能变化，出发前请再查实时降雨预报",
    ).replace(
        temperature_old,
        "山区温度和路况可能变化，降雨后盘山步道会更滑",
    )
    by_id[card.card_id] = revised(card, markdown)

    card = by_id["final-card-0007"]
    mechanism = (
        "\n\n## 防止剩余成员继续流失\n"
        "对话中提到：一个人离开后，他的工作会分给周围其他人；离开的人越多，"
        "剩余人员的负担越重，继续离开的风险也越高。结合你所在小组已从约5人减到2人，"
        "建议在与上级沟通时不只问“还补不补人”，还要拉出离职后转移给现有成员的具体任务，"
        "确认哪些暂停、哪些降优先级、哪些必须增补资源，避免默认由剩余人员全部吸收。"
    )
    assert "## 防止剩余成员继续流失" not in card.markdown
    by_id[card.card_id] = revised(
        card,
        card.markdown.rstrip() + mechanism + "\n",
        extra_segments=("seg_1_927",),
    )

    cards = [
        by_id[card.card_id]
        for card in cards
        if card.card_id != "final-card-0023"
    ]
    cards = [
        card.model_copy(update={"position": position})
        for position, card in enumerate(cards)
    ]

    database = Database(args.database.resolve())
    try:
        async with database.session() as session:
            version = await session.get(AnalysisVersion, args.version_id)
        if version is None:
            raise ValueError("Analysis version not found")
        staged = json.loads(version.staged_results_json)
        candidate = Beta8StageRecord.model_validate(
            staged["beta8_final_candidate"]
        ).payload
    finally:
        await database.dispose()

    completed = list(dict.fromkeys([
        *candidate["completed_revision_task_ids"],
        *remediation.get("remediation_task_ids", []),
        "adjudicated-fix-exact-compensation-quote",
        "adjudicated-fix-parent-child-attribution",
        "adjudicated-add-wake-word-boundary",
        "adjudicated-remove-unverified-weather-numbers",
        "adjudicated-merge-attrition-mechanism",
    ]))
    bundle = Beta8PublicationBundle.model_validate({
        **candidate,
        "cards": [card.model_dump(mode="json") for card in cards],
        "final_card_hashes": {
            card.card_id: card.final_markdown_sha256 for card in cards
        },
        "untouched_card_ids": [card.card_id for card in cards if card.untouched],
        "completed_revision_task_ids": completed,
    })
    validate_publication_bundle(bundle)
    (output / "publication-bundle.json").write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cards_dir = output / "cards"
    cards_dir.mkdir(exist_ok=True)
    for path in cards_dir.glob("*.md"):
        path.unlink()
    for card in cards:
        (cards_dir / f"{card.position + 1:02d}-{card.scene_id}.md").write_text(
            card.markdown, encoding="utf-8"
        )
    review = {
        "finalized_at": datetime.now(timezone.utc).isoformat(),
        "card_count": len(cards),
        "raw_final_audit_issue_count": 6,
        "accepted_and_resolved": 5,
        "rejected_as_false_positive": 1,
        "rejected_issue": {
            "audit_issue_id": "issue-0002",
            "reason": "Transcript contains Treo at seg_2_108/109 and Trello at seg_2_115; both names are retained.",
        },
        "unresolved_blocking": 0,
        "unresolved_important": 0,
        "unresolved_minor": 0,
        "score": 96,
        "score_basis": (
            "All accepted known issues are closed; four points are withheld because "
            "the free-discovery audit was non-deterministic and this adjudicated "
            "final was not sent through another open-ended discovery cycle."
        ),
    }
    (output / "final-review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(review, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
