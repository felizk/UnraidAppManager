"""The 'UAM itself' panel (Settings) and the dashboard banner for UAM's own updates."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from uam.errors import UamError
from uam.web.deps import ApiServices
from uam.web.ui.support import Services, UiRoute, error_text, render

router = APIRouter(route_class=UiRoute)


async def _panel(request: Request, services: ApiServices, *, error: str = "", notice: str = "") -> Response:
    updater = services.self_updater
    status = await updater.status() if updater is not None else None
    return render(request, "_self_update.html", s=status, error=error, notice=notice)


@router.get("/fragments/self")
async def self_panel(request: Request, services: Services) -> Response:
    """UAM's container, image and update state (polled while an update runs)."""
    return await _panel(request, services)


@router.get("/fragments/self/banner")
async def self_banner(request: Request, services: Services) -> Response:
    """A one-line dashboard notice when a UAM update is available or running (empty otherwise)."""
    updater = services.self_updater
    status = await updater.status() if updater is not None else None
    return render(request, "_self_banner.html", s=status)


@router.post("/self/check")
async def self_check(request: Request, services: Services) -> Response:
    """Check the registry for a newer UAM image now."""
    if services.self_updater is not None:
        await services.self_updater.check()
    return await _panel(request, services)


@router.post("/self/update")
async def self_update(request: Request, services: Services) -> Response:
    """Start updating UAM; the panel then polls until UAM is back."""
    if services.self_updater is None:
        return await _panel(request, services, error="self-update is not available in this mode")
    try:
        await services.self_updater.start_update()
    except UamError as exc:
        return await _panel(request, services, error=error_text(exc))
    return await _panel(
        request, services, notice="Updating. UAM restarts in a minute or so; this page reconnects."
    )
