from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path


PROMPT_SCENES = ("todo", "meeting", "parenting", "content", "growth", "inspiration")


class PromptConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromptDocument:
    scene_id: str
    version: int
    content: str


class PromptStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._locks = {scene_id: threading.RLock() for scene_id in PROMPT_SCENES}

    def initialize(self) -> list[PromptDocument]:
        return [self._initialize_scene(scene_id) for scene_id in PROMPT_SCENES]

    def get(self, scene_id: str) -> PromptDocument:
        self._validate_scene(scene_id)
        scene_root = self.root / scene_id
        if not (scene_root / "current.md").exists():
            return self._initialize_scene(scene_id)
        metadata = json.loads((scene_root / "metadata.json").read_text())
        return PromptDocument(
            scene_id,
            int(metadata["version"]),
            (scene_root / "current.md").read_text(),
        )

    def save(
        self, scene_id: str, *, expected_version: int, content: str
    ) -> PromptDocument:
        self._validate_scene(scene_id)
        with self._locks[scene_id]:
            normalized = content.strip()
            if not normalized:
                raise ValueError("Prompt content cannot be blank")
            current = self.get(scene_id)
            if current.version != expected_version:
                raise PromptConflictError(
                    f"Prompt version changed from {expected_version} to {current.version}"
                )
            scene_root = self.root / scene_id
            versions = scene_root / "versions"
            versions.mkdir(mode=0o700, parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            self._atomic_write(
                versions / f"{current.version}-{timestamp}.md", current.content
            )
            next_version = current.version + 1
            self._atomic_write(scene_root / "current.md", normalized)
            self._atomic_write(
                scene_root / "metadata.json",
                json.dumps({"version": next_version}, ensure_ascii=False),
            )
            return PromptDocument(scene_id, next_version, normalized)

    def _initialize_scene(self, scene_id: str) -> PromptDocument:
        self._validate_scene(scene_id)
        scene_root = self.root / scene_id
        current_path = scene_root / "current.md"
        if current_path.exists():
            return self.get(scene_id)
        scene_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (scene_root / "versions").mkdir(mode=0o700, exist_ok=True)
        default = (
            files("audio_memory.prompts.defaults")
            .joinpath(f"{scene_id}.md")
            .read_text()
            .strip()
        )
        self._atomic_write(current_path, default)
        self._atomic_write(
            scene_root / "metadata.json",
            json.dumps({"version": 1}, ensure_ascii=False),
        )
        return PromptDocument(scene_id, 1, default)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _validate_scene(scene_id: str) -> None:
        if scene_id not in PROMPT_SCENES:
            raise ValueError(f"Unsupported prompt scene: {scene_id}")
