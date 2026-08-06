from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from audio_memory.db import Database
from audio_memory.content.clear import HistoryCleaner
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    JobFile,
    ReanalysisBatch,
    ReanalysisItem,
    Transcript,
)
from audio_memory.prompts.store import PromptStore
from audio_memory.providers.types import ProviderState, ProviderStateName
from audio_memory.reanalysis.preview import PreviewSigner, ReanalysisPreviewBuilder
from audio_memory.reanalysis.service import ReanalysisService
from audio_memory.security.local_session import LocalSessionSecurity
from audio_memory.security.middleware import LocalWebSecurityMiddleware


ORIGIN = "http://127.0.0.1:8765"


class Provider:
    async def snapshot_active_with_generation(self):
        return (
            ProviderState(
                provider_id="deepseek",
                display_name="DeepSeek",
                model_id="deepseek-v4-flash",
                active=True,
                state=ProviderStateName.AVAILABLE,
            ),
            2,
        )

    async def validate_saved(self, provider_id: str):
        return type("Validation", (), {"ok": provider_id == "deepseek"})()


class NoProvider:
    async def snapshot_active_with_generation(self):
        raise LookupError("No active provider")


class Publisher:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    async def retry_profile(self, batch_id: str) -> None:
        self.calls.append(batch_id)
        async with self.database.session() as session:
            batch = await session.get(ReanalysisBatch, batch_id)
            assert batch is not None
            batch.status = "completed"
            await session.commit()


async def seed_source(database: Database) -> None:
    async with database.session() as session:
        session.add(AnalysisJob(id="job-1", stage="completed"))
        session.add(
            Batch(
                id="batch-1",
                job_id="job-1",
                natural_date="2026-08-05",
                uploaded_at="2026-08-05T12:00:00+00:00",
            )
        )
        session.add(
            JobFile(
                id="file-1",
                job_id="job-1",
                original_name="source.mp3",
                extension=".mp3",
                size_bytes=5,
                sha256="a" * 64,
                position=0,
                temporary_path="/audio/source.mp3",
            )
        )
        session.add(
            Transcript(
                id="transcript-1",
                job_file_id="file-1",
                segment_index=0,
                segment_uid="file-1:0",
                start_ms=0,
                end_ms=1000,
                text="hello",
                words_json="[]",
            )
        )
        await session.flush()
        session.add(
            AnalysisVersion(
                id="version-1",
                source_job_id="job-1",
                batch_id="batch-1",
                provider_id="kimi",
                model_id="old",
                credential_generation=1,
                prompt_snapshot_json="{}",
                profile_snapshot_json="[]",
                fixed_rules_hash="f" * 64,
                staged_results_json="{}",
                priority=0,
                status="completed",
            )
        )
        await session.flush()
        batch = await session.get(Batch, "batch-1")
        assert batch is not None
        batch.current_analysis_version_id = "version-1"
        await session.commit()


async def build_app(tmp_path: Path):
    from audio_memory.api.reanalysis import router
    from audio_memory.api.content import router as content_router

    database = Database(tmp_path / "api.sqlite3")
    await database.create_schema()
    await seed_source(database)
    prompts = PromptStore(tmp_path / "prompts")
    prompts.initialize()
    provider = Provider()
    publisher = Publisher(database)
    service = ReanalysisService(
        database=database,
        preview_builder=ReanalysisPreviewBuilder(
            database=database,
            prompt_store=prompts,
            provider_coordinator=provider,
            signer=PreviewSigner(secret=b"a" * 32),
        ),
        provider_coordinator=provider,
        publisher=publisher,
    )
    app = FastAPI()
    app.state.reanalysis_service = service
    app.state.history_cleaner = HistoryCleaner(
        database, tmp_path / "audio", tmp_path / "staging"
    )
    security = LocalSessionSecurity(tmp_path / "security.sqlite3")
    app.add_middleware(
        LocalWebSecurityMiddleware, security=security, allowed_port=8765
    )
    app.include_router(router)
    app.include_router(content_router)
    return app, database, publisher


async def session_headers(client: httpx.AsyncClient, key: str) -> dict[str, str]:
    issued = await client.get("/api/session", headers={"Origin": ORIGIN})
    assert issued.status_code == 200
    return {
        "Origin": ORIGIN,
        "X-Audio-Memory-Session": issued.json()["token"],
        "Idempotency-Key": key,
    }


@pytest.mark.asyncio
async def test_preview_create_replay_and_current_api_contract(tmp_path: Path) -> None:
    app, database, _publisher = await build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        preview = await client.get("/api/history/reanalysis-batches/preview")
        assert preview.status_code == 200
        assert preview.json()["source_batch_count"] == 1
        assert preview.json()["audio_file_count"] == 1
        assert preview.json()["transcript_character_count"] == 5
        assert preview.json()["whisper_calls"] == 0
        assert preview.json()["diarization_calls"] == 0

        rejected = await client.post(
            "/api/history/reanalysis-batches",
            json={"preview_token": preview.json()["preview_token"]},
        )
        assert rejected.status_code == 403

        headers = await session_headers(client, "create-history")
        first = await client.post(
            "/api/history/reanalysis-batches",
            headers=headers,
            json={"preview_token": preview.json()["preview_token"]},
        )
        replay = await client.post(
            "/api/history/reanalysis-batches",
            headers=headers,
            json={"preview_token": preview.json()["preview_token"]},
        )
        current = await client.get("/api/history/reanalysis-batches/current")

    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content
    assert current.status_code == 200
    assert current.json()["id"] == first.json()["id"]
    assert current.json()["status"] == "pending"
    assert current.json()["total"] == 1
    assert current.json()["pending"] == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_forged_preview_is_rejected_before_any_work_is_created(
    tmp_path: Path,
) -> None:
    app, database, _publisher = await build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        preview = (await client.get("/api/history/reanalysis-batches/preview")).json()
        headers = await session_headers(client, "forged-history")
        token = preview["preview_token"]
        forged = ("A" if token[0] != "A" else "B") + token[1:]
        response = await client.post(
            "/api/history/reanalysis-batches",
            headers=headers,
            json={"preview_token": forged},
        )
        current = await client.get("/api/history/reanalysis-batches/current")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "snapshot_changed"
    assert current.status_code == 204
    await database.dispose()


@pytest.mark.asyncio
async def test_no_provider_preview_create_returns_documented_conflict(
    tmp_path: Path,
) -> None:
    app, database, _publisher = await build_app(tmp_path)
    provider = NoProvider()
    app.state.reanalysis_service.provider_coordinator = provider
    app.state.reanalysis_service.preview_builder.provider_coordinator = provider
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        preview = (await client.get("/api/history/reanalysis-batches/preview")).json()
        response = await client.post(
            "/api/history/reanalysis-batches",
            headers=await session_headers(client, "no-provider-create"),
            json={"preview_token": preview["preview_token"]},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "reanalysis_blocked",
        "message": "Reanalysis preview is blocked",
        "blockers": ["no_active_provider"],
    }
    await database.dispose()


@pytest.mark.asyncio
async def test_stop_resume_and_profile_retry_are_protected_idempotent_controls(
    tmp_path: Path,
) -> None:
    app, database, publisher = await build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        preview = (await client.get("/api/history/reanalysis-batches/preview")).json()
        create_headers = await session_headers(client, "create-controls")
        created = await client.post(
            "/api/history/reanalysis-batches",
            headers=create_headers,
            json={"preview_token": preview["preview_token"]},
        )
        batch_id = created.json()["id"]

        rejected = await client.post(
            f"/api/history/reanalysis-batches/{batch_id}/stop"
        )
        control_headers = await session_headers(client, "stop-controls")
        stopped = await client.post(
            f"/api/history/reanalysis-batches/{batch_id}/stop",
            headers=control_headers,
        )
        replayed_stop = await client.post(
            f"/api/history/reanalysis-batches/{batch_id}/stop",
            headers=control_headers,
        )
        resumed = await client.post(
            f"/api/history/reanalysis-batches/{batch_id}/resume",
            headers=await session_headers(client, "resume-controls"),
        )

        async with database.session() as session:
            batch = await session.get(ReanalysisBatch, batch_id)
            item = await session.scalar(
                select(ReanalysisItem).where(
                    ReanalysisItem.reanalysis_batch_id == batch_id
                )
            )
            assert batch is not None and item is not None
            batch.status = "content_completed_profile_failed"
            item.status = "succeeded"
            await session.commit()
        retried = await client.post(
            f"/api/history/reanalysis-batches/{batch_id}/retry-profile",
            headers=await session_headers(client, "retry-profile-controls"),
        )

    assert rejected.status_code == 403
    assert stopped.status_code == replayed_stop.status_code == 200
    assert stopped.content == replayed_stop.content
    assert stopped.json()["status"] == "stopped"
    assert resumed.status_code == 200 and resumed.json()["status"] == "running"
    assert retried.status_code == 200 and retried.json()["status"] == "completed"
    assert publisher.calls == [batch_id]
    async with database.session() as session:
        generated = int(
            await session.scalar(
                select(func.count(AnalysisVersion.id)).where(
                    AnalysisVersion.reanalysis_batch_id == batch_id
                )
            )
            or 0
        )
    assert generated == 0
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_status", ["running", "paused"])
async def test_clear_history_returns_conflict_while_reanalysis_is_active(
    tmp_path: Path, batch_status: str
) -> None:
    app, database, _publisher = await build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        preview = (await client.get("/api/history/reanalysis-batches/preview")).json()
        created = await client.post(
            "/api/history/reanalysis-batches",
            headers=await session_headers(client, f"create-clear-{batch_status}"),
            json={"preview_token": preview["preview_token"]},
        )
        batch_id = created.json()["id"]
        async with database.session() as session:
            batch = await session.get(ReanalysisBatch, batch_id)
            assert batch is not None
            batch.status = batch_status
            await session.commit()
        response = await client.request(
            "DELETE",
            "/api/history",
            headers=await session_headers(client, f"clear-{batch_status}"),
            json={"confirm": True},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "history_reanalysis_active"
    async with database.session() as session:
        assert await session.get(Batch, "batch-1") is not None
    await database.dispose()


@pytest.mark.asyncio
async def test_application_lifespan_wires_and_closes_reanalysis_worker(
    tmp_path: Path,
) -> None:
    from audio_memory.config import AppPaths
    from audio_memory.main import create_app

    app = create_app(paths=AppPaths.from_home(tmp_path), local_port=8765)

    async with app.router.lifespan_context(app):
        assert app.state.reanalysis_service is not None
        assert app.state.reanalysis_worker._task is not None
        assert not app.state.reanalysis_worker._task.done()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            response = await client.get("/api/history/reanalysis-batches/current")
        assert response.status_code == 204

    assert app.state.reanalysis_worker._task is None
