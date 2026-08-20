#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RELEASE_ROOT="${AUDIO_MEMORY_RELEASE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DATA_ROOT="${AUDIO_MEMORY_DATA_ROOT:-$HOME/Library/Application Support/AudioMemory}"
APP_ROOT="$DATA_ROOT/app"
VERSIONS_ROOT="$APP_ROOT/versions"
CURRENT_LINK="$APP_ROOT/current"
DATABASE="$DATA_ROOT/audio-memory.sqlite3"
CLI_TARGET="$HOME/.local/bin/audio-memory"
BOOTSTRAP_PYTHON="${AUDIO_MEMORY_BOOTSTRAP_PYTHON:-}"

fail() {
  printf '安装失败：%s\n' "$1" >&2
  exit 1
}

for required in \
  VERSION \
  THIRD_PARTY_NOTICES.md \
  backend/pyproject.toml \
  backend/uv.lock \
  prototype/dist/client/index.html \
  scripts/audio-memory \
  scripts/backup_data.py \
  scripts/com.audio-memory.local.plist.template \
  scripts/doctor.sh \
  scripts/doctor_checks.py \
  scripts/runtime_config.py \
  scripts/start.sh \
  scripts/verify-ffmpeg-runtime.py \
  runtime/ffmpeg/bin/ffmpeg \
  runtime/ffmpeg/bin/ffprobe \
  runtime/ffmpeg/manifest.json \
  runtime/ffmpeg/LICENSE.md \
  runtime/uv/uv; do
  [ -f "$RELEASE_ROOT/$required" ] || fail "发布包缺少 $required"
done

run_bootstrap_python() {
  if [ -n "$BOOTSTRAP_PYTHON" ]; then
    "$BOOTSTRAP_PYTHON" "$@"
  else
    UV_CACHE_DIR="$RELEASE_ROOT/.uv-cache" \
      "$RELEASE_ROOT/runtime/uv/uv" run --no-project --python 3.12 python "$@"
  fi
}

run_bootstrap_python "$RELEASE_ROOT/scripts/verify-ffmpeg-runtime.py" \
  "$RELEASE_ROOT/runtime/ffmpeg"

VERSION="$(tr -d '[:space:]' < "$RELEASE_ROOT/VERSION")"
case "$VERSION" in
  *[!0-9A-Za-z.-]*|'') fail "版本号无效" ;;
esac

mkdir -p "$DATA_ROOT" "$VERSIONS_ROOT" "$DATA_ROOT/backups" "$HOME/.local/bin"
chmod 700 "$DATA_ROOT" "$APP_ROOT" "$VERSIONS_ROOT" "$DATA_ROOT/backups" "$HOME/.local" "$HOME/.local/bin" 2>/dev/null || true

if [ -x "$CURRENT_LINK/scripts/audio-memory" ]; then
  AUDIO_MEMORY_APP_ROOT="$APP_ROOT" AUDIO_MEMORY_DATA_ROOT="$DATA_ROOT" \
    "$CURRENT_LINK/scripts/audio-memory" stop >/dev/null 2>&1 || true
fi

if [ -f "$DATABASE" ]; then
  BACKUP_DIR="$DATA_ROOT/backups/$(date '+%Y%m%d-%H%M%S')-$$"
  mkdir -m 700 "$BACKUP_DIR"
  run_bootstrap_python "$RELEASE_ROOT/scripts/backup_data.py" \
    "$DATABASE" "$BACKUP_DIR/audio-memory.sqlite3"
fi

TARGET="$VERSIONS_ROOT/$VERSION"
TEMPORARY="$VERSIONS_ROOT/.install-$VERSION-$$"
CURRENT_TEMP="$APP_ROOT/.current-$$"
SETUP_MARKER="$TARGET/.release-setup-complete"
cleanup() {
  rm -rf "$TEMPORARY"
  rm -f "$CURRENT_TEMP"
}
trap cleanup EXIT INT TERM

if [ ! -d "$TARGET" ]; then
  mkdir -m 700 "$TEMPORARY"
  cp -R "$RELEASE_ROOT/." "$TEMPORARY/"
  chmod +x "$TEMPORARY/scripts/audio-memory" "$TEMPORARY/scripts/install-release.sh" \
    "$TEMPORARY/scripts/start.sh" "$TEMPORARY/scripts/doctor.sh" \
    "$TEMPORARY/scripts/verify-ffmpeg-runtime.py" \
    "$TEMPORARY/runtime/ffmpeg/bin/ffmpeg" "$TEMPORARY/runtime/ffmpeg/bin/ffprobe" \
    "$TEMPORARY/runtime/uv/uv"
  mv "$TEMPORARY" "$TARGET"
fi

[ "$(tr -d '[:space:]' < "$TARGET/VERSION")" = "$VERSION" ] || fail "已安装版本校验失败"
if [ "${AUDIO_MEMORY_SKIP_RELEASE_SETUP:-0}" != "1" ] && [ ! -f "$SETUP_MARKER" ]; then
  AUDIO_MEMORY_PREBUILT=1 "$TARGET/scripts/install.sh" || \
    fail "版本运行环境准备失败，当前版本未切换；重新安装会从安全断点继续"
  SETUP_TEMP="$TARGET/.release-setup-complete.$$"
  printf '%s\n' "$VERSION" > "$SETUP_TEMP"
  chmod 600 "$SETUP_TEMP"
  mv "$SETUP_TEMP" "$SETUP_MARKER"
fi
ln -s "$TARGET" "$CURRENT_TEMP"
mv -h -f "$CURRENT_TEMP" "$CURRENT_LINK"
ln -sfn "$CURRENT_LINK/scripts/audio-memory" "$CLI_TARGET"

trap - EXIT INT TERM
printf 'Audio Memory %s 已安装。\n' "$VERSION"
printf '用户数据保留在：%s\n' "$DATA_ROOT"
printf '如果终端找不到 audio-memory，请运行：export PATH="$HOME/.local/bin:$PATH"\n'
