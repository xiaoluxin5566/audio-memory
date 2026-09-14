from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from audio_memory.analysis.publisher import VersionPublisher
from audio_memory.db import Database
from audio_memory.prompts.beta8_pipeline_schema import Beta8PublicationBundle


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--worker-owner-id", required=True)
    args = parser.parse_args()

    database = Database(args.database.resolve())
    try:
        bundle = Beta8PublicationBundle.model_validate_json(
            args.bundle.read_text(encoding="utf-8")
        )
        outcome = await VersionPublisher(database).publish_beta8(
            args.version_id,
            bundle,
            worker_owner_id=args.worker_owner_id,
        )
        print(json.dumps({
            "batch_id": outcome.batch_id,
            "card_count": outcome.card_count,
            "todo_count": outcome.todo_count,
        }, ensure_ascii=False))
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
