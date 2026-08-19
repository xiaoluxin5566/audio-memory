#!/bin/bash
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/backend"
PYTHON_BIN="${BACKEND_ROOT}/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
CONFIG_VALUES="$("$PYTHON_BIN" "$PROJECT_ROOT/scripts/runtime_config.py" doctor-values --project-root "$PROJECT_ROOT" --home "${HOME:?HOME is required}")" || exit $?
IFS=$'\t' read -r PROFILE APP_DATA MODEL_ROOT MODELS_WRITABLE PORT <<EOF
$CONFIG_VALUES
EOF
FAILURES=0

check() {
  label="$1"; shift
  if "$@" >/dev/null 2>&1; then printf '✓ %s\n' "$label"; else printf '✗ %s\n' "$label"; FAILURES=$((FAILURES + 1)); fi
}

health_matches_profile() {
  payload="$(curl --silent --fail --max-time 2 "http://127.0.0.1:${PORT}/api/health")" || return 1
  printf '%s' "$payload" | "$PYTHON_BIN" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
raise SystemExit(
    0
    if isinstance(payload, dict)
    and payload.get("status") == "ok"
    and payload.get("profile") == sys.argv[1]
    else 1
)
' "$PROFILE"
}

printf 'Audio Memory 本地诊断\n\n'
printf '运行配置：profile=%s port=%s data=%s\n' "$PROFILE" "$PORT" "$PROFILE"
printf '系统：%s / %s\n' "$(uname -s)" "$(uname -m)"
printf '磁盘：%s\n' "$(df -h "$PROJECT_ROOT" | awk 'NR==2 {print $4 " 可用"}')"
if [ "${AUDIO_MEMORY_DOCTOR_CORE_ONLY:-0}" != "1" ]; then
  check 'macOS Apple Silicon' sh -c '[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]'
  check 'Python 3.12 / uv 环境' sh -c 'command -v uv >/dev/null && cd "$1/backend" && UV_CACHE_DIR="$1/.uv-cache" uv run --no-sync python -c "import sys; raise SystemExit(sys.version_info[:2] != (3,12))"' _ "$PROJECT_ROOT"
  check 'Node.js 与 npm' sh -c 'command -v node >/dev/null && command -v npm >/dev/null'
  check 'ffmpeg' command -v ffmpeg
  check '前端生产文件' test -f "$PROJECT_ROOT/prototype/dist/client/index.html"
fi
check 'Whisper 模型清单' python3 "$PROJECT_ROOT/scripts/doctor_checks.py" whisper "$APP_DATA" "$MODEL_ROOT"
check '说话人分段模型' python3 "$PROJECT_ROOT/scripts/doctor_checks.py" diarization "$APP_DATA" "$MODEL_ROOT"
check '分析迁移链' python3 "$PROJECT_ROOT/scripts/doctor_checks.py" migrations "$BACKEND_ROOT/migrations/versions"
check '历史重分析恢复' sh -c 'cd "$1" && PYTHONPATH=src "$2" -c "from audio_memory.reanalysis.worker import ReanalysisWorker"' _ "$BACKEND_ROOT" "$PYTHON_BIN"
check '本地会话安全' sh -c 'cd "$1" && PYTHONPATH=src "$2" -c "from audio_memory.security.local_session import LocalSessionSecurity"' _ "$BACKEND_ROOT" "$PYTHON_BIN"
check '固定 Prompt 资源' sh -c '
  test -f "$1/src/audio_memory/prompts/system.md" && test -f "$1/src/audio_memory/prompts/common-scene.md" && test -f "$1/src/audio_memory/prompts/event-map.md" || exit 1
  for scene in todo meeting parenting content growth inspiration; do test -f "$1/src/audio_memory/prompts/defaults/$scene.md" || exit 1; done
' _ "$BACKEND_ROOT"
check '本地数据目录可写' sh -c '[ ! -e "$1" ] || [ -w "$1" ]' _ "$APP_DATA"
if [ "${AUDIO_MEMORY_DOCTOR_CORE_ONLY:-0}" != "1" ]; then
  if [ "$PROFILE" = "production" ]; then
    check '系统钥匙串可访问' security show-keychain-info login.keychain-db
  fi
  check '本地服务健康' health_matches_profile
fi

if [ -f "$APP_DATA/audio-memory.sqlite3" ]; then
  check '本地数据库已迁移至 0014' python3 "$PROJECT_ROOT/scripts/doctor_checks.py" database "$APP_DATA/audio-memory.sqlite3"
  check '历史重分析状态已恢复' python3 "$PROJECT_ROOT/scripts/doctor_checks.py" recovery "$APP_DATA/audio-memory.sqlite3"
  if command -v sqlite3 >/dev/null 2>&1; then
    LAST_ERROR="$(sqlite3 -readonly "$APP_DATA/audio-memory.sqlite3" "select coalesce(last_validation_error_message,'') from provider_metadata where last_validation_error_message is not null order by last_validated_at desc limit 1;" 2>/dev/null || true)"
    if [ -n "$LAST_ERROR" ]; then printf '最近一次模型错误：%s\n' "$LAST_ERROR"; fi
  fi
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then printf '诊断完成：未发现问题。\n'; exit 0; fi
printf '发现 %s 项需要处理。根据上方缺失项修复后，再运行 ./scripts/install.sh 或 ./scripts/start.sh。\n' "$FAILURES"
exit 1
