#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLCHAIN_ROOT="${AUDIO_MEMORY_TOOLCHAIN_ROOT:-$PROJECT_ROOT}"
PYTHON="$TOOLCHAIN_ROOT/backend/.venv/bin/python"
COMMON_DIR="$(git rev-parse --git-common-dir)"

if [ ! -x "$PYTHON" ]; then
  printf '停止功能运行时失败：运行环境不存在。\n' >&2
  exit 1
fi

exec "$PYTHON" "$PROJECT_ROOT/scripts/feature_runtime.py" stop \
  --feature-id "${1:?feature_id is required}" \
  --git-common-dir "$COMMON_DIR"
