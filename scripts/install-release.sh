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
SHARED_UV_CACHE="$APP_ROOT/.uv-cache"
BOOTSTRAP_UV_CACHE="$RELEASE_ROOT/.uv-cache"
if [ -d "$SHARED_UV_CACHE" ]; then
  BOOTSTRAP_UV_CACHE="$SHARED_UV_CACHE"
elif [ -d "$CURRENT_LINK/.uv-cache" ]; then
  BOOTSTRAP_UV_CACHE="$CURRENT_LINK/.uv-cache"
fi

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
    UV_CACHE_DIR="$BOOTSTRAP_UV_CACHE" \
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
INSTALL_LOCK="$APP_ROOT/.install.lock"
install_lock_owner_is_active() {
  owner="$1"
  case "$owner" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$owner" 2>/dev/null
}
acquire_install_lock() {
  attempt=1
  while [ "$attempt" -le 3 ]; do
    if run_bootstrap_python -c \
      'import os, sys; os.symlink(sys.argv[1], sys.argv[2])' \
      "$$" "$INSTALL_LOCK" >/dev/null 2>&1; then
      return 0
    fi
    [ -L "$INSTALL_LOCK" ] || fail "安装锁状态异常，请检查 $INSTALL_LOCK"
    observed_owner="$(readlink "$INSTALL_LOCK" 2>/dev/null || true)"
    if install_lock_owner_is_active "$observed_owner"; then
      fail "另一个安装任务正在进行，请稍后重试"
    fi
    stale_lock="$APP_ROOT/.install.lock.stale.$$.$attempt"
    if mv "$INSTALL_LOCK" "$stale_lock" 2>/dev/null; then
      moved_owner="$(readlink "$stale_lock" 2>/dev/null || true)"
      if [ "$moved_owner" != "$observed_owner" ]; then
        mv "$stale_lock" "$INSTALL_LOCK" 2>/dev/null || true
        fail "安装锁在检查期间已变更，请重试"
      fi
      rm -f "$stale_lock"
    fi
    attempt=$((attempt + 1))
  done
  fail "无法获取安装锁，请稍后重试"
}
acquire_install_lock
release_install_lock() {
  if [ -L "$INSTALL_LOCK" ] && \
    [ "$(readlink "$INSTALL_LOCK" 2>/dev/null || true)" = "$$" ]; then
    rm -f "$INSTALL_LOCK"
  fi
}
trap release_install_lock EXIT INT TERM

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
  release_install_lock
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
  if [ ! -d "$SHARED_UV_CACHE" ]; then
    if [ -d "$CURRENT_LINK/.uv-cache" ]; then
      mv "$CURRENT_LINK/.uv-cache" "$SHARED_UV_CACHE"
    else
      mkdir -m 700 "$SHARED_UV_CACHE"
    fi
  fi
  AUDIO_MEMORY_PREBUILT=1 AUDIO_MEMORY_UV_CACHE_DIR="$SHARED_UV_CACHE" \
    "$TARGET/scripts/install.sh" || \
    fail "版本运行环境准备失败，当前版本未切换；重新安装会从安全断点继续"
  SETUP_TEMP="$TARGET/.release-setup-complete.$$"
  printf '%s\n' "$VERSION" > "$SETUP_TEMP"
  chmod 600 "$SETUP_TEMP"
  mv "$SETUP_TEMP" "$SETUP_MARKER"
fi
ln -s "$TARGET" "$CURRENT_TEMP"
mv -h -f "$CURRENT_TEMP" "$CURRENT_LINK"
ln -sfn "$CURRENT_LINK/scripts/audio-memory" "$CLI_TARGET"

release_install_lock
trap - EXIT INT TERM
printf 'Audio Memory %s 已安装。\n' "$VERSION"
printf '用户数据保留在：%s\n' "$DATA_ROOT"
printf '如果终端找不到 audio-memory，请运行：export PATH="$HOME/.local/bin:$PATH"\n'
