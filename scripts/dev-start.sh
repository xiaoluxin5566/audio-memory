#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  printf '启动失败：运行环境不存在，请先安装后端依赖。\n' >&2
  exit 1
fi

export PYTHONPATH="$PROJECT_ROOT/backend/src"
exec "$PYTHON" "$PROJECT_ROOT/scripts/dev_lifecycle.py" start \
  --project-root "$PROJECT_ROOT" --home "${HOME:?HOME is required}"
