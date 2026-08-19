#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"

[ -x "$PYTHON" ] || {
  printf '版本发布失败：运行环境不存在。\n' >&2
  exit 1
}

export AUDIO_MEMORY_TOOLCHAIN_ROOT="$PROJECT_ROOT"
exec "$PYTHON" "$PROJECT_ROOT/scripts/feature_governance.py" release-build "$@"
