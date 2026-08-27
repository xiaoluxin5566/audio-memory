#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MP3_FIXTURE="/Users/liujinxin/Downloads/08月01日 09-07 Pokee SE-audio.mp3"
AAC_FIXTURE="/Users/liujinxin/Downloads/feishu-minute-obcnu82z4n194o292136j459.aac"

for fixture in "$MP3_FIXTURE" "$AAC_FIXTURE"; do
  if [ ! -f "$fixture" ]; then
    printf '发版门禁缺少固定音频：%s\n' "$fixture" >&2
    exit 2
  fi
done

cd "$PROJECT_ROOT"
PYTHONPATH=backend/src backend/.venv/bin/python tests/real-pipeline-smoke.py \
  "$MP3_FIXTURE" \
  "$AAC_FIXTURE" \
  --timeout-seconds 3600
