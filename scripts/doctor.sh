#!/bin/bash
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${AUDIO_MEMORY_PORT:-8765}"
APP_DATA="${HOME}/Library/Application Support/AudioMemory"
FAILURES=0

check() {
  label="$1"; shift
  if "$@" >/dev/null 2>&1; then printf '✓ %s\n' "$label"; else printf '✗ %s\n' "$label"; FAILURES=$((FAILURES + 1)); fi
}

printf 'Audio Memory 本地诊断\n\n'
printf '系统：%s / %s\n' "$(uname -s)" "$(uname -m)"
printf '磁盘：%s\n' "$(df -h "$PROJECT_ROOT" | awk 'NR==2 {print $4 " 可用"}')"
check 'macOS Apple Silicon' sh -c '[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]'
check 'Python 3.12 / uv 环境' sh -c 'command -v uv >/dev/null && cd "$1/backend" && UV_CACHE_DIR="$1/.uv-cache" uv run --no-sync python -c "import sys; raise SystemExit(sys.version_info[:2] != (3,12))"' _ "$PROJECT_ROOT"
check 'Node.js 与 npm' sh -c 'command -v node >/dev/null && command -v npm >/dev/null'
check 'ffmpeg' command -v ffmpeg
check '前端生产文件' test -f "$PROJECT_ROOT/prototype/dist/client/index.html"
check 'Whisper 模型清单' test -f "$APP_DATA/whisper-model-manifest.json"
check '本地数据目录可写' sh -c '[ ! -e "$1" ] || [ -w "$1" ]' _ "$APP_DATA"
check '系统钥匙串可访问' security show-keychain-info login.keychain-db
check '本地服务健康' curl --silent --fail --max-time 2 "http://127.0.0.1:${PORT}/api/health"

if [ -f "$APP_DATA/audio-memory.sqlite3" ] && command -v sqlite3 >/dev/null 2>&1; then
  LAST_ERROR="$(sqlite3 -readonly "$APP_DATA/audio-memory.sqlite3" "select coalesce(last_validation_error_message,'') from provider_metadata where last_validation_error_message is not null order by last_validated_at desc limit 1;" 2>/dev/null || true)"
  if [ -n "$LAST_ERROR" ]; then printf '最近一次模型错误：%s\n' "$LAST_ERROR"; fi
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then printf '诊断完成：未发现问题。\n'; exit 0; fi
printf '发现 %s 项需要处理。根据上方缺失项修复后，再运行 ./scripts/install.sh 或 ./scripts/start.sh。\n' "$FAILURES"
exit 1
