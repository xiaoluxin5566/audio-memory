#!/usr/bin/env python3
"""Remove only failed Beta 8 search packets and rebuild the stage integrity hash."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from audio_memory.analysis.beta8_state import Beta8StageRecord, canonical_hash
from audio_memory.db import Database
from audio_memory.models import AnalysisVersion


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--version-id", required=True)
    args = parser.parse_args()
    database = Database(args.database.resolve())
    try:
        async with database.session() as session:
            version = await session.get(AnalysisVersion, args.version_id)
            if version is None:
                raise ValueError("Analysis version not found")
            staged = json.loads(version.staged_results_json)
            record = Beta8StageRecord.model_validate(staged["beta8_search_packets"])
            payload = dict(record.payload)
            removed = sorted(
                key
                for key, value in payload.items()
                if isinstance(value, dict) and value.get("status") == "failed"
            )
            for key in removed:
                payload.pop(key)
            staged["beta8_search_packets"] = record.model_copy(update={
                "payload": payload,
                "artifact_hash": canonical_hash(payload),
            }).model_dump(mode="json")
            version.staged_results_json = json.dumps(staged, ensure_ascii=False)
            await session.commit()
        print(json.dumps({"removed": removed}, ensure_ascii=False))
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
