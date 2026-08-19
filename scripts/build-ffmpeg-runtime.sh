#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="8.0.1"
SOURCE_URL="https://ffmpeg.org/releases/ffmpeg-${VERSION}.tar.xz"
SOURCE_SHA256="05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41"
OUTPUT="${AUDIO_MEMORY_FFMPEG_RUNTIME_OUTPUT:-$PROJECT_ROOT/vendor/ffmpeg-darwin-arm64}"
CACHE="${AUDIO_MEMORY_FFMPEG_BUILD_CACHE:-$PROJECT_ROOT/.ffmpeg-build-cache}"
ARCHIVE="$CACHE/ffmpeg-${VERSION}.tar.xz"

[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || {
  printf '构建失败：FFmpeg 发布运行组件必须在 macOS Apple Silicon 上构建。\n' >&2
  exit 1
}
for tool in curl shasum tar make clang; do
  command -v "$tool" >/dev/null 2>&1 || { printf '构建失败：缺少 %s。\n' "$tool" >&2; exit 1; }
done

mkdir -p "$CACHE"
if [ ! -f "$ARCHIVE" ]; then
  curl -fL --retry 3 -o "$ARCHIVE.download" "$SOURCE_URL"
  mv "$ARCHIVE.download" "$ARCHIVE"
fi
ACTUAL_SOURCE_SHA="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
[ "$ACTUAL_SOURCE_SHA" = "$SOURCE_SHA256" ] || {
  printf '构建失败：FFmpeg 官方源码包 SHA-256 不匹配。\n' >&2
  exit 1
}

BUILD_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/audio-memory-ffmpeg.XXXXXX")"
cleanup() { rm -rf "$BUILD_PARENT"; }
trap cleanup EXIT INT TERM
tar -C "$BUILD_PARENT" -xf "$ARCHIVE"
SOURCE_ROOT="$BUILD_PARENT/ffmpeg-$VERSION"
PREFIX="$BUILD_PARENT/install"
FLAGS=(
  --prefix="$PREFIX"
  --arch=arm64
  --cc=clang
  --disable-autodetect
  --disable-gpl
  --disable-nonfree
  --disable-doc
  --disable-debug
  --disable-ffplay
  --disable-network
  --enable-static
  --disable-shared
)
(cd "$SOURCE_ROOT" && ./configure "${FLAGS[@]}" && make -j"$(sysctl -n hw.logicalcpu)" ffmpeg ffprobe && make install)

TEMPORARY="${OUTPUT}.build-$$"
rm -rf "$TEMPORARY"
mkdir -p "$TEMPORARY/bin"
cp "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe" "$TEMPORARY/bin/"
chmod 755 "$TEMPORARY/bin/ffmpeg" "$TEMPORARY/bin/ffprobe"
cp "$SOURCE_ROOT/COPYING.LGPLv2.1" "$TEMPORARY/LICENSE.md"

FFMPEG_SHA="$(shasum -a 256 "$TEMPORARY/bin/ffmpeg" | awk '{print $1}')"
FFPROBE_SHA="$(shasum -a 256 "$TEMPORARY/bin/ffprobe" | awk '{print $1}')"
FLAGS_JSON="$(printf '%s\n' "${FLAGS[@]}" | /usr/bin/python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin]))')"
/usr/bin/python3 - "$TEMPORARY/manifest.json" "$FFMPEG_SHA" "$FFPROBE_SHA" "$FLAGS_JSON" <<'PY'
import json
import sys
from pathlib import Path

target, ffmpeg_hash, ffprobe_hash, flags = sys.argv[1:]
Path(target).write_text(json.dumps({
    "schema_version": 1,
    "ffmpeg_version": "8.0.1",
    "platform": "darwin-arm64",
    "source_url": "https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz",
    "source_sha256": "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41",
    "configure_flags": json.loads(flags),
    "binaries": {
        "ffmpeg": {"path": "bin/ffmpeg", "sha256": ffmpeg_hash},
        "ffprobe": {"path": "bin/ffprobe", "sha256": ffprobe_hash},
    },
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

"$PROJECT_ROOT/backend/.venv/bin/python" "$PROJECT_ROOT/scripts/verify-ffmpeg-runtime.py" "$TEMPORARY"
rm -rf "$OUTPUT"
mv "$TEMPORARY" "$OUTPUT"
printf 'FFmpeg %s 发布运行组件已生成：%s\n' "$VERSION" "$OUTPUT"
