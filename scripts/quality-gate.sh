#!/bin/bash
set -euo pipefail

FEATURE_ROOT="$(pwd -P)"
TOOLCHAIN_ROOT="${AUDIO_MEMORY_TOOLCHAIN_ROOT:-$FEATURE_ROOT}"
PYTEST="$TOOLCHAIN_ROOT/backend/.venv/bin/pytest"
NODE="$(command -v node)"
NPM="$(command -v npm)"
PLAYWRIGHT="$TOOLCHAIN_ROOT/prototype/node_modules/.bin/playwright"
FRONTEND_PATH="$TOOLCHAIN_ROOT/prototype/node_modules/.bin:$PATH"

for required in "$PYTEST" "$NODE" "$NPM" "$PLAYWRIGHT"; do
  [ -x "$required" ] || {
    printf '功能门禁失败：缺少可执行工具 %s\n' "$required" >&2
    exit 1
  }
done

(cd "$FEATURE_ROOT/backend" && "$PYTEST" -q)
(cd "$FEATURE_ROOT/prototype" && "$NODE" --test tests/*.test.mjs)
(cd "$FEATURE_ROOT/prototype" && env PATH="$FRONTEND_PATH" "$NPM" run build)
(cd "$FEATURE_ROOT/prototype" && "$PLAYWRIGHT" test)
(cd "$FEATURE_ROOT/backend" && "$PYTEST" -q tests/unit/test_feature_runtime.py)

printf '%s\n' backend frontend browser runtime-isolation
