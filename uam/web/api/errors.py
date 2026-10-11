"""Central mapping of `UamError` subclasses to HTTP responses."""

from __future__ import annotations

import math
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from uam.errors import (
    AuthError,
    ConflictError,
    DeployError,
    NotFound,
    PortRangeExhausted,
    RateLimited,
    RemoteError,
    TemplateParseError,
    UamError,
    ValidationError,
)
from uam.logging import redact
from uam.web.auth import CsrfError


def error_response(exc: UamError) -> JSONResponse:
    """Translate a `UamError` into a JSON response: `{"detail": ..., "stage"?: ...}`."""
    detail = redact(str(exc))
    headers: dict[str, str] = {}
    body: dict[str, object] = {"detail": detail}
    status = 500
    if isinstance(exc, CsrfError):
        status = 403
    elif isinstance(exc, AuthError):
        status = 401
    elif isinstance(exc, NotFound):
        status = 404
    elif isinstance(exc, ConflictError | PortRangeExhausted):
        status = 409
    elif isinstance(exc, ValidationError | TemplateParseError):
        status = 422
    elif isinstance(exc, RateLimited):
        status = 429
        reset = exc.reset_at if exc.reset_at.tzinfo else exc.reset_at.replace(tzinfo=UTC)
        headers["Retry-After"] = str(max(1, math.ceil((reset - datetime.now(UTC)).total_seconds())))
    elif isinstance(exc, RemoteError):
        status = 502
    elif isinstance(exc, DeployError):
        body["stage"] = exc.stage
    return JSONResponse(body, status_code=status, headers=headers)


class UamRoute(APIRoute):
    """Route class that converts `UamError`s into responses, so the router works on any app."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the normal handler with the error mapping."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except UamError as exc:
                return error_response(exc)

        return handler
