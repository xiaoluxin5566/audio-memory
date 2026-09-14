from __future__ import annotations

import json

import pytest

from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion, Card
from audio_memory.analysis.pipeline_identity import build_pipeline_parameters
from sqlalchemy import select, func


@pytest.mark.asyncio
async def test_stored_version_cannot_run_changed_prompt_rules(tmp_path, monkeypatch):
    from audio_memory.analysis.beta8_writing_runner import Beta8WritingRunner
    from audio_memory.analysis.beta8_writing_store import WritingStopped
    from audio_memory.prompts.beta8_writing_composer import WritingPrompts
    db = Database(tmp_path / "changed.sqlite3")
    await db.create_schema()
    parameters, canonical, fingerprint = build_pipeline_parameters(pipeline_kind="beta8_writing_v1", provider_id="deepseek", model_id="deepseek-v4-pro", credential_generation=1, search_provider_id=None, search_model_id=None)
    async with db.session() as session:
        session.add(AnalysisJob(id="job", stage="analyzing"))
        session.add(AnalysisVersion(id="version", source_job_id="job", provider_id="deepseek", model_id="deepseek-v4-pro", credential_generation=1, prompt_snapshot_json="{}", profile_snapshot_json="[]", fixed_rules_hash=parameters["fixed_rules_hash"], pipeline_parameters_json=canonical, pipeline_parameters_fingerprint=fingerprint, status="running", worker_owner_id="owner"))
        await session.commit()
    monkeypatch.setattr(WritingPrompts, "fixed_rules_hash", classmethod(lambda cls: "0" * 64))
    runner = Beta8WritingRunner(database=db, transport=None, generation_source=None, output_root=tmp_path / "drafts")
    with pytest.raises(WritingStopped, match="rules changed"):
        await runner.run("version", "owner")
    assert not (tmp_path / "drafts").exists()
    await db.dispose()


def test_new_pipeline_has_its_own_prompt_identity():
    from audio_memory.prompts.beta8_writing_composer import WritingPrompts
    values, _, _ = build_pipeline_parameters(pipeline_kind="beta8_writing_v1", provider_id="deepseek", model_id="deepseek-v4-pro", credential_generation=1, search_provider_id="kimi", search_model_id="kimi-k2.6")
    assert values["fixed_rules_hash"] == WritingPrompts.fixed_rules_hash()
    assert len(values["prompt_manifest"]) == 10


def test_p1_p5_pipeline_has_its_own_prompt_identity():
    from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts
    values, _, _ = build_pipeline_parameters(
        pipeline_kind="beta8_p1_p5_v1",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        credential_generation=1,
        search_provider_id="kimi",
        search_model_id="kimi-k2.6",
    )
    assert values["fixed_rules_hash"] == P1P5Prompts.fixed_rules_hash()
    assert values["prompt_manifest"] == P1P5Prompts.manifest()


@pytest.mark.asyncio
async def test_registered_runner_pauses_for_authorization_without_calling_provider(tmp_path):
    from audio_memory.analysis.beta8_writing_runner import Beta8WritingRunner
    from audio_memory.analysis.version_runner_router import VersionRunnerRouter
    db = Database(tmp_path / "test.sqlite3")
    await db.create_schema()
    parameters, canonical, fingerprint = build_pipeline_parameters(pipeline_kind="beta8_writing_v1", provider_id="deepseek", model_id="deepseek-v4-pro", credential_generation=1, search_provider_id="kimi", search_model_id="kimi-k2.6")
    async with db.session() as session:
        session.add(AnalysisJob(id="job", stage="analyzing"))
        session.add(AnalysisVersion(id="version", source_job_id="job", provider_id="deepseek", model_id="deepseek-v4-pro", credential_generation=1, prompt_snapshot_json="{}", profile_snapshot_json="[]", fixed_rules_hash=parameters["fixed_rules_hash"], pipeline_parameters_json=canonical, pipeline_parameters_fingerprint=fingerprint, status="running", worker_owner_id="owner"))
        await session.commit()
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("No provider or credential access before authorization")
    runner = Beta8WritingRunner(database=db, transport=Forbidden(), generation_source=Forbidden(), output_root=tmp_path / "drafts")
    router = VersionRunnerRouter(database=db, runners={"beta8_writing_v1": runner})
    result = await router.run("version", "owner")
    assert result["status"] == "waiting_for_authorization"
    assert (tmp_path / "drafts/version/execution-request.json").exists()
    async with db.session() as session:
        version = await session.get(AnalysisVersion, "version")
        assert version.status == "paused"
        assert json.loads(version.pipeline_checkpoints_json)["report_phase"] == "waiting_for_authorization"
        assert await session.scalar(select(func.count()).select_from(Card)) == 0
    await db.dispose()
