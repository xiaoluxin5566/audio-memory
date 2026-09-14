import json
from hashlib import sha256

import pytest

from audio_memory.analysis.version_runner_router import VersionRunnerRouter
from audio_memory.db import Database
from audio_memory.models import AnalysisJob, AnalysisVersion
from audio_memory.prompts.beta8_composer import Beta8PromptComposer


class Runner:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def run(self, version_id, worker_owner_id):
        self.calls.append((version_id, worker_owner_id))
        return self.name


def fingerprint(parameters: dict[str, object]) -> str:
    canonical = json.dumps(
        parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode()).hexdigest()


def indexed_parameters(
    pipeline_kind: str = "beta8_indexed_scene_v2",
) -> dict[str, object]:
    return {
        "pipeline_kind": pipeline_kind,
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "search_provider_id": "frozen-search",
        "search_model_id": "frozen-search-model",
        "credential_generation": 1,
        "fixed_rules_hash": Beta8PromptComposer.fixed_rules_hash(),
        "prompt_manifest": [
            {"prompt_id": item["prompt_id"], "sha256": item["sha256"]}
            for item in Beta8PromptComposer.prompt_manifest()
        ],
    }


async def seed(database, *, parameters, parameters_fingerprint=None):
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="analyzing"))
        session.add(AnalysisVersion(
            id="version-1", source_job_id="job-1", provider_id="deepseek",
            model_id="deepseek-v4-pro", credential_generation=1,
            prompt_snapshot_json="{}", profile_snapshot_json="[]",
            fixed_rules_hash=(
                parameters.get("fixed_rules_hash")
                or (
                    Beta8PromptComposer.fixed_rules_hash()
                    if str(parameters.get("pipeline_kind", "")).startswith("beta8_")
                    else "a" * 64
                )
            ),
            staged_results_json="{}",
            pipeline_parameters_json=json.dumps(parameters), status="running",
            pipeline_parameters_fingerprint=parameters_fingerprint,
            worker_owner_id="worker-1",
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_legacy_version_without_pipeline_kind_uses_single_runner(tmp_path) -> None:
    database = Database(tmp_path / "legacy.sqlite3"); await database.create_schema()
    await seed(database, parameters={})
    single, beta8 = Runner("single"), Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"single_report_v1": single, "beta8_multi_scene_v1": beta8},
    )
    assert await router.run("version-1", "worker-1") == "single"
    assert beta8.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_pre_task11_five_field_fingerprinted_row_uses_single_runner(
    tmp_path,
) -> None:
    from audio_memory.prompts.composer import PromptComposer

    database = Database(tmp_path / "legacy-five-field.sqlite3")
    await database.create_schema()
    parameters = {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "credential_generation": 1,
        "fixed_rules_hash": PromptComposer.fixed_rules_hash(),
        "prompt_manifest": [
            {
                "role": item["role"],
                "files": list(item["files"]),
                "sha256": item["sha256"],
            }
            for item in PromptComposer.final_report_prompt_manifest()
        ],
    }
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    single = Runner("single")
    router = VersionRunnerRouter(
        database=database,
        runners={"single_report_v1": single},
    )

    assert await router.run("version-1", "worker-1") == "single"
    await database.dispose()


@pytest.mark.asyncio
async def test_historical_fingerprintless_beta8_v1_row_uses_beta8_runner(
    tmp_path,
) -> None:
    database = Database(tmp_path / "legacy-beta8-no-fingerprint.sqlite3")
    await database.create_schema()
    await seed(
        database,
        parameters={
            "pipeline_kind": "beta8_multi_scene_v1",
            "search_provider_id": "old-search",
            "search_model_id": "old-search-model",
        },
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_multi_scene_v1": beta8},
    )

    assert await router.run("version-1", "worker-1") == "beta8"
    await database.dispose()


@pytest.mark.asyncio
async def test_historical_fingerprintless_beta8_v1_kind_only_is_bounded_legacy(
    tmp_path,
) -> None:
    database = Database(tmp_path / "legacy-beta8-kind-only.sqlite3")
    await database.create_schema()
    await seed(
        database,
        parameters={"pipeline_kind": "beta8_multi_scene_v1"},
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_multi_scene_v1": beta8},
    )

    assert await router.run("version-1", "worker-1") == "beta8"
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters",
    [
        {
            "pipeline_kind": "beta8_multi_scene_v1",
            "search_provider_id": "old-search",
        },
        {
            "pipeline_kind": "beta8_multi_scene_v1",
            "search_model_id": "old-search-model",
        },
    ],
)
async def test_historical_fingerprintless_beta8_v1_rejects_half_search_binding(
    tmp_path, parameters,
) -> None:
    database = Database(tmp_path / "legacy-beta8-half-search.sqlite3")
    await database.create_schema()
    await seed(database, parameters=parameters)
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_multi_scene_v1": beta8},
    )

    with pytest.raises(ValueError, match="configured together"):
        await router.run("version-1", "worker-1")

    assert beta8.calls == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        [{"role": "", "files": ["prompt.md"], "sha256": "2" * 64}],
        [{"role": "system", "files": [], "sha256": "2" * 64}],
        [{"role": "system", "files": [""], "sha256": "2" * 64}],
        [{"role": "system", "files": ["prompt.md"], "sha256": "invalid"}],
    ],
)
async def test_fingerprinted_single_rejects_invalid_manifest_fields(
    tmp_path, manifest,
) -> None:
    from audio_memory.prompts.composer import PromptComposer

    database = Database(tmp_path / "single-invalid-manifest.sqlite3")
    await database.create_schema()
    parameters = {
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "credential_generation": 1,
        "fixed_rules_hash": PromptComposer.fixed_rules_hash(),
        "prompt_manifest": manifest,
    }
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    single = Runner("single")
    router = VersionRunnerRouter(
        database=database,
        runners={"single_report_v1": single},
    )

    with pytest.raises(ValueError, match="prompt manifest"):
        await router.run("version-1", "worker-1")

    assert single.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_stored_valid_previous_prompt_binding_still_routes_by_identity(
    tmp_path,
) -> None:
    database = Database(tmp_path / "previous-prompt-binding.sqlite3")
    await database.create_schema()
    parameters = {
        **indexed_parameters(),
        "fixed_rules_hash": "1" * 64,
        "prompt_manifest": [
            {"prompt_id": "event_index", "sha256": "2" * 64}
        ],
    }
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_indexed_scene_v2": beta8},
    )

    assert await router.run("version-1", "worker-1") == "beta8"
    await database.dispose()


@pytest.mark.asyncio
async def test_beta8_version_uses_beta8_runner_after_restart(tmp_path) -> None:
    database = Database(tmp_path / "beta8.sqlite3"); await database.create_schema()
    parameters = indexed_parameters("beta8_multi_scene_v1")
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    single, beta8 = Runner("single"), Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"single_report_v1": single, "beta8_multi_scene_v1": beta8},
    )
    assert await router.run("version-1", "worker-1") == "beta8"
    assert single.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_beta8_version_uses_beta8_runner_after_restart(tmp_path) -> None:
    database = Database(tmp_path / "indexed-beta8.sqlite3"); await database.create_schema()
    parameters = indexed_parameters()
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    single, beta8 = Runner("single"), Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={
            "single_report_v1": single,
            "beta8_multi_scene_v1": beta8,
            "beta8_indexed_scene_v2": beta8,
        },
    )

    assert await router.run("version-1", "worker-1") == "beta8"
    assert single.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_pipeline_rejects_known_kind_tamper_with_stale_fingerprint(
    tmp_path,
) -> None:
    database = Database(tmp_path / "known-kind-tamper.sqlite3")
    await database.create_schema()
    original = indexed_parameters()
    tampered = {**original, "pipeline_kind": "beta8_multi_scene_v1"}
    await seed(
        database,
        parameters=tampered,
        parameters_fingerprint=fingerprint(original),
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_multi_scene_v1": beta8},
    )

    with pytest.raises(ValueError, match="fingerprint"):
        await router.run("version-1", "worker-1")

    assert beta8.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_pipeline_rejects_malformed_stored_manifest_with_valid_fingerprint(
    tmp_path,
) -> None:
    database = Database(tmp_path / "manifest-tamper.sqlite3")
    await database.create_schema()
    tampered = {**indexed_parameters(), "prompt_manifest": {}}
    await seed(
        database,
        parameters=tampered,
        parameters_fingerprint=fingerprint(tampered),
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_indexed_scene_v2": beta8},
    )

    with pytest.raises(ValueError, match="prompt manifest must be a list"):
        await router.run("version-1", "worker-1")

    assert beta8.calls == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        [],
        [{"prompt_id": "", "sha256": "2" * 64}],
        [{"prompt_id": "event_index", "sha256": "not-a-digest"}],
        [{"role": "system", "files": ["prompt.md"], "sha256": "2" * 64}],
    ],
)
async def test_indexed_pipeline_rejects_semantically_invalid_modern_manifest(
    tmp_path, manifest,
) -> None:
    database = Database(tmp_path / f"invalid-manifest-{len(str(manifest))}.sqlite3")
    await database.create_schema()
    parameters = {**indexed_parameters(), "prompt_manifest": manifest}
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    runner = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_indexed_scene_v2": runner},
    )

    with pytest.raises(ValueError, match="prompt manifest"):
        await router.run("version-1", "worker-1")

    assert runner.calls == []
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search_provider_id", "search_model_id"),
    [("frozen-search", None), (None, "frozen-search-model")],
)
async def test_indexed_pipeline_rejects_half_stored_search_binding(
    tmp_path, search_provider_id, search_model_id,
) -> None:
    database = Database(
        tmp_path / f"half-search-{search_provider_id}-{search_model_id}.sqlite3"
    )
    await database.create_schema()
    parameters = {
        **indexed_parameters(),
        "search_provider_id": search_provider_id,
        "search_model_id": search_model_id,
    }
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    runner = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_indexed_scene_v2": runner},
    )

    with pytest.raises(ValueError, match="configured together"):
        await router.run("version-1", "worker-1")

    assert runner.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_pipeline_rejects_parameter_column_mismatch_with_valid_fingerprint(
    tmp_path,
) -> None:
    database = Database(tmp_path / "pipeline-column-mismatch.sqlite3")
    await database.create_schema()
    mismatched = {**indexed_parameters(), "provider_id": "other-provider"}
    await seed(
        database,
        parameters=mismatched,
        parameters_fingerprint=fingerprint(mismatched),
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_indexed_scene_v2": beta8},
    )

    with pytest.raises(ValueError, match="provider_id does not match its column"):
        await router.run("version-1", "worker-1")

    assert beta8.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_indexed_pipeline_rejects_fingerprintless_row(tmp_path) -> None:
    database = Database(tmp_path / "fingerprintless-indexed.sqlite3")
    await database.create_schema()
    await seed(database, parameters=indexed_parameters())
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(
        database=database,
        runners={"beta8_indexed_scene_v2": beta8},
    )

    with pytest.raises(ValueError, match="requires a pipeline fingerprint"):
        await router.run("version-1", "worker-1")

    assert beta8.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_explicit_known_pipeline_rejects_fingerprintless_nonlegacy_row(
    tmp_path,
) -> None:
    database = Database(tmp_path / "fingerprintless-explicit-single.sqlite3")
    await database.create_schema()
    await seed(database, parameters={"pipeline_kind": "single_report_v1"})
    single = Runner("single")
    router = VersionRunnerRouter(
        database=database,
        runners={"single_report_v1": single},
    )

    with pytest.raises(ValueError, match="explicit pipeline identity"):
        await router.run("version-1", "worker-1")

    assert single.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_retry_keeps_original_pipeline_kind_and_prompt_manifest(tmp_path) -> None:
    database = Database(tmp_path / "retry.sqlite3"); await database.create_schema()
    parameters = indexed_parameters("beta8_multi_scene_v1")
    await seed(
        database,
        parameters=parameters,
        parameters_fingerprint=fingerprint(parameters),
    )
    beta8 = Runner("beta8")
    router = VersionRunnerRouter(database=database, runners={"beta8_multi_scene_v1": beta8})
    await router.run("version-1", "worker-1")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, "version-1")
    assert json.loads(version.pipeline_parameters_json) == parameters
    await database.dispose()


@pytest.mark.asyncio
async def test_unknown_pipeline_kind_fails_closed(tmp_path) -> None:
    database = Database(tmp_path / "unknown.sqlite3"); await database.create_schema()
    await seed(database, parameters={"pipeline_kind": "future"})
    runner = Runner("single")
    router = VersionRunnerRouter(database=database, runners={"single_report_v1": runner})
    with pytest.raises(ValueError, match="Unknown report pipeline: future"):
        await router.run("version-1", "worker-1")
    assert runner.calls == []
    await database.dispose()


@pytest.mark.asyncio
async def test_unknown_pipeline_kind_cannot_be_enabled_by_injected_mapping(tmp_path) -> None:
    database = Database(tmp_path / "unknown-injected.sqlite3"); await database.create_schema()
    await seed(database, parameters={"pipeline_kind": "future"})
    future = Runner("future")
    router = VersionRunnerRouter(database=database, runners={"future": future})

    with pytest.raises(ValueError, match="Unknown report pipeline: future"):
        await router.run("version-1", "worker-1")

    assert future.calls == []
    await database.dispose()
