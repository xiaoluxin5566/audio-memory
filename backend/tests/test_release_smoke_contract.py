from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_real_smoke_module():
    script = Path(__file__).resolve().parents[2] / "tests" / "real-pipeline-smoke.py"
    spec = importlib.util.spec_from_file_location("real_pipeline_smoke", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_smoke_accepts_every_published_beta8_card() -> None:
    module = _load_real_smoke_module()
    cards = [
        {"id": "card-1", "scene_id": "work_communication"},
        {"id": "card-2", "scene_id": "work_communication"},
        {"id": "card-3", "scene_id": "parenting_family"},
    ]

    module.validate_published_cards(cards, published_card_count=3)
