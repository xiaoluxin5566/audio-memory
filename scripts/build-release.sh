#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")"
PACKAGE_NAME="audio-memory-v${VERSION}-macos-arm64"
ARCHIVE_ROOT="audio-memory-v${VERSION}"
DIST_ROOT="${AUDIO_MEMORY_RELEASE_DIST:-$PROJECT_ROOT/dist}"
FFMPEG_RUNTIME="${AUDIO_MEMORY_FFMPEG_RUNTIME:-$PROJECT_ROOT/vendor/ffmpeg-darwin-arm64}"
UV_BINARY="${AUDIO_MEMORY_UV_BINARY:-$(command -v uv 2>/dev/null || true)}"

if [ "${AUDIO_MEMORY_ALLOW_DIRTY_RELEASE:-0}" != "1" ] && [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
  printf '发布失败：候选工作树存在未提交改动。\n' >&2
  exit 1
fi

if [ "${AUDIO_MEMORY_SKIP_RELEASE_BUILD:-0}" != "1" ]; then
  (cd "$PROJECT_ROOT/prototype" && npm run build)
fi
[ -f "$PROJECT_ROOT/prototype/dist/client/index.html" ] || {
  printf '发布失败：缺少前端生产文件。\n' >&2
  exit 1
}
"${AUDIO_MEMORY_BUILD_PYTHON:-python3}" \
  "$PROJECT_ROOT/scripts/verify-ffmpeg-runtime.py" "$FFMPEG_RUNTIME"
[ -n "$UV_BINARY" ] && [ -x "$UV_BINARY" ] || {
  printf '发布失败：缺少可随包的 uv 可执行文件。\n' >&2
  exit 1
}
"$UV_BINARY" --version >/dev/null
if [ "${AUDIO_MEMORY_SKIP_FFMPEG_ARCH_CHECK:-0}" != "1" ]; then
  /usr/bin/file "$UV_BINARY" | grep -q arm64 || {
    printf '发布失败：uv 不是 Apple Silicon 可执行文件。\n' >&2
    exit 1
  }
fi

STAGING_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/audio-memory-release.XXXXXX")"
STAGING="$STAGING_PARENT/$ARCHIVE_ROOT"
cleanup() { rm -rf "$STAGING_PARENT"; }
trap cleanup EXIT INT TERM

mkdir -p \
  "$STAGING/backend" \
  "$STAGING/prototype/dist" \
  "$STAGING/runtime" \
  "$STAGING/scripts"

cp "$PROJECT_ROOT/VERSION" "$STAGING/VERSION"
cp "$PROJECT_ROOT/README.md" "$STAGING/README.md"
cp "$PROJECT_ROOT/CHANGELOG.md" "$STAGING/CHANGELOG.md"
cp "$PROJECT_ROOT/PRIVACY.md" "$STAGING/PRIVACY.md"
cp "$PROJECT_ROOT/THIRD_PARTY_NOTICES.md" "$STAGING/THIRD_PARTY_NOTICES.md"
cp "$PROJECT_ROOT/backend/pyproject.toml" "$STAGING/backend/pyproject.toml"
cp "$PROJECT_ROOT/backend/uv.lock" "$STAGING/backend/uv.lock"
cp "$PROJECT_ROOT/backend/alembic.ini" "$STAGING/backend/alembic.ini"
cp -R "$PROJECT_ROOT/backend/src" "$STAGING/backend/src"
cp -R "$PROJECT_ROOT/backend/migrations" "$STAGING/backend/migrations"
cp -R "$PROJECT_ROOT/prototype/dist/client" "$STAGING/prototype/dist/client"
cp -R "$FFMPEG_RUNTIME" "$STAGING/runtime/ffmpeg"
mkdir -p "$STAGING/runtime/uv"
cp "$UV_BINARY" "$STAGING/runtime/uv/uv"

for script in \
  audio-memory \
  backup_data.py \
  build-release.sh \
  build-ffmpeg-runtime.sh \
  com.audio-memory.local.plist.template \
  doctor.sh \
  doctor_checks.py \
  install-release.sh \
  install.sh \
  runtime_config.py \
  start.sh; do
  cp "$PROJECT_ROOT/scripts/$script" "$STAGING/scripts/$script"
done
cp "$PROJECT_ROOT/scripts/verify-ffmpeg-runtime.py" "$STAGING/scripts/verify-ffmpeg-runtime.py"
find "$STAGING" -type d \( \
  -iname .git -o \
  -iname .venv -o \
  -iname .runtime -o \
  -iname .uv-cache -o \
  -iname .pytest_cache -o \
  -iname .mypy_cache -o \
  -iname .ruff_cache -o \
  -iname '.env*' -o \
  -iname '*.egg-info' -o \
  -iname node_modules -o \
  -iname tests -o \
  -iname outputs -o \
  -iname screenshots -o \
  -iname designs -o \
  -iname models -o \
  -iname audio -o \
  -iname build -o \
  -iname __pycache__ \
\) -prune -exec rm -rf {} +
find "$STAGING" -type f \( \
  -iname '.env*' -o \
  -iname '*.pyc' -o \
  -iname '*.pyo' -o \
  -iname '*.sqlite' -o \
  -iname '*.sqlite3' -o \
  -iname '*.sqlite-wal' -o \
  -iname '*.sqlite-shm' -o \
  -iname '*.sqlite-journal' -o \
  -iname '*.sqlite3-wal' -o \
  -iname '*.sqlite3-shm' -o \
  -iname '*.sqlite3-journal' -o \
  -iname '*.db' -o \
  -iname '*.db-wal' -o \
  -iname '*.db-shm' -o \
  -iname '*.db-journal' -o \
  -iname '*.mp3' -o \
  -iname '*.aac' -o \
  -iname '*.m4a' -o \
  -iname '*.wav' -o \
  -iname '*.flac' -o \
  -iname '*.ogg' -o \
  -iname '*.opus' -o \
  -iname '*.wma' -o \
  -iname '*.caf' -o \
  -iname '*.aiff' -o \
  -iname '*.log' -o \
  -iname '*.log.*' \
\) -delete
find "$STAGING" -type l -delete
chmod +x "$STAGING/scripts/audio-memory" "$STAGING/scripts/backup_data.py" \
  "$STAGING/scripts/build-release.sh" "$STAGING/scripts/install-release.sh" \
  "$STAGING/scripts/build-ffmpeg-runtime.sh" \
  "$STAGING/scripts/install.sh" "$STAGING/scripts/start.sh" "$STAGING/scripts/doctor.sh" \
  "$STAGING/scripts/verify-ffmpeg-runtime.py" "$STAGING/runtime/ffmpeg/bin/ffmpeg" \
  "$STAGING/runtime/ffmpeg/bin/ffprobe" "$STAGING/runtime/uv/uv"

mkdir -p "$DIST_ROOT"
ARCHIVE="$DIST_ROOT/$PACKAGE_NAME.tar.gz"
COPYFILE_DISABLE=1 tar -C "$STAGING_PARENT" -czf "$ARCHIVE" "$ARCHIVE_ROOT"
(cd "$DIST_ROOT" && shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
printf '%s\n' "$ARCHIVE"
