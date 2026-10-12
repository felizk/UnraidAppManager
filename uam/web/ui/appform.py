"""Add-app and edit-app forms, generated from the template's configs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from uam.domain import App, ProxySettings, Source, TemplateConfig, UnraidTemplate
from uam.errors import UamError
from uam.web.deps import ApiServices, OrchestratorService
from uam.web.ui.support import (
    Services,
    UiRoute,
    error_text,
    form_value,
    redirect,
    render,
    run_in_background,
    status_for,
)

router = APIRouter(route_class=UiRoute)

GROUPS: tuple[tuple[str, str], ...] = (
    ("Port", "Ports"),
    ("Path", "Paths"),
    ("Variable", "Variables"),
    ("Label", "Labels"),
    ("Device", "Devices"),
)
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def override_key(c: TemplateConfig) -> str:
    """The key under which `c`'s override is stored (`Type:Target`, `Port:Target/proto` for ports)."""
    if c.type == "Port":
        return f"Port:{c.target}/{c.mode or 'tcp'}"
    return f"{c.type}:{c.target}"


def baseline(c: TemplateConfig) -> str:
    """The value used when there is no override."""
    return c.value or c.default


@dataclass
class FieldView:
    """One generated input."""

    key: str
    config: TemplateConfig
    value: str = ""
    error: str = ""
    placeholder: str = ""
    has_existing: bool = False

    @property
    def name(self) -> str:
        """The form field name."""
        return f"ov:{self.key}"

    @property
    def advanced(self) -> bool:
        """Shown in the collapsible section."""
        return self.config.display.startswith("advanced")


@dataclass
class FormState:
    """Current values of the non-template inputs."""

    name: str = ""
    subdomain: str = ""
    forward_mode: str = "host_port"
    certificate_id: str = ""
    access_list_id: str = ""
    ssl_forced: bool = True
    websockets: bool = True
    auto_ports: bool = True
    auto_update_image: bool = True
    auto_update_template: bool = False
    image_tag_override: str = ""
    install: bool = True
    errors: dict[str, str] = field(default_factory=dict)


async def _token(services: ApiServices, source: Source) -> str | None:
    if source.credential_id is None:
        return None
    return await services.vault.reveal(source.credential_id)


async def fetch_template(
    services: ApiServices, source_id: int, path: str
) -> tuple[Source, str, UnraidTemplate]:
    """Source, commit sha and parsed template for `path` at the source's ref."""
    source = await services.sources.get(source_id)
    found = await services.github.get_template(source, path, await _token(services, source))
    return source, found.commit_sha, services.codec.parse(found.xml)


async def next_free_ports(services: ApiServices, count: int) -> list[int]:
    """The next `count` host ports inside the configured range that are not in use or reserved."""
    settings = await services.settings.get()
    taken = {p for p, _ in await services.ports.in_use()} | set(settings.reserved_ports)
    taken |= {a.host_port for a in await services.ports.allocations()}
    found: list[int] = []
    for port in range(settings.port_range_start, settings.port_range_end + 1):
        if len(found) >= count:
            break
        if port not in taken:
            found.append(port)
    return found


async def proxy_choices(services: ApiServices) -> tuple[list[tuple[int, str]], list[tuple[int, str]], str]:
    """Certificates, access lists and a note (empty when NPM answered)."""
    if services.proxy is None:
        return [], [], "Nginx Proxy Manager is not configured (see Settings); IDs can still be typed in."
    try:
        return await services.proxy.list_certificates(), await services.proxy.list_access_lists(), ""
    except UamError as exc:
        return [], [], f"Could not read Nginx Proxy Manager: {error_text(exc)}"


def _state_from_app(app: App) -> FormState:
    p = app.proxy
    return FormState(
        name=app.name,
        subdomain=p.subdomain or "",
        forward_mode=p.forward_mode,
        certificate_id="" if p.certificate_id is None else str(p.certificate_id),
        access_list_id="" if p.access_list_id is None else str(p.access_list_id),
        ssl_forced=p.ssl_forced,
        websockets=p.websockets,
        auto_ports=app.auto_ports,
        auto_update_image=app.auto_update_image,
        auto_update_template=app.auto_update_template,
        image_tag_override=app.image_tag_override or "",
        install=False,
    )


async def _build_fields(
    services: ApiServices,
    tpl: UnraidTemplate,
    existing: dict[str, str],
    auto_ports: bool,
    submitted: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> list[FieldView]:
    port_count = sum(1 for c in tpl.configs if c.type == "Port")
    free = iter(await next_free_ports(services, port_count)) if auto_ports else iter(())
    fields: list[FieldView] = []
    for c in tpl.configs:
        key = override_key(c)
        view = FieldView(key=key, config=c, has_existing=key in existing)
        if c.type == "Port" and auto_ports:
            nxt = next(free, None)
            view.placeholder = f"auto (next free: {nxt})" if nxt is not None else "auto (range exhausted)"
        if submitted is not None and view.name in submitted:
            view.value = "" if c.mask else submitted[view.name]
        elif c.mask:
            view.value = ""
        elif key in existing:
            view.value = existing[key]
        elif c.type == "Port" and auto_ports:
            view.value = ""
        else:
            view.value = baseline(c)
        if errors and key in errors:
            view.error = errors[key]
        fields.append(view)
    return fields


async def _form_context(
    services: ApiServices,
    *,
    tpl: UnraidTemplate,
    state: FormState,
    existing: dict[str, str],
    submitted: dict[str, str] | None,
    action: str,
    source_id: int,
    template_path: str,
    app: App | None,
) -> dict[str, Any]:
    fields = await _build_fields(services, tpl, existing, state.auto_ports, submitted, state.errors)
    grouped = []
    for type_, title in GROUPS:
        members = [f for f in fields if f.config.type == type_]
        if members:
            grouped.append(
                (title, [f for f in members if not f.advanced], [f for f in members if f.advanced])
            )
    certs, lists, note = await proxy_choices(services)
    settings = await services.settings.get()
    return {
        "tpl": tpl,
        "state": state,
        "groups": grouped,
        "action": action,
        "source_id": source_id,
        "template_path": template_path,
        "app": app,
        "certs": certs,
        "access_lists": lists,
        "proxy_note": note,
        "base_domain": settings.base_domain or "",
        "active": "sources",
    }


@router.get("/fragments/subdomain-preview")
async def subdomain_preview(
    request: Request, services: Services, subdomain: Annotated[str, Query()] = ""
) -> Response:
    """The `subdomain.base_domain` shown under the subdomain input."""
    settings = await services.settings.get()
    return render(
        request,
        "_subdomain_preview.html",
        subdomain=subdomain.strip().lower(),
        base_domain=settings.base_domain or "",
    )


@router.get("/apps/new")
async def new_app_form(
    request: Request, services: Services, source: Annotated[int, Query()], path: Annotated[str, Query()]
) -> Response:
    """Empty add-app form for one template."""
    _, _, tpl = await fetch_template(services, source, path)
    state = FormState(name=tpl.name.lower().replace(" ", "-"))
    ctx = await _form_context(
        services,
        tpl=tpl,
        state=state,
        existing={},
        submitted=None,
        action="/apps/new",
        source_id=source,
        template_path=path,
        app=None,
    )
    return render(request, "app_form.html", **ctx)


@router.get("/apps/{app_id}/edit")
async def edit_app_form(app_id: int, request: Request, services: Services) -> Response:
    """Edit form pre-filled from the app; masked values are never sent to the browser."""
    app = await services.apps.get(app_id)
    _, _, tpl = await fetch_template(services, app.source_id, app.template_path)
    ctx = await _form_context(
        services,
        tpl=tpl,
        state=_state_from_app(app),
        existing=app.overrides,
        submitted=None,
        action=f"/apps/{app_id}/edit",
        source_id=app.source_id,
        template_path=app.template_path,
        app=app,
    )
    return render(request, "app_form.html", **ctx)


def _parse_id(raw: str, label: str, state: FormState, key: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        state.errors[key] = f"{label} must be a number"
        return None


async def _parse(
    services: ApiServices, form: Any, tpl: UnraidTemplate, existing: dict[str, str], editing: bool
) -> tuple[FormState, dict[str, str], ProxySettings, dict[str, str]]:
    """Read and validate the posted form. Returns state (with errors), overrides, proxy, raw fields."""
    state = FormState(
        name=form_value(form, "name").strip(),
        subdomain=form_value(form, "subdomain").strip().lower(),
        forward_mode=form_value(form, "forward_mode", "host_port"),
        certificate_id=form_value(form, "certificate_id"),
        access_list_id=form_value(form, "access_list_id"),
        ssl_forced=bool(form.get("ssl_forced")),
        websockets=bool(form.get("websockets")),
        auto_ports=bool(form.get("auto_ports")),
        auto_update_image=bool(form.get("auto_update_image")),
        auto_update_template=bool(form.get("auto_update_template")),
        image_tag_override=form_value(form, "image_tag_override").strip(),
        install=bool(form.get("install")),
    )
    raw = {k: v for k, v in form.items() if isinstance(v, str) and k.startswith("ov:")}
    errors = state.errors
    if not editing and state.name and not _NAME.match(state.name):
        errors["name"] = "use letters, digits, '.', '_' and '-' only"
    settings = await services.settings.get()
    if state.subdomain:
        if not _LABEL.match(state.subdomain):
            errors["subdomain"] = "use lowercase letters, digits and '-' (one DNS label)"
        elif not settings.base_domain:
            errors["subdomain"] = "set the base domain in Settings first"
    if state.forward_mode not in ("host_port", "container_network"):
        errors["forward_mode"] = "unknown forward mode"
    cert = _parse_id(state.certificate_id, "certificate", state, "certificate_id")
    acl = _parse_id(state.access_list_id, "access list", state, "access_list_id")

    overrides = {k: v for k, v in existing.items() if k not in {override_key(c) for c in tpl.configs}}
    for c in tpl.configs:
        key = override_key(c)
        if f"ov:{key}" not in raw:
            if key in existing:
                overrides[key] = existing[key]
            continue
        value = raw[f"ov:{key}"]
        if not c.mask:
            value = value.strip()
        have_existing = key in existing
        if c.mask and value == "":
            if have_existing:
                overrides[key] = existing[key]
            elif c.required and not baseline(c):
                errors[key] = "required"
            continue
        if c.type == "Port":
            if value == "":
                if not state.auto_ports and c.required and not baseline(c):
                    errors[key] = "required"
                continue
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                errors[key] = "must be a port number between 1 and 65535"
                continue
            overrides[key] = value
            continue
        if value == "" and c.required and not baseline(c):
            errors[key] = "required"
            continue
        if value != baseline(c):
            overrides[key] = value
    proxy = ProxySettings(
        subdomain=state.subdomain or None,
        forward_mode="container_network" if state.forward_mode == "container_network" else "host_port",
        certificate_id=cert,
        ssl_forced=state.ssl_forced,
        websockets=state.websockets,
        access_list_id=acl,
    )
    return state, overrides, proxy, raw


async def _rerender(
    request: Request, services: ApiServices, ctx_args: dict[str, Any], status_code: int = 422
) -> Response:
    ctx = await _form_context(services, **ctx_args)
    return render(request, "_app_form.html", status_code=status_code, **ctx)


@router.post("/apps/new")
async def create_app(request: Request, background: BackgroundTasks, services: Services) -> Response:
    """Validate, create the app and (optionally) start the install in the background."""
    form = await request.form()
    source_id = int(form_value(form, "source_id", "0") or 0)
    path = form_value(form, "template_path")
    _, _, tpl = await fetch_template(services, source_id, path)
    state, overrides, proxy, raw = await _parse(services, form, tpl, {}, editing=False)
    common: dict[str, Any] = {
        "tpl": tpl,
        "state": state,
        "existing": {},
        "submitted": raw,
        "action": "/apps/new",
        "source_id": source_id,
        "template_path": path,
        "app": None,
    }
    if state.errors:
        return await _rerender(request, services, common)
    orchestrator: OrchestratorService = services.orchestrator
    try:
        app = await orchestrator.add_app(
            source_id, path, state.name or None, overrides, proxy, state.auto_ports
        )
    except UamError as exc:
        state.errors["_general"] = error_text(exc)
        return await _rerender(request, services, common, status_for(exc))
    if (app.auto_update_image, app.auto_update_template) != (
        state.auto_update_image,
        state.auto_update_template,
    ):
        app.auto_update_image = state.auto_update_image
        app.auto_update_template = state.auto_update_template
        await services.apps.save(app)
    if state.install:
        background.add_task(
            run_in_background(request, services, app.id, "install", lambda: orchestrator.install(app.id))
        )
    return redirect(request, f"/apps/{app.id}")


@router.post("/apps/{app_id}/edit")
async def update_app(
    app_id: int, request: Request, background: BackgroundTasks, services: Services
) -> Response:
    """Validate and apply edits (flags at once, config changes by recreating the container)."""
    app = await services.apps.get(app_id)
    form = await request.form()
    _, _, tpl = await fetch_template(services, app.source_id, app.template_path)
    state, overrides, proxy, raw = await _parse(services, form, tpl, app.overrides, editing=True)
    state.name = app.name
    common: dict[str, Any] = {
        "tpl": tpl,
        "state": state,
        "existing": app.overrides,
        "submitted": raw,
        "action": f"/apps/{app_id}/edit",
        "source_id": app.source_id,
        "template_path": app.template_path,
        "app": app,
    }
    if state.errors:
        return await _rerender(request, services, common)
    orchestrator: OrchestratorService = services.orchestrator
    tag = state.image_tag_override or None
    flags = (state.auto_ports, state.auto_update_image, state.auto_update_template)
    if flags != (app.auto_ports, app.auto_update_image, app.auto_update_template):
        app.auto_ports, app.auto_update_image, app.auto_update_template = flags
        await services.apps.save(app)
    if overrides != app.overrides or proxy != app.proxy or tag != app.image_tag_override:
        background.add_task(
            run_in_background(
                request,
                services,
                app_id,
                "reconfigure",
                lambda: orchestrator.reconfigure(app_id, overrides, proxy, tag),
            )
        )
    return redirect(request, f"/apps/{app_id}")
