#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLATFORM="${AUDIO_MEMORY_PLATFORM_OVERRIDE:-$(uname -s)}"
ARCHITECTURE="${AUDIO_MEMORY_ARCH_OVERRIDE:-$(uname -m)}"
DRY_RUN="${AUDIO_MEMORY_DRY_RUN:-0}"

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
need uv
need npm
need ffmpeg

printf '正在准备 Audio Memory…\n'
(
  cd "$PROJECT_ROOT/backend"
  run env UV_CACHE_DIR="$PROJECT_ROOT/.uv-cache" uv sync --frozen --no-dev --extra database --extra macos --extra transcription --extra diarization
)
(
  cd "$PROJECT_ROOT/prototype"
  run npm ci
  run npm run build
)

if [ "${AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    printf '将下载并登记本地 Whisper 模型：mlx-community/whisper-large-v3-turbo\n'
    printf '将下载本地语音检测模型：silero_vad.onnx\n'
    printf '将下载本地说话人分段模型：sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx\n'
    printf '将下载本地说话人嵌入模型：3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx\n'
  else
    (
      cd "$PROJECT_ROOT/backend"
      env UV_CACHE_DIR="$PROJECT_ROOT/.uv-cache" uv run --no-sync python - <<'PY'
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

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
        digest = hasher.hexdigest()
        files.append({"path": str(path.relative_to(snapshot)), "sha256": digest, "size": path.stat().st_size})
manifest_root = Path.home() / "Library" / "Application Support" / "AudioMemory"
manifest_root.mkdir(mode=0o700, parents=True, exist_ok=True)
manifest = {"model_id": model_id, "snapshot": str(snapshot), "created_at": datetime.now(UTC).isoformat(), "files": files}
target = manifest_root / "whisper-model-manifest.json"
temporary = target.with_suffix(".tmp")
temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
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
vad_url = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "asr-models/silero_vad.onnx"
)
segmentation_url = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/"
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
embedding_url = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/"
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)


def atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)


if not vad_target.is_file():
    with urlopen(vad_url, timeout=120) as response:
        atomic_write(vad_target, response.read())

if not segmentation_target.is_file():
    with urlopen(segmentation_url, timeout=120) as response:
        archive_bytes = response.read()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:bz2") as archive:
        member = next(
            item
            for item in archive.getmembers()
            if item.isfile() and item.name.endswith("/model.int8.onnx")
        )
        extracted = archive.extractfile(member)
        if extracted is None:
            raise RuntimeError("Pyannote INT8 model is missing from official archive")
        atomic_write(segmentation_target, extracted.read())

if not embedding_target.is_file():
    with urlopen(embedding_url, timeout=120) as response:
        atomic_write(embedding_target, response.read())

diarization_files = []
for model_path in (vad_target, segmentation_target, embedding_target):
    hasher = hashlib.sha256()
    with model_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    diarization_files.append(
        {
            "path": str(model_path.relative_to(manifest_root)),
            "sha256": hasher.hexdigest(),
            "size": model_path.stat().st_size,
        }
    )
diarization_manifest = {
    "created_at": datetime.now(UTC).isoformat(),
    "source": "k2-fsa/sherpa-onnx official releases",
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

printf '\n安装完成。运行下面的命令启动：\n  ./scripts/start.sh\n'
