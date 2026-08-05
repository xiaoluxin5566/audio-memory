#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL="$ROOT/scripts/install.sh"

expect_failure() {
  expected="$1"; shift
  output="$("$@" 2>&1 || true)"
  printf '%s' "$output" | grep -F "$expected" >/dev/null
}

expect_failure '第一期仅支持 macOS' env AUDIO_MEMORY_PLATFORM_OVERRIDE=Linux AUDIO_MEMORY_ARCH_OVERRIDE=arm64 "$INSTALL"
expect_failure '第一期仅支持 Apple Silicon' env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=x86_64 "$INSTALL"
expect_failure '缺少 ffmpeg' env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=arm64 AUDIO_MEMORY_FORCE_MISSING=ffmpeg "$INSTALL"

env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=arm64 AUDIO_MEMORY_DRY_RUN=1 AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD=1 "$INSTALL" >/dev/null
env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=arm64 AUDIO_MEMORY_DRY_RUN=1 AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD=1 "$INSTALL" >/dev/null

printf 'install smoke: ok\n'
