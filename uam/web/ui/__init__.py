"""HTML UI: `create_ui_router()` for mounting (T14), `create_ui_app()` for tests."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from uam.web.auth import csrf_guard
from uam.web.deps import ApiServices
from uam.web.ui import admin, appform, apps, auth_pages, imports, sources
from uam.web.ui.support import STATIC_DIR, UiRoute, require_page

__all__ = ["create_ui_app", "create_ui_router", "mount_static"]


def create_ui_router() -> APIRouter:
    """All page routes. They read `request.app.state.services` (an `ApiServices`) at request time.

    Static files are mounted separately with `mount_static(app)`.
    """
    root = APIRouter(route_class=UiRoute, dependencies=[Depends(csrf_guard)])
    root.include_router(auth_pages.router)
    protected = APIRouter(route_class=UiRoute, dependencies=[Depends(require_page)])
    for module in (appform, apps, sources, admin, imports):
        protected.include_router(module.router)
    root.include_router(protected)
    return root


def mount_static(app: FastAPI) -> None:
    """Serve `uam/web/static` at `/static`."""
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def create_ui_app(services: ApiServices | None = None) -> FastAPI:
    """A FastAPI app with only the UI (and static files) mounted, for tests."""
    app = FastAPI(title="Unraid App Manager UI", docs_url=None, redoc_url=None)
    if services is not None:
        app.state.services = services
    app.include_router(create_ui_router())
    mount_static(app)
    return app
