#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/backend"
BACKEND_SOURCE="$BACKEND_ROOT/src"
PYTHON="$BACKEND_ROOT/.venv/bin/python"
CONFIG_HELPER="$PROJECT_ROOT/scripts/runtime_config.py"

if [ ! -x "$PYTHON" ]; then
  printf '启动失败：运行环境不存在，请先安装后端依赖。\n' >&2
  exit 1
fi

decode_assignment() {
  "$PYTHON" -c '
import shlex
import sys

name, line = sys.argv[1:]
parts = shlex.split(line, posix=True)
prefix = name + "="
if len(parts) != 1 or not parts[0].startswith(prefix):
    raise SystemExit(2)
print(parts[0][len(prefix):], end="")
' "$1" "$2"
}

load_development_config() {
  local output assignment name value
  local seen_profile=0 seen_data=0 seen_model=0 seen_writable=0 seen_service=0 seen_port=0
  local seen_runtime=0 seen_pid=0 seen_log=0
  output="$("$PYTHON" "$CONFIG_HELPER" development-env \
    --project-root "$PROJECT_ROOT" --home "${HOME:?HOME is required}")" || return $?

  while IFS= read -r assignment || [ -n "$assignment" ]; do
    [ -n "$assignment" ] || { printf '启动失败：运行配置输出无效。\n' >&2; return 2; }
    name="${assignment%%=*}"
    case "$name" in
      AUDIO_MEMORY_PROFILE|AUDIO_MEMORY_DATA_ROOT|AUDIO_MEMORY_MODEL_ROOT|AUDIO_MEMORY_MODELS_WRITABLE|AUDIO_MEMORY_KEYCHAIN_SERVICE|AUDIO_MEMORY_PORT|AUDIO_MEMORY_RUNTIME_DIR|AUDIO_MEMORY_PID_FILE|AUDIO_MEMORY_LOG_FILE) ;;
      *) printf '启动失败：运行配置包含未允许的字段。\n' >&2; return 2 ;;
    esac
    value="$(decode_assignment "$name" "$assignment")" || {
      printf '启动失败：运行配置输出无法安全解析。\n' >&2
      return 2
    }
    case "$name" in
      AUDIO_MEMORY_PROFILE) [ "$seen_profile" -eq 0 ] || return 2; AUDIO_MEMORY_PROFILE="$value"; seen_profile=1 ;;
      AUDIO_MEMORY_DATA_ROOT) [ "$seen_data" -eq 0 ] || return 2; AUDIO_MEMORY_DATA_ROOT="$value"; seen_data=1 ;;
      AUDIO_MEMORY_MODEL_ROOT) [ "$seen_model" -eq 0 ] || return 2; AUDIO_MEMORY_MODEL_ROOT="$value"; seen_model=1 ;;
      AUDIO_MEMORY_MODELS_WRITABLE) [ "$seen_writable" -eq 0 ] || return 2; AUDIO_MEMORY_MODELS_WRITABLE="$value"; seen_writable=1 ;;
      AUDIO_MEMORY_KEYCHAIN_SERVICE) [ "$seen_service" -eq 0 ] || return 2; AUDIO_MEMORY_KEYCHAIN_SERVICE="$value"; seen_service=1 ;;
      AUDIO_MEMORY_PORT) [ "$seen_port" -eq 0 ] || return 2; AUDIO_MEMORY_PORT="$value"; seen_port=1 ;;
      AUDIO_MEMORY_RUNTIME_DIR) [ "$seen_runtime" -eq 0 ] || return 2; AUDIO_MEMORY_RUNTIME_DIR="$value"; seen_runtime=1 ;;
      AUDIO_MEMORY_PID_FILE) [ "$seen_pid" -eq 0 ] || return 2; AUDIO_MEMORY_PID_FILE="$value"; seen_pid=1 ;;
      AUDIO_MEMORY_LOG_FILE) [ "$seen_log" -eq 0 ] || return 2; AUDIO_MEMORY_LOG_FILE="$value"; seen_log=1 ;;
    esac
  done <<< "$output"

  if [ "$seen_profile$seen_data$seen_model$seen_writable$seen_service$seen_port$seen_runtime$seen_pid$seen_log" != "111111111" ]; then
    printf '启动失败：运行配置字段不完整。\n' >&2
    return 2
  fi
  case "$AUDIO_MEMORY_MODELS_WRITABLE" in
    0) unset AUDIO_MEMORY_MODEL_ROOT ;;
    1) export AUDIO_MEMORY_MODEL_ROOT ;;
    *) printf '启动失败：模型目录可写标记无效。\n' >&2; return 2 ;;
  esac
  export AUDIO_MEMORY_PROFILE AUDIO_MEMORY_DATA_ROOT
  export AUDIO_MEMORY_KEYCHAIN_SERVICE AUDIO_MEMORY_PORT
}

health_is_development() {
  "$PYTHON" -c '
import json
import sys
try:
    payload = json.loads(sys.argv[1])
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
raise SystemExit(0 if payload.get("status") == "ok" and payload.get("profile") == "development" else 1)
' "$1"
}

load_development_config

URL="http://127.0.0.1:${AUDIO_MEMORY_PORT}/"
HEALTH="http://127.0.0.1:${AUDIO_MEMORY_PORT}/api/health"
if HEALTH_RESPONSE="$(curl --silent --fail --max-time 2 "$HEALTH" 2>/dev/null)"; then
  if health_is_development "$HEALTH_RESPONSE"; then
    printf 'Audio Memory 开发环境已在运行：%s\n' "$URL"
    exit 0
  fi
  printf '启动失败：端口 %s 上的服务不是 Audio Memory 开发环境。\n' "$AUDIO_MEMORY_PORT" >&2
  exit 1
fi

if lsof -nP -iTCP:"$AUDIO_MEMORY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  printf '启动失败：端口 %s 已被其他程序占用。\n' "$AUDIO_MEMORY_PORT" >&2
  exit 1
fi

umask 077
mkdir -p "$AUDIO_MEMORY_RUNTIME_DIR"
chmod 700 "$AUDIO_MEMORY_RUNTIME_DIR"

SERVER_PID=""
SERVER_RUNNING=0
cleanup() {
  if [ "$SERVER_RUNNING" -eq 1 ] && [ -n "$SERVER_PID" ]; then
    kill -TERM "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  if [ -f "$AUDIO_MEMORY_PID_FILE" ] && [ "$(sed -n '1p' "$AUDIO_MEMORY_PID_FILE")" = "$SERVER_PID" ]; then
    rm -f "$AUDIO_MEMORY_PID_FILE"
  fi
}
trap cleanup INT TERM EXIT

cd "$BACKEND_ROOT"
env PYTHONPATH="$BACKEND_SOURCE" "$PYTHON" -m uvicorn audio_memory.main:app \
  --app-dir "$BACKEND_SOURCE" --host 127.0.0.1 --port "$AUDIO_MEMORY_PORT" \
  >>"$AUDIO_MEMORY_LOG_FILE" 2>&1 &
SERVER_PID=$!
SERVER_RUNNING=1
PID_TEMP="$AUDIO_MEMORY_PID_FILE.tmp.$$"
printf '%s\n' "$SERVER_PID" > "$PID_TEMP"
chmod 600 "$PID_TEMP"
mv "$PID_TEMP" "$AUDIO_MEMORY_PID_FILE"

for _ in $(seq 1 60); do
  if HEALTH_RESPONSE="$(curl --silent --fail --max-time 1 "$HEALTH" 2>/dev/null)"; then
    if health_is_development "$HEALTH_RESPONSE"; then
      printf 'Audio Memory 开发环境已启动：%s\n' "$URL"
      set +e
      wait "$SERVER_PID"
      STATUS=$?
      set -e
      SERVER_RUNNING=0
      cleanup
      trap - INT TERM EXIT
      exit "$STATUS"
    fi
    printf '启动失败：健康检查返回了错误的运行环境。\n' >&2
    exit 1
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    set +e
    wait "$SERVER_PID"
    STATUS=$?
    set -e
    SERVER_RUNNING=0
    cleanup
    trap - INT TERM EXIT
    exit "$STATUS"
  fi
  sleep 0.5
done

printf '启动失败：开发服务在 30 秒内未就绪，请检查 %s。\n' "$AUDIO_MEMORY_LOG_FILE" >&2
exit 1
