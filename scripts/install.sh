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
  run env UV_CACHE_DIR="$PROJECT_ROOT/.uv-cache" uv sync --frozen --no-dev --extra database --extra macos --extra transcription
)
(
  cd "$PROJECT_ROOT/prototype"
  run npm ci
  run npm run build
)

if [ "${AUDIO_MEMORY_SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    printf '将下载并登记本地 Whisper 模型：mlx-community/whisper-large-v3-turbo\n'
  else
    (
      cd "$PROJECT_ROOT/backend"
      env UV_CACHE_DIR="$PROJECT_ROOT/.uv-cache" uv run --no-sync python - <<'PY'
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

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
PY
    )
  fi
fi

printf '\n安装完成。运行下面的命令启动：\n  ./scripts/start.sh\n'
