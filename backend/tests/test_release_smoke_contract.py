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


def test_release_smoke_validates_saved_cloud_asr_before_job_creation() -> None:
    module = _load_real_smoke_module()
    calls: list[str] = []

    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {
                "providers": [
                    {
                        "provider_id": "deepseek",
                        "state": "available",
                    }
                ]
            }

    class Client:
        def get(self, path: str):
            calls.append(path)
            return Response()

    def post(path: str):
        calls.append(path)
        return Response()

    module.prepare_runtime_providers(Client(), post)

    assert calls == [
        "/api/providers",
        "/api/providers/deepseek/activate",
        "/api/asr/validate",
    ]
