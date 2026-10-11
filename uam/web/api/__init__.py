"""REST API: `create_api_router()` for mounting (T14) and `create_api_app()` for tests and OpenAPI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from uam.web.api.errors import UamRoute
from uam.web.api.routes import protected, public
from uam.web.auth import csrf_guard, require_session
from uam.web.deps import ApiServices


def create_api_router() -> APIRouter:
    """The `/api` router. It reads `request.app.state.services` (an `ApiServices`) at request time."""
    root = APIRouter(prefix="/api", route_class=UamRoute, dependencies=[Depends(csrf_guard)])
    root.include_router(public)
    root.include_router(protected, dependencies=[Depends(require_session)])
    return root


def create_api_app(services: ApiServices | None = None) -> FastAPI:
    """A FastAPI app with only the API mounted (tests, OpenAPI generation)."""
    app = FastAPI(title="Unraid App Manager", docs_url=None, redoc_url=None)
    if services is not None:
        app.state.services = services
    app.include_router(create_api_router())
    return app
