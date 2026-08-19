from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from audio_memory.security.local_session import LocalSessionSecurity
from audio_memory.security.middleware import LocalWebSecurityMiddleware


app = FastAPI()
calls = 0
profile = os.environ.get("AUDIO_MEMORY_TEST_BACKEND_PROFILE", "development")
security = LocalSessionSecurity(Path(os.environ["AUDIO_MEMORY_TEST_SECURITY_DB"]))
app.add_middleware(
    LocalWebSecurityMiddleware,
    security=security,
    allowed_port=int(os.environ["AUDIO_MEMORY_TEST_BACKEND_PORT"]),
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "profile": profile,
    }


@app.get("/api/test-profile/{next_profile}")
async def set_profile(next_profile: str) -> dict[str, str]:
    global profile
    profile = next_profile
    return {"profile": profile}


@app.post("/api/effect", status_code=201)
async def effect() -> dict[str, int]:
    global calls
    calls += 1
    return {"calls": calls}


@app.get("/api/count")
async def count() -> dict[str, int]:
    return {"calls": calls}
