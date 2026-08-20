#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  printf '读取集成验收环境失败：运行环境不存在。\n' >&2
  exit 1
fi

exec "$PYTHON" "$PROJECT_ROOT/scripts/integration_runtime.py" status
