#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"
PORT="${AUDIO_MEMORY_PORT:-8765}"
URL="http://127.0.0.1:${PORT}/"
HEALTH="http://127.0.0.1:${PORT}/api/health"
FFMPEG_BIN="$PROJECT_ROOT/runtime/ffmpeg/bin"

if curl --silent --fail --max-time 2 "$HEALTH" >/dev/null 2>&1; then
  printf 'Audio Memory 已在运行：%s\n' "$URL"
  [ "${AUDIO_MEMORY_NO_OPEN:-0}" = "1" ] || open "$URL"
  exit 0
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  printf '启动失败：端口 %s 已被其他程序占用。\n' "$PORT" >&2
  printf '可运行 AUDIO_MEMORY_PORT=8766 ./scripts/start.sh 使用其他端口。\n' >&2
  exit 1
fi

if [ ! -x "$PYTHON" ]; then
  printf '启动失败：运行环境不存在，请重新运行安装程序。\n' >&2
  exit 1
fi
if [ ! -x "$FFMPEG_BIN/ffmpeg" ] || [ ! -x "$FFMPEG_BIN/ffprobe" ]; then
  printf '启动失败：随包音频运行组件不完整，请重新安装。\n' >&2
  exit 1
fi

cd "$PROJECT_ROOT/backend"
env \
  AUDIO_MEMORY_RELEASE_MODE=1 \
  AUDIO_MEMORY_RELEASE_ROOT="$PROJECT_ROOT" \
  AUDIO_MEMORY_FFMPEG="$PROJECT_ROOT/runtime/ffmpeg/bin/ffmpeg" \
  AUDIO_MEMORY_FFPROBE="$PROJECT_ROOT/runtime/ffmpeg/bin/ffprobe" \
  PATH="$PROJECT_ROOT/runtime/ffmpeg/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONPATH="$PROJECT_ROOT/backend/src" \
  "$PYTHON" -m uvicorn audio_memory.main:app \
  --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" >/dev/null 2>&1 || true; }
trap cleanup INT TERM EXIT

for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 1 "$HEALTH" >/dev/null 2>&1; then
    printf 'Audio Memory 已启动：%s\n' "$URL"
    [ "${AUDIO_MEMORY_NO_OPEN:-0}" = "1" ] || open "$URL"
    wait "$SERVER_PID"
    exit $?
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.5
done

printf '启动失败：本地服务在 30 秒内未就绪。请运行 ./scripts/doctor.sh。\n' >&2
exit 1
