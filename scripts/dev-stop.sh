#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/backend"
BACKEND_SOURCE="$BACKEND_ROOT/src"
PYTHON="$BACKEND_ROOT/.venv/bin/python"
CONFIG_HELPER="$PROJECT_ROOT/scripts/runtime_config.py"

if [ ! -x "$PYTHON" ]; then
  printf '停止失败：运行环境不存在。\n' >&2
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
    [ -n "$assignment" ] || { printf '停止失败：运行配置输出无效。\n' >&2; return 2; }
    name="${assignment%%=*}"
    case "$name" in
      AUDIO_MEMORY_PROFILE|AUDIO_MEMORY_DATA_ROOT|AUDIO_MEMORY_MODEL_ROOT|AUDIO_MEMORY_MODELS_WRITABLE|AUDIO_MEMORY_KEYCHAIN_SERVICE|AUDIO_MEMORY_PORT|AUDIO_MEMORY_RUNTIME_DIR|AUDIO_MEMORY_PID_FILE|AUDIO_MEMORY_LOG_FILE) ;;
      *) printf '停止失败：运行配置包含未允许的字段。\n' >&2; return 2 ;;
    esac
    value="$(decode_assignment "$name" "$assignment")" || {
      printf '停止失败：运行配置输出无法安全解析。\n' >&2
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
    printf '停止失败：运行配置字段不完整。\n' >&2
    return 2
  fi
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

discard_stale_pid() {
  rm -f "$AUDIO_MEMORY_PID_FILE"
  printf 'Audio Memory 开发环境未运行；已清理过期的 PID 记录。\n' >&2
  exit 1
}

load_development_config

if [ ! -f "$AUDIO_MEMORY_PID_FILE" ]; then
  printf 'Audio Memory 开发环境未运行。\n'
  exit 0
fi

PID="$(<"$AUDIO_MEMORY_PID_FILE")"
if ! [[ "$PID" =~ ^[1-9][0-9]*$ ]]; then
  discard_stale_pid
fi

if ! PROCESS_COMMAND="$(ps -p "$PID" -o command= 2>/dev/null)" || [ -z "$PROCESS_COMMAND" ]; then
  discard_stale_pid
fi

case "$PROCESS_COMMAND" in
  *"-m uvicorn"*) ;;
  *) discard_stale_pid ;;
esac
case "$PROCESS_COMMAND" in
  *"audio_memory.main:app"*) ;;
  *) discard_stale_pid ;;
esac
case "$PROCESS_COMMAND" in
  *"--app-dir $BACKEND_SOURCE"*) ;;
  *) discard_stale_pid ;;
esac
case "$PROCESS_COMMAND" in
  *"--port $AUDIO_MEMORY_PORT"*) ;;
  *) discard_stale_pid ;;
esac

HEALTH="http://127.0.0.1:${AUDIO_MEMORY_PORT}/api/health"
if ! HEALTH_RESPONSE="$(curl --silent --fail --max-time 2 "$HEALTH" 2>/dev/null)"; then
  discard_stale_pid
fi
if ! health_is_development "$HEALTH_RESPONSE"; then
  discard_stale_pid
fi

KILL_COMMAND="$(type -P kill || true)"
if [ -z "$KILL_COMMAND" ]; then
  KILL_COMMAND="/bin/kill"
fi
if ! "$KILL_COMMAND" -TERM -- "$PID"; then
  printf '停止失败：无法向已验证的开发进程发送 TERM。\n' >&2
  exit 1
fi

rm -f "$AUDIO_MEMORY_PID_FILE"
printf 'Audio Memory 开发环境已停止。\n'
