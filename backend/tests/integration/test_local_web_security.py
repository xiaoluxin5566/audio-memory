from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from audio_memory.config import AppPaths
from audio_memory.main import create_app


LOCAL_BASE_URL = "http://127.0.0.1:8765"
LOCAL_ORIGIN = "http://127.0.0.1:8765"


def protected_app(
    storage: Path,
    calls: dict[str, int],
    gate: asyncio.Event | None = None,
    *,
    max_records: int = 1000,
    max_sessions: int = 1000,
    session_ttl_seconds: int = 24 * 60 * 60,
):
    try:
        from audio_memory.security.local_session import LocalSessionSecurity
        from audio_memory.security.middleware import LocalWebSecurityMiddleware
    except ModuleNotFoundError:
        pytest.fail("the local web security boundary has not been implemented")

    app = FastAPI()
    security = LocalSessionSecurity(
        storage,
        max_idempotency_records=max_records,
        max_live_sessions=max_sessions,
        session_ttl_seconds=session_ttl_seconds,
    )
    app.add_middleware(
        LocalWebSecurityMiddleware,
        security=security,
        allowed_port=8765,
    )

    @app.get("/api/read")
    async def read_only() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/effect")
    async def effect(request: Request) -> JSONResponse:
        calls["effect"] = calls.get("effect", 0) + 1
        if gate is not None:
            await gate.wait()
        payload = await request.json()
        return JSONResponse(
            {"call": calls["effect"], "value": payload["value"]}, status_code=201
        )

    @app.post("/api/other")
    async def other() -> dict[str, bool]:
        calls["other"] = calls.get("other", 0) + 1
        return {"ok": True}

    @app.delete("/api/no-content", status_code=204)
    async def no_content() -> Response:
        calls["delete"] = calls.get("delete", 0) + 1
        return Response(status_code=204)

    @app.post("/api/rejected")
    async def rejected() -> JSONResponse:
        calls["rejected"] = calls.get("rejected", 0) + 1
        return JSONResponse(
            {"detail": {"code": "invalid", "message": "bad input"}},
            status_code=422,
        )

    @app.post("/api/upload")
    async def upload(request: Request) -> JSONResponse:
        calls["upload"] = calls.get("upload", 0) + 1
        form = await request.form()
        uploaded = form["file"]
        content = await uploaded.read()
        return JSONResponse(
            {"call": calls["upload"], "name": uploaded.filename, "size": len(content)},
            status_code=201,
        )

    @app.post("/api/explodes")
    async def explodes() -> None:
        calls["explodes"] = calls.get("explodes", 0) + 1
        raise RuntimeError("route exploded")

    @app.get("/")
    async def frontend() -> Response:
        return Response("product")

    @app.get("/apiary")
    async def api_prefix_lookalike() -> Response:
        return Response("static product route")

    return app


async def issue_session(
    client: httpx.AsyncClient, *, origin: str | None = LOCAL_ORIGIN
) -> str:
    headers = {"Origin": origin} if origin is not None else {}
    response = await client.get("/api/session", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()["token"]


def mutation_headers(token: str, key: str = "action-1", *, origin: str = LOCAL_ORIGIN):
    return {
        "Origin": origin,
        "X-Audio-Memory-Session": token,
        "Idempotency-Key": key,
    }


@pytest.mark.asyncio
async def test_session_and_mutations_require_the_trusted_local_web_context(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        missing_origin = await client.post(
            "/api/effect",
            headers={
                "X-Audio-Memory-Session": token,
                "Idempotency-Key": "missing-origin",
            },
            json={"value": "x"},
        )
        foreign_origin = await client.post(
            "/api/effect",
            headers=mutation_headers(token, "foreign", origin="https://evil.example"),
            json={"value": "x"},
        )
        missing_session = await client.post(
            "/api/effect",
            headers={"Origin": LOCAL_ORIGIN, "Idempotency-Key": "missing-session"},
            json={"value": "x"},
        )
        malformed_session = await client.post(
            "/api/effect",
            headers=mutation_headers("not-a-session", "malformed"),
            json={"value": "x"},
        )
        non_ascii_session = await client.post(
            "/api/effect",
            headers=[
                (b"Origin", LOCAL_ORIGIN.encode()),
                (b"X-Audio-Memory-Session", token.encode() + b"\xff"),
                (b"Idempotency-Key", b"non-ascii-session"),
            ],
            json={"value": "x"},
        )
        missing_key = await client.post(
            "/api/effect",
            headers={"Origin": LOCAL_ORIGIN, "X-Audio-Memory-Session": token},
            json={"value": "x"},
        )

    assert missing_origin.status_code == 403
    assert foreign_origin.status_code == 403
    assert missing_session.status_code == 401
    assert malformed_session.status_code == 401
    assert non_ascii_session.status_code == 401
    assert missing_key.status_code == 400
    assert calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("http://localhost:8765", "http://localhost:8765"),
        ("http://[::1]:8765", "http://[::1]:8765"),
    ],
)
async def test_exact_ipv4_ipv6_and_localhost_origins_are_allowed(
    tmp_path: Path, base_url: str, origin: str
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        token = await issue_session(client, origin=origin)
        response = await client.post(
            "/api/effect",
            headers=mutation_headers(token, origin=origin),
            json={"value": "local"},
        )

    assert response.status_code == 201
    assert response.json() == {"call": 1, "value": "local"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "cross_origin"),
    [
        ("http://127.0.0.1:8765", "http://localhost:8765"),
        ("http://127.0.0.1:8765", "http://[::1]:8765"),
        ("http://localhost:8765", "http://127.0.0.1:8765"),
        ("http://localhost:8765", "http://[::1]:8765"),
        ("http://[::1]:8765", "http://127.0.0.1:8765"),
        ("http://[::1]:8765", "http://localhost:8765"),
    ],
)
async def test_mutation_origin_must_match_the_validated_host(
    tmp_path: Path, base_url: str, cross_origin: str
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        token = await issue_session(client, origin=base_url)
        response = await client.post(
            "/api/effect",
            headers=mutation_headers(token, "cross-pair", origin=cross_origin),
            json={"value": "must not run"},
        )

    assert response.status_code == 403
    assert calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "cross_origin"),
    [
        ("http://127.0.0.1:8765", "http://localhost:8765"),
        ("http://localhost:8765", "http://[::1]:8765"),
        ("http://[::1]:8765", "http://127.0.0.1:8765"),
    ],
)
async def test_session_origin_when_present_must_match_the_validated_host(
    tmp_path: Path, base_url: str, cross_origin: str
) -> None:
    app = protected_app(tmp_path / "security.sqlite3", {})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        response = await client.get("/api/session", headers={"Origin": cross_origin})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_cross_site_browser_session_request_without_origin_is_rejected(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "security.sqlite3"
    app = protected_app(storage, {})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        cross_site = await client.get(
            "/api/session", headers={"Sec-Fetch-Site": "cross-site"}
        )
        direct_client = await client.get("/api/session")

    assert cross_site.status_code == 403
    assert cross_site.json()["detail"]["code"] == "untrusted_origin"
    assert direct_client.status_code == 200
    with sqlite3.connect(storage) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_sessions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_dns_rebinding_and_lookalike_local_hosts_are_rejected(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://evil.example:8765"
    ) as client:
        rebound_session = await client.get(
            "/api/session", headers={"Origin": "http://evil.example:8765"}
        )
        rebound_read = await client.get("/api/read")
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        lookalike = await client.post(
            "/api/effect",
            headers=mutation_headers(
                token, "lookalike", origin="http://localhost.evil.example:8765"
            ),
            json={"value": "x"},
        )

    assert rebound_session.status_code == 403
    assert rebound_read.status_code == 403
    assert lookalike.status_code == 403
    assert calls == {}


@pytest.mark.asyncio
async def test_static_navigation_and_read_only_api_do_not_require_session_headers(
    tmp_path: Path,
) -> None:
    app = protected_app(tmp_path / "security.sqlite3", {})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        frontend = await client.get("/")
        read_only = await client.get("/api/read")
        session_without_origin = await client.get("/api/session")
    async with httpx.AsyncClient(
        transport=transport, base_url="http://public-navigation.example"
    ) as client:
        api_prefix_lookalike = await client.get("/apiary")

    assert frontend.status_code == 200
    assert read_only.status_code == 200
    assert session_without_origin.status_code == 200
    assert api_prefix_lookalike.status_code == 200


@pytest.mark.asyncio
async def test_completed_response_replays_without_repeating_the_side_effect(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers=mutation_headers(token),
            json={"value": "kept"},
        )
        replay = await client.post(
            "/api/effect",
            headers=mutation_headers(token),
            json={"value": "kept"},
        )

    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_idempotency_scope_includes_session_endpoint_and_body(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token_a = await issue_session(client)
        token_b = await issue_session(client)
        first = await client.post(
            "/api/effect", headers=mutation_headers(token_a), json={"value": "one"}
        )
        changed_body = await client.post(
            "/api/effect", headers=mutation_headers(token_a), json={"value": "two"}
        )
        other_endpoint = await client.post(
            "/api/other", headers=mutation_headers(token_a)
        )
        other_session = await client.post(
            "/api/effect", headers=mutation_headers(token_b), json={"value": "one"}
        )

    assert first.status_code == 201
    assert changed_body.status_code == 409
    assert changed_body.json()["detail"]["code"] == "idempotency_key_reused"
    assert other_endpoint.status_code == 200
    assert other_session.status_code == 201
    assert calls == {"effect": 2, "other": 1}


@pytest.mark.asyncio
async def test_query_is_request_input_not_a_separate_idempotency_scope(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect?mode=one",
            headers=mutation_headers(token, "query-action"),
            json={"value": "same"},
        )
        changed_query = await client.post(
            "/api/effect?mode=two",
            headers=mutation_headers(token, "query-action"),
            json={"value": "same"},
        )

    assert first.status_code == 201
    assert changed_query.status_code == 409
    assert changed_query.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_configuration_session_header_is_part_of_request_fingerprint(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "configuration-action"),
                "X-Configuration-Session": "window-a",
            },
            json={"value": "same"},
        )
        changed_header = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "configuration-action"),
                "X-Configuration-Session": "window-b",
            },
            json={"value": "same"},
        )

    assert first.status_code == 201
    assert changed_header.status_code == 409
    assert changed_header.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_semantic_content_type_is_part_of_request_fingerprint(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "content-type-action"),
                "Content-Type": "application/json",
            },
            content=b'{"value":"same"}',
        )
        changed_header = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "content-type-action"),
                "Content-Type": "application/json; profile=changed",
            },
            content=b'{"value":"same"}',
        )

    assert first.status_code == 201
    assert changed_header.status_code == 409
    assert changed_header.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_content_type_parameter_structure_cannot_collide_in_fingerprint(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "structured-content-type"),
                "Content-Type": 'application/json; a="x;b=y"',
            },
            content=b'{"value":"same"}',
        )
        structurally_different = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "structured-content-type"),
                "Content-Type": "application/json; a=x; b=y",
            },
            content=b'{"value":"same"}',
        )

    assert first.status_code == 201
    assert structurally_different.status_code == 409
    assert structurally_different.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_content_type_parameter_order_normalizes_for_replay(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "ordered-content-type"),
                "Content-Type": "application/json; b=y; a=x",
            },
            content=b'{"value":"same"}',
        )
        reordered = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "ordered-content-type"),
                "Content-Type": "application/json; a=x; b=y",
            },
            content=b'{"value":"same"}',
        )

    assert first.status_code == 201
    assert reordered.status_code == 201
    assert reordered.content == first.content
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_non_multipart_boundary_parameter_remains_semantic(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "non-multipart-boundary"),
                "Content-Type": "application/json; boundary=one",
            },
            content=b'{"value":"same"}',
        )
        changed_boundary = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "non-multipart-boundary"),
                "Content-Type": "application/json; boundary=two",
            },
            content=b'{"value":"same"}',
        )

    assert first.status_code == 201
    assert changed_boundary.status_code == 409
    assert changed_boundary.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_extended_content_type_parameter_is_fingerprinted_without_error(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        response = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "extended-content-type"),
                "Content-Type": "application/json; title*=utf-8''caf%C3%A9",
            },
            content=b'{"value":"same"}',
        )

    assert response.status_code == 201
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_distinct_malformed_content_types_cannot_replay_one_fingerprint(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "malformed-content-type"),
                "Content-Type": "not a content type",
            },
            content=b'{"value":"same"}',
        )
        different = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "malformed-content-type"),
                "Content-Type": "also invalid",
            },
            content=b'{"value":"same"}',
        )

    assert first.status_code == 201
    assert different.status_code == 409
    assert different.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_malformed_content_type_parameters_use_conservative_raw_fingerprint(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "malformed-parameter"),
                "Content-Type": 'application/json; a="x',
            },
            content=b'{"value":"same"}',
        )
        different_raw_value = await client.post(
            "/api/effect",
            headers={
                **mutation_headers(token, "malformed-parameter"),
                "Content-Type": 'application/json;a="x',
            },
            content=b'{"value":"same"}',
        )

    assert first.status_code == 201
    assert different_raw_value.status_code == 409
    assert different_raw_value.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_concurrent_duplicates_wait_for_and_replay_one_execution(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    gate = asyncio.Event()
    app = protected_app(tmp_path / "security.sqlite3", calls, gate)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        headers = mutation_headers(token)
        first_task = asyncio.create_task(
            client.post("/api/effect", headers=headers, json={"value": "once"})
        )
        while calls.get("effect", 0) == 0:
            await asyncio.sleep(0)
        replay_task = asyncio.create_task(
            client.post("/api/effect", headers=headers, json={"value": "once"})
        )
        await asyncio.sleep(0.02)
        assert calls == {"effect": 1}
        gate.set()
        first, replay = await asyncio.gather(first_task, replay_task)

    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_completed_response_and_session_survive_security_store_restart(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "security.sqlite3"
    calls: dict[str, int] = {}
    first_app = protected_app(storage, calls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app), base_url=LOCAL_BASE_URL
    ) as client:
        token = await issue_session(client)
        first = await client.delete(
            "/api/no-content", headers=mutation_headers(token, "delete-once")
        )

    restarted_app = protected_app(storage, calls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app), base_url=LOCAL_BASE_URL
    ) as client:
        replay = await client.delete(
            "/api/no-content", headers=mutation_headers(token, "delete-once")
        )

    assert first.status_code == replay.status_code == 204
    assert first.content == replay.content == b""
    assert calls == {"delete": 1}


@pytest.mark.asyncio
async def test_non_success_response_is_replayed_exactly(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/rejected", headers=mutation_headers(token, "invalid-once")
        )
        replay = await client.post(
            "/api/rejected", headers=mutation_headers(token, "invalid-once")
        )

    assert first.status_code == replay.status_code == 422
    assert first.content == replay.content
    assert calls == {"rejected": 1}


@pytest.mark.asyncio
async def test_unhandled_route_failure_is_published_and_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from audio_memory.security import middleware

    monkeypatch.setattr(middleware, "MAX_PENDING_WAIT_SECONDS", 0.05)
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/explodes", headers=mutation_headers(token, "explodes-once")
        )
        replay = await client.post(
            "/api/explodes", headers=mutation_headers(token, "explodes-once")
        )

    assert first.status_code == replay.status_code == 500
    assert first.content == replay.content
    assert calls == {"explodes": 1}


@pytest.mark.asyncio
async def test_full_ledger_fails_closed_without_evicting_live_replay_protection(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls, max_records=1)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        first = await client.post(
            "/api/effect",
            headers=mutation_headers(token, "first"),
            json={"value": "one"},
        )
        at_capacity = await client.post(
            "/api/other", headers=mutation_headers(token, "second")
        )
        replay = await client.post(
            "/api/effect",
            headers=mutation_headers(token, "first"),
            json={"value": "one"},
        )

    assert first.status_code == replay.status_code == 201
    assert at_capacity.status_code == 409
    assert at_capacity.json()["detail"]["code"] == "idempotency_capacity"
    assert calls == {"effect": 1}


@pytest.mark.asyncio
async def test_upload_retries_hash_multipart_content_instead_of_random_boundary(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        headers = mutation_headers(token, "upload-action")
        first = await client.post(
            "/api/upload",
            headers=headers,
            files={"file": ("meeting.mp3", b"same audio", "audio/mpeg")},
        )
        replay = await client.post(
            "/api/upload",
            headers=headers,
            files={"file": ("meeting.mp3", b"same audio", "audio/mpeg")},
        )

    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content
    assert calls == {"upload": 1}


@pytest.mark.asyncio
async def test_boundary_like_file_bytes_cannot_collapse_different_uploads(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    app = protected_app(tmp_path / "security.sqlite3", calls)
    transport = httpx.ASGITransport(app=app)

    def multipart(boundary: str, file_bytes: bytes) -> bytes:
        return b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file"; filename="meeting.mp3"\r\n',
                b"Content-Type: audio/mpeg\r\n\r\n",
                file_bytes,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )

    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        token = await issue_session(client)
        headers = mutation_headers(token, "boundary-content")
        first = await client.post(
            "/api/upload",
            headers={**headers, "Content-Type": "multipart/form-data; boundary=BOUNDARY"},
            content=multipart("BOUNDARY", b"prefix--BOUNDARYsuffix"),
        )
        changed = await client.post(
            "/api/upload",
            headers={**headers, "Content-Type": "multipart/form-data; boundary=OTHER"},
            content=multipart("OTHER", b"prefix--OTHERsuffix"),
        )

    assert first.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls == {"upload": 1}


@pytest.mark.asyncio
async def test_product_app_protects_every_paid_or_mutating_route_before_dispatch(
    tmp_path: Path,
) -> None:
    app = create_app(paths=AppPaths.from_home(tmp_path), local_port=8765)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    cases = [
        ("POST", "/api/jobs"),
        ("POST", "/api/jobs/job-1/files"),
        ("DELETE", "/api/jobs/job-1/files/file-1"),
        ("POST", "/api/jobs/job-1/start"),
        ("POST", "/api/jobs/job-1/resume"),
        ("POST", "/api/jobs/job-1/retry-analysis"),
        ("DELETE", "/api/jobs/job-1"),
        ("POST", "/api/providers/validate-configured"),
        ("POST", "/api/providers/kimi/validate"),
        ("PUT", "/api/providers/kimi/key"),
        ("DELETE", "/api/providers/kimi/candidate/window-1"),
        ("POST", "/api/providers/kimi/activate"),
        ("PUT", "/api/settings/analysis"),
        ("PUT", "/api/prompts/meeting"),
        ("PATCH", "/api/todos/todo-1"),
        ("DELETE", "/api/todos/todo-1"),
        ("POST", "/api/cards/card-1/questions"),
        ("POST", "/api/cards/card-1/feedback"),
        ("DELETE", "/api/history"),
        ("POST", "/api/history/reanalysis-batches"),
    ]
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        session = await client.get("/api/session")
        responses = [
            await client.request(
                method,
                path,
                headers={"Origin": LOCAL_ORIGIN, "Idempotency-Key": f"case-{index}"},
            )
            for index, (method, path) in enumerate(cases)
        ]
        health = await client.get("/api/health")

    assert session.status_code == 200
    assert all(response.status_code == 401 for response in responses)
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_product_app_uses_the_configured_runtime_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIO_MEMORY_PORT", "9123")
    app = create_app(paths=AppPaths.from_home(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost:9123"
    ) as client:
        response = await client.get("/api/session")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_session_cap_is_transactional_under_concurrent_issuance(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "security.sqlite3"
    app = protected_app(storage, {}, max_sessions=2)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        responses = await asyncio.gather(
            *(client.get("/api/session") for _ in range(8))
        )

    assert [response.status_code for response in responses].count(200) == 2
    assert [response.status_code for response in responses].count(429) == 6
    assert all(
        response.json()["detail"]["code"] == "session_capacity"
        for response in responses
        if response.status_code == 429
    )
    with sqlite3.connect(storage) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_sessions").fetchone()[0] == 2
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(local_sessions)")
        }
    assert "ix_local_sessions_expires_at" in indexes


@pytest.mark.asyncio
async def test_expired_sessions_are_removed_before_capacity_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from audio_memory.security import local_session

    now = 100.0
    monkeypatch.setattr(local_session.time, "time", lambda: now)
    storage = tmp_path / "security.sqlite3"
    app = protected_app(
        storage,
        {},
        max_sessions=1,
        session_ttl_seconds=10,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=LOCAL_BASE_URL) as client:
        first = await client.get("/api/session")
        at_capacity = await client.get("/api/session")
        now = 111.0
        after_expiry = await client.get("/api/session")

    assert first.status_code == 200
    assert at_capacity.status_code == 429
    assert after_expiry.status_code == 200
    with sqlite3.connect(storage) as connection:
        rows = connection.execute("SELECT token_hash FROM local_sessions").fetchall()
    assert len(rows) == 1
    assert first.json()["token"] not in rows[0][0]
