"""Dashboard, app cards, lifecycle actions and the app detail page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from uam.errors import ConflictError
from uam.logging import redact
from uam.web.deps import (
    OrchestratorService,
    UpdateCheckerService,
    get_checker,
    get_orchestrator,
)
from uam.web.ui.support import (
    AppView,
    Services,
    UiRoute,
    build_views,
    form_value,
    redirect,
    render,
    run_in_background,
    shown_value,
)

router = APIRouter(route_class=UiRoute)
Orchestrator = Annotated[OrchestratorService, Depends(get_orchestrator)]
Checker = Annotated[UpdateCheckerService, Depends(get_checker)]


async def _view(request: Request, services: Services, app_id: int) -> AppView:
    app = await services.apps.get(app_id)
    return (await build_views(request, services, [app]))[0]


async def _respond(request: Request, services: Services, app_id: int, view: str) -> Response:
    """The card (dashboard) or the summary block (detail page) after an action."""
    if view == "detail":
        return render(request, "_summary.html", v=await _view(request, services, app_id))
    return render(request, "_card.html", v=await _view(request, services, app_id))


@router.get("/")
async def dashboard(request: Request, services: Services) -> Response:
    """One card per app."""
    views = await build_views(request, services, await services.apps.list())
    return render(request, "dashboard.html", views=views, active="dashboard")


@router.get("/fragments/cards")
async def cards_fragment(request: Request, services: Services) -> Response:
    """The card grid (polled by the dashboard)."""
    views = await build_views(request, services, await services.apps.list())
    return render(request, "_cards.html", views=views)


@router.get("/fragments/apps/{app_id}/card")
async def card_fragment(app_id: int, request: Request, services: Services) -> Response:
    """One card (polled while an operation is in progress)."""
    return await _respond(request, services, app_id, "card")


@router.get("/fragments/apps/{app_id}/summary")
async def summary_fragment(app_id: int, request: Request, services: Services) -> Response:
    """The detail page's status block."""
    return await _respond(request, services, app_id, "detail")


@router.post("/apps/{app_id}/uninstall")
async def uninstall(
    app_id: int, request: Request, services: Services, orchestrator: Orchestrator
) -> Response:
    """Remove the container (and optionally appdata), then go back to the dashboard."""
    await services.apps.get(app_id)
    form = await request.form()
    await orchestrator.uninstall(app_id, bool(form.get("delete_appdata")))
    return redirect(request, "/")


@router.post("/apps/{app_id}/{action}")
async def app_action(
    app_id: int,
    action: str,
    request: Request,
    background: BackgroundTasks,
    services: Services,
    orchestrator: Orchestrator,
    checker: Checker,
    view: Annotated[str, Query()] = "card",
) -> Response:
    """start / stop / restart / check run now; install / update run in the background."""
    await services.apps.get(app_id)
    if action == "start":
        await orchestrator.start(app_id)
    elif action == "stop":
        await orchestrator.stop(app_id)
    elif action == "restart":
        await orchestrator.restart(app_id)
    elif action == "check":
        await checker.check_now(app_id)
    elif action == "install":
        background.add_task(
            run_in_background(request, services, app_id, "install", lambda: orchestrator.install(app_id))
        )
    elif action == "update":
        form = await request.form()
        template = form_value(form, "apply_template", "1") == "1"
        image = form_value(form, "apply_image", "1") == "1"
        background.add_task(
            run_in_background(
                request, services, app_id, "update", lambda: orchestrator.update(app_id, template, image)
            )
        )
    else:
        raise ConflictError(f"unknown action {action}")
    return await _respond(request, services, app_id, view)


@router.get("/apps/{app_id}")
async def app_detail(app_id: int, request: Request, services: Services) -> Response:
    """Config summary, ports, update info, events and logs."""
    v = await _view(request, services, app_id)
    source = await services.sources.get(v.app.source_id)
    overrides = {k: shown_value(k, val) for k, val in sorted(v.app.overrides.items())}
    compare = ""
    if v.update_template and v.app.installed_template_commit:
        compare = (
            f"https://github.com/{source.owner}/{source.repo}/compare/"
            f"{v.app.installed_template_commit}...{v.app.available_template_commit}"
        )
    return render(
        request,
        "app_detail.html",
        v=v,
        source=source,
        overrides=overrides,
        compare_url=compare,
        active="dashboard",
    )


@router.get("/fragments/apps/{app_id}/events")
async def events_fragment(app_id: int, request: Request, services: Services) -> Response:
    """Event timeline of one app."""
    await services.apps.get(app_id)
    events = await services.events.recent(app_id, 50)
    return render(request, "_events.html", events=events, app_id=app_id)


@router.get("/fragments/apps/{app_id}/logs")
async def logs_fragment(
    app_id: int, request: Request, services: Services, tail: Annotated[int, Query(ge=1, le=2000)] = 200
) -> Response:
    """Last log lines of the container, secrets masked."""
    app = await services.apps.get(app_id)
    if not app.container_id:
        return render(request, "_logs.html", logs="(no container yet)", app_id=app_id)
    text = await services.docker.logs(app.container_id, tail)
    return render(request, "_logs.html", logs=redact(text) or "(no output)", app_id=app_id)
