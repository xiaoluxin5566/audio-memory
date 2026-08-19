#!/bin/bash
set -euo pipefail

FEATURE_ROOT="$(pwd -P)"
TOOLCHAIN_ROOT="${AUDIO_MEMORY_TOOLCHAIN_ROOT:-$FEATURE_ROOT}"
PYTEST="$TOOLCHAIN_ROOT/backend/.venv/bin/pytest"
NODE="$(command -v node)"
NPM="$(command -v npm)"
PLAYWRIGHT="$TOOLCHAIN_ROOT/prototype/node_modules/.bin/playwright"
FRONTEND_PATH="$TOOLCHAIN_ROOT/prototype/node_modules/.bin:$PATH"
REQUESTED_CHECK="${1:-all}"

for required in "$PYTEST" "$NODE" "$NPM" "$PLAYWRIGHT"; do
  [ -x "$required" ] || {
    printf '功能门禁失败：缺少可执行工具 %s\n' "$required" >&2
    exit 1
  }
done

run_backend() {
  (cd "$FEATURE_ROOT/backend" && "$PYTEST" -q)
}

run_frontend() {
  (cd "$FEATURE_ROOT/prototype" && "$NODE" --test tests/*.test.mjs)
  (cd "$FEATURE_ROOT/prototype" && env PATH="$FRONTEND_PATH" "$NPM" run build)
}

run_browser() {
  (cd "$FEATURE_ROOT/prototype" && "$PLAYWRIGHT" test)
}

run_runtime_isolation() {
  (cd "$FEATURE_ROOT/backend" && "$PYTEST" -q tests/unit/test_feature_runtime.py)
}

case "$REQUESTED_CHECK" in
  all)
    run_backend
    run_frontend
    run_browser
    run_runtime_isolation
    printf '%s\n' backend frontend browser runtime-isolation
    ;;
  backend) run_backend; printf '%s\n' backend ;;
  frontend) run_frontend; printf '%s\n' frontend ;;
  browser) run_browser; printf '%s\n' browser ;;
  runtime-isolation) run_runtime_isolation; printf '%s\n' runtime-isolation ;;
  *)
    printf '功能门禁失败：未知检查 %s\n' "$REQUESTED_CHECK" >&2
    exit 2
    ;;
esac
