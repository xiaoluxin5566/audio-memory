#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"
ARCHIVE="${2:-}"
MP3_FIXTURE="/Users/liujinxin/Downloads/08月01日 09-07 Pokee SE-audio.mp3"
AAC_FIXTURE="/Users/liujinxin/Downloads/feishu-minute-obcnu82z4n194o292136j459.aac"
MP3_SHA256="197b90cdf9a1f7b52c15871519a2f8b737f072470ea1984f39658a0582f3c754"
AAC_SHA256="fc776b4532c43f53165d4682f9b4aefecdaffb5f8d94575ed3650a06d5118c17"

if [ -z "$VERSION" ] || [ -z "$ARCHIVE" ]; then
  printf '用法: %s <version> <archive>\n' "$0" >&2
  exit 2
fi
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
[ -f "$ARCHIVE" ] || { printf '封板失败：发布包不存在：%s\n' "$ARCHIVE" >&2; exit 2; }

verify_fixture() {
  local fixture="$1" expected="$2" actual
  [ -f "$fixture" ] || { printf '封板失败：缺少固定音频：%s\n' "$fixture" >&2; exit 2; }
  actual="$(shasum -a 256 "$fixture" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || { printf '封板失败：固定音频哈希已变更：%s\n' "$fixture" >&2; exit 2; }
}
verify_fixture "$MP3_FIXTURE" "$MP3_SHA256"
verify_fixture "$AAC_FIXTURE" "$AAC_SHA256"

ARCHIVE_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
MAIN_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/audio-memory-sealed-smoke.XXXXXX")"
cleanup() { rm -rf "$SMOKE_ROOT"; }
trap cleanup EXIT INT TERM
tar -xzf "$ARCHIVE" -C "$SMOKE_ROOT"
UNPACKED="$SMOKE_ROOT/audio-memory-${VERSION}"
[ -d "$UNPACKED/backend/src/audio_memory" ] || { printf '封板失败：发布包目录与版本不一致。\n' >&2; exit 2; }
EVIDENCE="$SMOKE_ROOT/evidence.json"

cd "$PROJECT_ROOT"
PATH="$UNPACKED/runtime/ffmpeg/bin:$PATH" \
PYTHONPATH="$UNPACKED/backend/src" backend/.venv/bin/python tests/real-pipeline-smoke.py \
  "$MP3_FIXTURE" \
  "$AAC_FIXTURE" \
  --timeout-seconds 3600 \
  --evidence-output "$EVIDENCE" \
  --target-version "$VERSION" \
  --main-commit "$MAIN_COMMIT" \
  --archive-sha256 "$ARCHIVE_SHA256"

backend/.venv/bin/python scripts/feature_governance.py release-smoke \
  "$VERSION" "$ARCHIVE" "$EVIDENCE"
