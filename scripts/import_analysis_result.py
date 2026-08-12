from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from audio_memory.analysis.result_import import import_latest_analysis
from audio_memory.db import Database


async def run(database_path: Path, input_path: Path) -> dict[str, object]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    database = Database(database_path)
    try:
        version_id = await import_latest_analysis(database, payload)
    finally:
        await database.dispose()
    return {
        "version_id": version_id,
        "cards": len(payload.get("cards", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.database, args.input))))


if __name__ == "__main__":
    main()
