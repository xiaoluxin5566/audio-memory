from __future__ import annotations

import re

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


_UPLOAD_PATH = re.compile(r"/api/jobs/[^/]+/files")


class ReadinessUploadMiddleware:
    """Reject multipart uploads before the ASGI receive channel is consumed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _UPLOAD_PATH.fullmatch(str(scope.get("path", ""))) is not None
        ):
            fastapi_app = scope.get("app")
            readiness = getattr(
                getattr(fastapi_app, "state", None), "pipeline_readiness", None
            )
            if readiness is not None:
                result = await readiness.check()
                if not result.ready:
                    response = JSONResponse(
                        status_code=409,
                        content={
                            "detail": {
                                "code": "configuration_required",
                                "missing": list(result.missing),
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)

