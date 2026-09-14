from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


class P1P5Prompts:
    root = Path(__file__).parent / "beta8-p1-p5-v1"
    input_protocol_version = "p1-p5-complete-activities-v1"
    p4_scene_ids = {
        "work_communication",
        "parenting_family",
        "health_state",
        "content_consumption",
        "inspiration_insight",
        "self_growth",
        "life_decisions",
    }

    @classmethod
    def manifest(cls):
        manifest = json.loads((cls.root / "MANIFEST.json").read_text())
        for item in manifest["prompts"]:
            path = cls.root / f"{item['prompt_id']}.md"
            if sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("P1-P5 prompt fingerprint changed")
        scene_prompts = manifest.get("p4_scene_prompts", [])
        if {item.get("scene_id") for item in scene_prompts} != cls.p4_scene_ids:
            raise ValueError("P4 scene prompt manifest is incomplete")
        for item in scene_prompts:
            path = cls.root / "P4-scenes" / f"{item['scene_id']}.md"
            if sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("P4 scene prompt fingerprint changed")
        if sha256((cls.root / "INPUT-TEMPLATE.md").read_bytes()).hexdigest() != manifest["input_template_sha256"]:
            raise ValueError("P1-P5 input template fingerprint changed")
        if sha256((cls.root / "Q0.md").read_bytes()).hexdigest() != manifest["rubric_sha256"]:
            raise ValueError("P1-P5 rubric fingerprint changed")
        return manifest["prompts"]

    @classmethod
    def fixed_rules_hash(cls):
        prompts = cls.manifest()
        manifest = json.loads((cls.root / "MANIFEST.json").read_text())
        return sha256(json.dumps({
            "prompts": prompts,
            "p4_scene_prompts": manifest["p4_scene_prompts"],
            "input_template_sha256": manifest["input_template_sha256"],
            "rubric_sha256": manifest["rubric_sha256"],
            "protocol": cls.input_protocol_version,
        }, sort_keys=True).encode()).hexdigest()

    @classmethod
    def system(cls, stage, scene_id=None):
        if stage not in {"P1", "P2", "P3", "P4", "P5"}:
            raise ValueError("P1-P5 pipeline permits only P1-P5")
        base = (cls.root / f"{stage}.md").read_text()
        if stage == "P4":
            if scene_id not in cls.p4_scene_ids:
                raise ValueError("P4 requires one valid scene_id")
            scene = (cls.root / "P4-scenes" / f"{scene_id}.md").read_text()
            base += "\n\n# 当前场景的专属写作能力\n\n" + scene
        if stage == "P5":
            base += "\n\n以下是唯一有效的评分细则：\n\n" + (cls.root / "Q0.md").read_text()
        return base

    @classmethod
    def user(cls, data):
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
