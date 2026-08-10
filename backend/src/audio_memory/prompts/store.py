from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextlib import ExitStack
from hashlib import sha256
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path


PROMPT_SCENES = ("todo", "meeting", "parenting", "content", "growth", "inspiration")
PACKAGED_DEFAULT_VERSION = 4

KNOWN_LEGACY_DEFAULT_HASHES = {
    "todo": frozenset(
        {
            "5c54c8f52ffe241b65b881cacb43a411c477d915b1a50e057d5d8b325dc18b79",
            "045c1ad65195e7e421c2344a6977fd18f0b1da409b516ebf7a3b9d28dc9f07b6",
        }
    ),
    "meeting": frozenset(
        {
            "c724e614e20ff1e6911f3462258ddf485f13a5ea42fda5c490cf4fa5b5780df5",
            "dbd507a9b89e50ba747d0c7ad222f3234a9430fc3704530dec5f76c92bcbc776",
            "dccf15ecdaf6f604bc15aa12734e141738429b123c1fa297a619f4d3f19b74e0",
            "8e1cf25d9ce1dc777cccf10605914c7c4f4f6a343d8619d8b077420f72c9b6ea",
        }
    ),
    "parenting": frozenset(
        {
            "d4e5b25b2a4370ed106d171c4b3b54abac24e158d72a08d2ce3c3954bcc26b12",
            "3459fe4214c28868433b1a8a0c90554be2d6840841a816b13c642dfe9d8d130f",
        }
    ),
    "content": frozenset(
        {
            "c89714f2b1a89c495e49fbe40d6c0db1606a99c158d880915b25bad7b07798e7",
            "8a397345d398578a1668328f21c396007b10f48473966adae422e8998c2a1a1c",
        }
    ),
    "growth": frozenset(
        {
            "a2554e99c6daa055acb5da995f79867eec2d5869c4248fdcdeb1115c40c9eced",
            "9094c8e301469f428a5cf84ef6bb6e2ce876de63b28c127129c759704ac0252b",
        }
    ),
    "inspiration": frozenset(
        {
            "47e97665335d8ffe94bd4f500dee0e8dae6583859ae6880365d60a1b4bdd01ff",
            "9d55e673757e4236e17f306b50ac9a32c45d8a788231a2767708dc274d44c67c",
        }
    ),
}


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

    @contextmanager
    def freeze(self):
        """Prevent Prompt mutation while another durable snapshot is committed."""
        with ExitStack() as stack:
            for scene_id in PROMPT_SCENES:
                stack.enter_context(self._locks[scene_id])
            yield

    def get(self, scene_id: str) -> PromptDocument:
        self._validate_scene(scene_id)
        with self._locks[scene_id]:
            scene_root = self.root / scene_id
            if not (scene_root / "current.md").exists():
                return self._initialize_scene(scene_id)
            return self._reconcile_existing_scene(scene_id)

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
                self._metadata_json(
                    version=next_version,
                    current_source="user",
                ),
            )
            return PromptDocument(scene_id, next_version, normalized)

    def _initialize_scene(self, scene_id: str) -> PromptDocument:
        self._validate_scene(scene_id)
        with self._locks[scene_id]:
            scene_root = self.root / scene_id
            current_path = scene_root / "current.md"
            if current_path.exists():
                return self._reconcile_existing_scene(scene_id)
            scene_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            (scene_root / "versions").mkdir(mode=0o700, exist_ok=True)
            default = self._packaged_default(scene_id)
            self._atomic_write(current_path, default)
            self._atomic_write(
                scene_root / "metadata.json",
                self._metadata_json(version=1, current_source="packaged"),
            )
            return PromptDocument(scene_id, 1, default)

    def _reconcile_existing_scene(self, scene_id: str) -> PromptDocument:
        scene_root = self.root / scene_id
        current_path = scene_root / "current.md"
        metadata_path = scene_root / "metadata.json"
        content = current_path.read_text()
        metadata = (
            json.loads(metadata_path.read_text())
            if metadata_path.exists()
            else {"version": 1}
        )
        version = int(metadata.get("version", 1))
        packaged = self._packaged_default(scene_id)
        content_hash = self._content_hash(content)
        packaged_hash = self._content_hash(packaged)

        if content_hash in KNOWN_LEGACY_DEFAULT_HASHES[scene_id]:
            versions = scene_root / "versions"
            versions.mkdir(mode=0o700, parents=True, exist_ok=True)
            archive = versions / (
                f"{version}-packaged-default-v{PACKAGED_DEFAULT_VERSION}-upgrade.md"
            )
            if not archive.exists():
                self._atomic_write(archive, content)
            version += 1
            content = packaged
            self._atomic_write(current_path, content)
            current_source = "packaged"
        elif content_hash == packaged_hash:
            current_source = "packaged"
        else:
            current_source = "user"

        reconciled = {
            "version": version,
            "packaged_default_version": PACKAGED_DEFAULT_VERSION,
            "current_source": current_source,
        }
        if metadata != reconciled:
            self._atomic_write(
                metadata_path,
                json.dumps(reconciled, ensure_ascii=False, separators=(",", ":")),
            )
        return PromptDocument(scene_id, version, content)

    @staticmethod
    def _packaged_default(scene_id: str) -> str:
        return (
            files("audio_memory.prompts.defaults")
            .joinpath(f"{scene_id}.md")
            .read_text()
            .strip()
        )

    @staticmethod
    def _content_hash(content: str) -> str:
        return sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _metadata_json(*, version: int, current_source: str) -> str:
        return json.dumps(
            {
                "version": version,
                "packaged_default_version": PACKAGED_DEFAULT_VERSION,
                "current_source": current_source,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

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
