#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLATFORM="${AUDIO_MEMORY_PLATFORM_OVERRIDE:-$(uname -s)}"
ARCHITECTURE="${AUDIO_MEMORY_ARCH_OVERRIDE:-$(uname -m)}"
DRY_RUN="${AUDIO_MEMORY_DRY_RUN:-0}"
MODEL_SMOKE_TEST="${AUDIO_MEMORY_MODEL_SMOKE_TEST:-0}"
PREBUILT="${AUDIO_MEMORY_PREBUILT:-0}"
UV="uv"
FFMPEG_BIN=""
DEPENDENCY_CACHE="${AUDIO_MEMORY_UV_CACHE_DIR:-$PROJECT_ROOT/.uv-cache}"
if [ "$PREBUILT" = "1" ]; then
  UV="$PROJECT_ROOT/runtime/uv/uv"
  FFMPEG_BIN="$PROJECT_ROOT/runtime/ffmpeg/bin"
fi

fail() { printf '安装失败：%s\n' "$1" >&2; exit 1; }
need() {
  if [ "${AUDIO_MEMORY_FORCE_MISSING:-}" = "$1" ] || ! command -v "$1" >/dev/null 2>&1; then
    fail "缺少 $1。请先安装后重新运行 ./scripts/install.sh"
  fi
}
run() {
  if [ "$DRY_RUN" = "1" ]; then printf '将执行：'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

[ "$PLATFORM" = "Darwin" ] || fail "第一期仅支持 macOS。"
[ "$ARCHITECTURE" = "arm64" ] || fail "第一期仅支持 Apple Silicon（M 系列芯片）。"
if [ "$MODEL_SMOKE_TEST" = "1" ]; then
  need python3
else
  if [ "$PREBUILT" = "1" ]; then
    [ -x "$UV" ] || fail "随包 uv 运行组件缺失"
    [ -x "$FFMPEG_BIN/ffmpeg" ] && [ -x "$FFMPEG_BIN/ffprobe" ] || fail "随包音频运行组件缺失"
  else
    need uv
    need ffmpeg
    need npm
  fi

  printf '正在准备 Audio Memory…\n'
  (
    cd "$PROJECT_ROOT/backend"
    run env UV_CACHE_DIR="$DEPENDENCY_CACHE" "$UV" sync --frozen --no-dev --extra database --extra macos --extra transcription --extra diarization
  )
  if [ "$PREBUILT" != "1" ]; then
    (
      cd "$PROJECT_ROOT/prototype"
      run npm ci
      run npm run build
    )
  elif [ ! -f "$PROJECT_ROOT/prototype/dist/client/index.html" ]; then
    fail "预构建前端文件不存在。"
  fi
fi

if [ "${AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    printf '将下载并登记本地 Whisper 模型：mlx-community/whisper-large-v3-turbo\n'
    printf '将下载并校验本地语音检测模型：silero_vad.onnx sha256=9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6\n'
    printf '将下载并校验本地说话人分段模型：sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx sha256=10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e\n'
    printf '将下载并校验本地说话人嵌入模型：3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx sha256=1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b\n'
  else
    (
      cd "$PROJECT_ROOT/backend"
      python_command=(env UV_CACHE_DIR="$DEPENDENCY_CACHE" "$UV" run --no-sync python)
      if [ "$MODEL_SMOKE_TEST" = "1" ]; then
        python_command=(python3)
      fi
      "${python_command[@]}" - <<'PY'
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

smoke_test = os.environ.get("AUDIO_MEMORY_MODEL_SMOKE_TEST") == "1"
root_override = os.environ.get("AUDIO_MEMORY_MODEL_ROOT_OVERRIDE")
manifest_root = (
    Path(root_override)
    if root_override
    else Path.home() / "Library" / "Application Support" / "AudioMemory"
)
manifest_root.mkdir(mode=0o700, parents=True, exist_ok=True)

if not smoke_test:
    from huggingface_hub import snapshot_download

    model_id = "mlx-community/whisper-large-v3-turbo"
    snapshot = Path(snapshot_download(repo_id=model_id))
    files = []
    for path in sorted(snapshot.rglob("*")):
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            files.append(
                {
                    "path": str(path.relative_to(snapshot)),
                    "sha256": hasher.hexdigest(),
                    "size": path.stat().st_size,
                }
            )
    manifest = {
        "model_id": model_id,
        "snapshot": str(snapshot),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    target = manifest_root / "whisper-model-manifest.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)

diarization_root = manifest_root / "models" / "diarization"
vad_target = diarization_root / "silero_vad.onnx"
segmentation_target = (
    diarization_root
    / "sherpa-onnx-pyannote-segmentation-3-0"
    / "model.int8.onnx"
)
embedding_target = (
    diarization_root
    / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)
if smoke_test:
    fixture_root = Path(os.environ["AUDIO_MEMORY_MODEL_FIXTURE_DIR"])
    model_specs = (
        (vad_target, (fixture_root / "silero_vad.onnx").as_uri(), "4eb700fab059058cd267f0b640d4f5aff07f5158b97553ea7dbeaec7e263d122", 12),
        (segmentation_target, (fixture_root / "model.int8.onnx").as_uri(), "a95925d7809c1af5b13c188c3642c62fa1553645b6fd068504c54fc2e7531870", 21),
        (embedding_target, (fixture_root / "embedding.onnx").as_uri(), "abda33e7bd954bcc6381affcb1525774aaf7ad77aa931b0f8f8e54325aa61de2", 18),
    )
else:
    model_specs = (
        (
            vad_target,
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
            "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
            None,
        ),
        (
            segmentation_target,
            "https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/340b52f1f5cd12d45a30fa284691417eaad2ff92/model.int8.onnx",
            "10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e",
            1540514,
        ),
        (
            embedding_target,
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
            "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b",
            None,
        ),
    )


def atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def model_is_valid(path: Path, expected_sha256: str, expected_size) -> bool:
    return (
        path.is_file()
        and (expected_size is None or path.stat().st_size == expected_size)
        and file_sha256(path) == expected_sha256
    )


def install_verified_model(
    destination: Path,
    url: str,
    expected_sha256: str,
    expected_size,
) -> None:
    if model_is_valid(destination, expected_sha256, expected_size):
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    temporary.unlink(missing_ok=True)
    try:
        with urlopen(url, timeout=120) as response, temporary.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not model_is_valid(temporary, expected_sha256, expected_size):
            raise RuntimeError(f"Model integrity verification failed: {destination.name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


for model_path, model_url, expected_sha256, expected_size in model_specs:
    install_verified_model(
        model_path, model_url, expected_sha256, expected_size
    )

diarization_files = []
for model_path, model_url, expected_sha256, expected_size in model_specs:
    diarization_files.append(
        {
            "path": str(model_path.relative_to(manifest_root)),
            "sha256": file_sha256(model_path),
            "expected_sha256": expected_sha256,
            "size": model_path.stat().st_size,
            "expected_size": expected_size,
            "source": model_url,
        }
    )
diarization_manifest = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source": "pinned official model sources",
    "files": diarization_files,
}
diarization_manifest_target = manifest_root / "diarization-model-manifest.json"
atomic_write(
    diarization_manifest_target,
    json.dumps(diarization_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
)
PY
    )
  fi
fi

if [ "$PREBUILT" = "1" ]; then
  printf '\n运行环境准备完成。请运行：audio-memory start\n'
else
  printf '\n安装完成。运行下面的命令启动：\n  ./scripts/start.sh\n'
fi
