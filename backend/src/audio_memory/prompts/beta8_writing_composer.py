from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


class WritingPrompts:
    root = Path(__file__).parent / "beta8-writing-v1"
    input_protocol_version = "complete-segments-file-metadata-once-v1"

    @classmethod
    def manifest(cls) -> list[dict[str, str]]:
        manifest = json.loads((cls.root / "MANIFEST.json").read_text())
        for item in manifest["prompts"]:
            if sha256((cls.root / (item["prompt_id"] + ".md")).read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Writing prompt fingerprint changed")
        for name, key in [("INPUT-TEMPLATE.md", "input_template_sha256"), ("Q0.md", "rubric_sha256")]:
            if sha256((cls.root / name).read_bytes()).hexdigest() != manifest[key]:
                raise ValueError("Writing input/rubric fingerprint changed")
        return manifest["prompts"]

    @classmethod
    def fixed_rules_hash(cls) -> str:
        return sha256(json.dumps({"prompts": cls.manifest(), "template": (cls.root / "INPUT-TEMPLATE.md").read_text(), "input_protocol": cls.input_protocol_version}, sort_keys=True).encode()).hexdigest()

    @classmethod
    def system(cls, stage: str, scene_id: str | None = None) -> str:
        if stage not in {"P1", "P2", "P3", "P4"}:
            raise ValueError("Writing V1 permits only P1-P4")
        key = f"P4-{scene_id}" if stage == "P4" else stage
        if key not in {item["prompt_id"] for item in cls.manifest()}:
            raise ValueError("Unknown writing scene")
        return (cls.root / f"{key}.md").read_text()

    @classmethod
    def user(cls, data: dict) -> str:
        source_files = {}
        def compact(value):
            if isinstance(value, list):
                return [compact(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: compact(item) for key, item in value.items()}
            if {"segment_id", "source_file", "text"}.issubset(result):
                metadata = source_files.setdefault(result["source_file"], {})
                for name in ("file_id", "file_name", "recording_started_at", "timezone"):
                    if name in result:
                        item = result.pop(name)
                        if name in metadata and metadata[name] != item:
                            raise ValueError("Conflicting source file metadata")
                        metadata[name] = item
            return result
        payload = compact(data)
        if any(source_files.values()):
            payload["source_files"] = source_files
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for char, escaped in [("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e")]:
            encoded = encoded.replace(char, escaped)
        return (cls.root / "INPUT-TEMPLATE.md").read_text().replace("{{INPUT_JSON}}", encoded)
