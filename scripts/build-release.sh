#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")"
PACKAGE_NAME="audio-memory-v${VERSION}-macos-arm64"
ARCHIVE_ROOT="audio-memory-v${VERSION}"
DIST_ROOT="${AUDIO_MEMORY_RELEASE_DIST:-$PROJECT_ROOT/dist}"

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

STAGING_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/audio-memory-release.XXXXXX")"
STAGING="$STAGING_PARENT/$ARCHIVE_ROOT"
cleanup() { rm -rf "$STAGING_PARENT"; }
trap cleanup EXIT INT TERM

mkdir -p \
  "$STAGING/backend" \
  "$STAGING/prototype/dist" \
  "$STAGING/scripts"

cp "$PROJECT_ROOT/VERSION" "$STAGING/VERSION"
cp "$PROJECT_ROOT/README.md" "$STAGING/README.md"
cp "$PROJECT_ROOT/CHANGELOG.md" "$STAGING/CHANGELOG.md"
cp "$PROJECT_ROOT/PRIVACY.md" "$STAGING/PRIVACY.md"
cp "$PROJECT_ROOT/backend/pyproject.toml" "$STAGING/backend/pyproject.toml"
cp "$PROJECT_ROOT/backend/uv.lock" "$STAGING/backend/uv.lock"
cp "$PROJECT_ROOT/backend/alembic.ini" "$STAGING/backend/alembic.ini"
cp -R "$PROJECT_ROOT/backend/src" "$STAGING/backend/src"
cp -R "$PROJECT_ROOT/backend/migrations" "$STAGING/backend/migrations"
find "$STAGING/backend" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGING/backend" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
cp -R "$PROJECT_ROOT/prototype/dist/client" "$STAGING/prototype/dist/client"

for script in \
  audio-memory \
  backup_data.py \
  build-release.sh \
  com.audio-memory.local.plist.template \
  doctor.sh \
  doctor_checks.py \
  install-release.sh \
  install.sh \
  start.sh; do
  cp "$PROJECT_ROOT/scripts/$script" "$STAGING/scripts/$script"
done
chmod +x "$STAGING/scripts/audio-memory" "$STAGING/scripts/backup_data.py" \
  "$STAGING/scripts/build-release.sh" "$STAGING/scripts/install-release.sh" \
  "$STAGING/scripts/install.sh" "$STAGING/scripts/start.sh" "$STAGING/scripts/doctor.sh"

mkdir -p "$DIST_ROOT"
ARCHIVE="$DIST_ROOT/$PACKAGE_NAME.tar.gz"
COPYFILE_DISABLE=1 tar -C "$STAGING_PARENT" -czf "$ARCHIVE" "$ARCHIVE_ROOT"
(cd "$DIST_ROOT" && shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
printf '%s\n' "$ARCHIVE"
