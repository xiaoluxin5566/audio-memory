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

dry_run_output="$(env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=arm64 AUDIO_MEMORY_DRY_RUN=1 "$INSTALL")"
[[ "$dry_run_output" == *"--extra diarization"* ]]
printf '%s' "$dry_run_output" | grep -F "silero_vad.onnx" >/dev/null
printf '%s' "$dry_run_output" | grep -F "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6" >/dev/null
[[ "$dry_run_output" == *"sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"* ]]
printf '%s' "$dry_run_output" | grep -F "10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e" >/dev/null
[[ "$dry_run_output" == *"3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"* ]]
printf '%s' "$dry_run_output" | grep -F "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b" >/dev/null

env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=arm64 AUDIO_MEMORY_DRY_RUN=1 AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD=1 "$INSTALL" >/dev/null
env AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin AUDIO_MEMORY_ARCH_OVERRIDE=arm64 AUDIO_MEMORY_DRY_RUN=1 AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD=1 "$INSTALL" >/dev/null

smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/audio-memory-model-smoke.XXXXXX")"
cleanup_smoke_root() {
  case "$smoke_root" in
    "${TMPDIR:-/tmp}"/audio-memory-model-smoke.*) rm -rf -- "$smoke_root" ;;
  esac
}
trap cleanup_smoke_root EXIT
fixture_root="$smoke_root/fixtures"
model_root="$smoke_root/install"
mkdir -p "$fixture_root"
printf 'trusted vad\n' >"$fixture_root/silero_vad.onnx"
printf 'trusted segmentation\n' >"$fixture_root/model.int8.onnx"
printf 'trusted embedding\n' >"$fixture_root/embedding.onnx"
vad_target="$model_root/models/diarization/silero_vad.onnx"
segmentation_target="$model_root/models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
embedding_target="$model_root/models/diarization/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
mkdir -p "$(dirname "$segmentation_target")"
printf 'corrupt\n' >"$vad_target"
printf 'corrupt\n' >"$segmentation_target"
printf 'corrupt\n' >"$embedding_target"

model_smoke_env=(
  env
  AUDIO_MEMORY_PLATFORM_OVERRIDE=Darwin
  AUDIO_MEMORY_ARCH_OVERRIDE=arm64
  AUDIO_MEMORY_MODEL_SMOKE_TEST=1
  AUDIO_MEMORY_MODEL_FIXTURE_DIR="$fixture_root"
  AUDIO_MEMORY_MODEL_ROOT_OVERRIDE="$model_root"
)
"${model_smoke_env[@]}" "$INSTALL" >/dev/null
cmp "$fixture_root/silero_vad.onnx" "$vad_target"
cmp "$fixture_root/model.int8.onnx" "$segmentation_target"
cmp "$fixture_root/embedding.onnx" "$embedding_target"

mv "$fixture_root" "$smoke_root/fixtures-hidden"
"${model_smoke_env[@]}" "$INSTALL" >/dev/null
mv "$smoke_root/fixtures-hidden" "$fixture_root"
printf 'corrupt again\n' >"$vad_target"
"${model_smoke_env[@]}" "$INSTALL" >/dev/null
cmp "$fixture_root/silero_vad.onnx" "$vad_target"

printf 'install smoke: ok\n'
