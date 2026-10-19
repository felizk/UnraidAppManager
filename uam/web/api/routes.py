"""The REST endpoints, split into a public router (health, auth) and a session-protected one."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from uam.domain import (
    CredentialKind,
    DiscoveredTemplate,
    ImportCandidate,
    ImportPlan,
    ProxySettings,
    Source,
)
from uam.errors import AuthError, ConflictError, UamError, ValidationError
from uam.logging import redact
from uam.web.api.errors import UamRoute
from uam.web.api.schemas import (
    AcceptedOut,
    AppCreate,
    AppOut,
    AppPatch,
    AppUpdateIn,
    AuthStatusOut,
    CredentialIn,
    CredentialOut,
    CredentialTestOut,
    CredentialUpdateIn,
    DiscoveredTemplateOut,
    ExportApplyOut,
    ExportApplyRequest,
    ExportPlanModel,
    ExportPlanRequest,
    HealthOut,
    ImportPlanRequest,
    LogsOut,
    NamedIdOut,
    OkOut,
    PasswordIn,
    PortAllocationOut,
    PortInUseOut,
    PortsOut,
    SettingsModel,
    SourceIn,
    SourceOut,
    SourceUpdateIn,
    TemplatePreviewOut,
    UpdateCheckOut,
)
from uam.web.auth import COOKIE_NAME, SESSION_MAX_AGE, AuthManager
from uam.web.deps import (
    ApiServices,
    OrchestratorService,
    TemplateExporterService,
    UpdateCheckerService,
    get_auth,
    get_checker,
    get_exporter,
    get_orchestrator,
    get_services,
    pat_check,
)
from uam.web.deps import app_logs as container_logs

log = logging.getLogger("uam.api")

try:
    __version__ = version("unraid-app-manager")
except PackageNotFoundError:
    __version__ = "0.0.0"


def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie(COOKIE_NAME, value, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", path="/")


async def _guarded(
    services: ApiServices, app_id: int | None, label: str, call: Callable[[], Awaitable[object]]
) -> None:
    """Run a long operation in the background; failures are logged and recorded, never raised."""
    try:
        await call()
    except Exception as exc:  # noqa: BLE001 - background task boundary
        message = redact(f"{label} failed: {exc}")
        log.error(message)
        try:
            await services.events.record(app_id, f"api_{label}_failed", message, "error")
        except Exception:  # noqa: BLE001
            log.exception("could not record background failure")


def _token_for(services: ApiServices, source: Source) -> Callable[[], Awaitable[str | None]]:
    async def token() -> str | None:
        if source.credential_id is None:
            return None
        return await services.vault.reveal(source.credential_id)

    return token


# =========================================================================== public
public = APIRouter(route_class=UamRoute)


@public.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """Liveness probe."""
    return HealthOut(version=__version__)


@public.get("/auth/status", response_model=AuthStatusOut)
async def auth_status(request: Request, auth: Annotated[AuthManager, Depends(get_auth)]) -> AuthStatusOut:
    """Whether first-run setup is needed and whether the caller is signed in."""
    authed = auth.bearer_valid(request) or await auth.session_valid(request.cookies.get(COOKIE_NAME))
    return AuthStatusOut(setup_required=not await auth.is_setup(), authenticated=authed)


@public.post("/setup", response_model=OkOut, status_code=201)
async def setup(
    body: PasswordIn, response: Response, auth: Annotated[AuthManager, Depends(get_auth)]
) -> OkOut:
    """First-run only: set the admin password and sign in."""
    await auth.set_password(body.password)
    _set_cookie(response, await auth.issue_session())
    return OkOut()


@public.post("/login", response_model=OkOut)
async def login(
    body: PasswordIn, response: Response, auth: Annotated[AuthManager, Depends(get_auth)]
) -> OkOut:
    """Check the password and set the signed session cookie."""
    if not await auth.is_setup():
        raise AuthError("setup required")
    if not await auth.verify_password(body.password):
        raise AuthError("invalid password")
    _set_cookie(response, await auth.issue_session())
    return OkOut()


@public.post("/logout", response_model=OkOut)
async def logout(response: Response) -> OkOut:
    """Clear the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/")
    return OkOut()


# =========================================================================== protected
protected = APIRouter(route_class=UamRoute)


# ---------------------------------------------------------------- settings
@protected.get("/settings", response_model=SettingsModel)
async def get_settings(services: Annotated[ApiServices, Depends(get_services)]) -> SettingsModel:
    """Current settings."""
    return SettingsModel.from_settings(await services.settings.get())


@protected.put("/settings", response_model=SettingsModel)
async def put_settings(
    body: SettingsModel, services: Annotated[ApiServices, Depends(get_services)]
) -> SettingsModel:
    """Replace the settings."""
    await services.settings.save(body.to_settings())
    return body


# ---------------------------------------------------------------- credentials
@protected.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(services: Annotated[ApiServices, Depends(get_services)]) -> list[CredentialOut]:
    """All credentials, without secrets."""
    return [CredentialOut.from_domain(c) for c in await services.vault.list()]


@protected.post("/credentials", response_model=CredentialOut, status_code=201)
async def create_credential(
    body: CredentialIn, services: Annotated[ApiServices, Depends(get_services)]
) -> CredentialOut:
    """Store a credential; the secret is write-only."""
    cred = await services.vault.store(body.name, body.kind, body.username, body.secret, body.registry_host)
    return CredentialOut.from_domain(cred)


@protected.patch("/credentials/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: int, body: CredentialUpdateIn, services: Annotated[ApiServices, Depends(get_services)]
) -> CredentialOut:
    """Replace a credential's username and/or secret (e.g. after rotating a token)."""
    await services.vault.get(credential_id)
    if body.username is not None:
        await services.vault.update_username(credential_id, body.username)
    if body.secret is not None:
        await services.vault.update_secret(credential_id, body.secret)
    return CredentialOut.from_domain(await services.vault.get(credential_id))


@protected.delete("/credentials/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: int, services: Annotated[ApiServices, Depends(get_services)]
) -> Response:
    """Delete a credential."""
    await services.vault.get(credential_id)
    await services.vault.delete(credential_id)
    return Response(status_code=204)


@protected.post("/credentials/{credential_id}/test", response_model=CredentialTestOut)
async def test_credential(
    credential_id: int, services: Annotated[ApiServices, Depends(get_services)]
) -> CredentialTestOut:
    """Check that a stored credential works against its service where that can be tested."""
    cred = await services.vault.get(credential_id)
    try:
        if cred.kind == CredentialKind.GITHUB_PAT:
            token = await services.vault.reveal(credential_id)
            scopes = await services.github.validate_token(token)
            ok, detail = pat_check(token, cred.username, scopes)
            return CredentialTestOut(ok=ok, scopes=scopes, detail=detail)
        if cred.kind == CredentialKind.NPM:
            if services.proxy is None:
                return CredentialTestOut(ok=False, detail="Nginx Proxy Manager is not configured")
            await services.proxy.list_certificates()
            return CredentialTestOut(ok=True, detail="logged in to Nginx Proxy Manager")
        if cred.kind == CredentialKind.PUSHOVER:
            if services.notifier is None:
                return CredentialTestOut(ok=False, detail="notifications are not available in this mode")
            await services.notifier.send_test(credential_id)
            return CredentialTestOut(ok=True, detail="test notification sent")
    except UamError as exc:
        return CredentialTestOut(ok=False, detail=redact(str(exc)))
    return CredentialTestOut(ok=True, tested=False, detail="registry credentials are checked on first pull")


# ---------------------------------------------------------------- sources
@protected.get("/sources", response_model=list[SourceOut])
async def list_sources(services: Annotated[ApiServices, Depends(get_services)]) -> list[SourceOut]:
    """All template sources."""
    return [SourceOut.from_domain(s) for s in await services.sources.list()]


@protected.post("/sources", response_model=SourceOut, status_code=201)
async def create_source(body: SourceIn, services: Annotated[ApiServices, Depends(get_services)]) -> SourceOut:
    """Add a GitHub repo as a template source."""
    if body.credential_id is not None:
        await services.vault.get(body.credential_id)
    source = await services.sources.add(
        Source(0, body.owner, body.repo, body.ref, body.template_path, body.credential_id)
    )
    return SourceOut.from_domain(source)


@protected.patch("/sources/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: int, body: SourceUpdateIn, services: Annotated[ApiServices, Depends(get_services)]
) -> SourceOut:
    """Change a source's branch, template path or credential."""
    source = await services.sources.get(source_id)
    sent = body.model_fields_set
    if "credential_id" in sent and body.credential_id is not None:
        await services.vault.get(body.credential_id)
    source = replace(
        source,
        ref=body.ref if "ref" in sent and body.ref else source.ref,
        template_path=(body.template_path or None) if "template_path" in sent else source.template_path,
        credential_id=body.credential_id if "credential_id" in sent else source.credential_id,
    )
    await services.sources.update(source)
    return SourceOut.from_domain(await services.sources.get(source_id))


@protected.delete("/sources/{source_id}", status_code=204)
async def delete_source(source_id: int, services: Annotated[ApiServices, Depends(get_services)]) -> Response:
    """Delete a source (409 while apps still use it)."""
    await services.sources.get(source_id)
    await services.sources.delete(source_id)
    return Response(status_code=204)


def _discovered_out(services: ApiServices, tpl: DiscoveredTemplate) -> DiscoveredTemplateOut:
    out = DiscoveredTemplateOut(
        path=tpl.path, sha=tpl.sha, commit_sha=tpl.commit_sha, committed_at=tpl.committed_at
    )
    try:
        parsed = services.codec.parse(tpl.xml)
    except UamError as exc:
        out.parse_error = str(exc)
    else:
        out.name, out.repository, out.icon = parsed.name, parsed.repository, parsed.icon
    return out


@protected.get("/sources/{source_id}/templates", response_model=list[DiscoveredTemplateOut])
async def discover_templates(
    source_id: int, services: Annotated[ApiServices, Depends(get_services)]
) -> list[DiscoveredTemplateOut]:
    """Templates found in the source repo."""
    source = await services.sources.get(source_id)
    token = await _token_for(services, source)()
    return [_discovered_out(services, t) for t in await services.github.discover_templates(source, token)]


@protected.get("/sources/{source_id}/templates/preview", response_model=TemplatePreviewOut)
async def preview_template(
    source_id: int,
    path: Annotated[str, Query(min_length=1)],
    services: Annotated[ApiServices, Depends(get_services)],
) -> TemplatePreviewOut:
    """Parsed template and config list for the add-app form."""
    source = await services.sources.get(source_id)
    tpl = await services.github.get_template(source, path, await _token_for(services, source)())
    return TemplatePreviewOut.from_template(tpl.path, tpl.commit_sha, services.codec.parse(tpl.xml))


# ---------------------------------------------------------------- apps
@protected.get("/apps", response_model=list[AppOut])
async def list_apps(services: Annotated[ApiServices, Depends(get_services)]) -> list[AppOut]:
    """All apps."""
    return [AppOut.from_domain(a) for a in await services.apps.list()]


@protected.post("/apps", response_model=AcceptedOut, status_code=202)
async def create_app(
    body: AppCreate,
    response: Response,
    background: BackgroundTasks,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AcceptedOut:
    """Add an app. With `install` (default) the install runs in the background and this returns 202."""
    app = await orchestrator.add_app(
        body.source_id, body.template_path, body.name, body.overrides, body.proxy.to_domain(), body.auto_ports
    )
    if body.install:
        background.add_task(_guarded, services, app.id, "install", lambda: orchestrator.install(app.id))
    else:
        response.status_code = 201
    return AcceptedOut(app_id=app.id)


@protected.get("/apps/{app_id}", response_model=AppOut)
async def get_app(app_id: int, services: Annotated[ApiServices, Depends(get_services)]) -> AppOut:
    """One app."""
    return AppOut.from_domain(await services.apps.get(app_id))


@protected.patch("/apps/{app_id}", response_model=AppOut | AcceptedOut)
async def patch_app(
    app_id: int,
    body: AppPatch,
    response: Response,
    background: BackgroundTasks,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AppOut | AcceptedOut:
    """Edit an app. Flag changes apply at once (200); changes to overrides, proxy or image tag recreate
    the container in the background (202)."""
    app = await services.apps.get(app_id)
    fields = body.model_fields_set
    for name in ("auto_ports", "auto_update_image", "auto_update_template"):
        value = getattr(body, name)
        if name in fields and value is not None:
            setattr(app, name, value)
    if fields & {"auto_ports", "auto_update_image", "auto_update_template"}:
        await services.apps.save(app)
    if fields & {"overrides", "proxy", "image_tag_override"}:
        proxy: ProxySettings | None = body.proxy.to_domain() if body.proxy is not None else None
        background.add_task(
            _guarded,
            services,
            app_id,
            "reconfigure",
            lambda: orchestrator.reconfigure(app_id, body.overrides, proxy, body.image_tag_override),
        )
        response.status_code = 202
        return AcceptedOut(app_id=app_id)
    return AppOut.from_domain(app)


@protected.delete("/apps/{app_id}", status_code=204)
async def delete_app(
    *,
    app_id: int,
    delete_appdata: bool = False,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> Response:
    """Uninstall an app, optionally deleting its appdata directory."""
    await services.apps.get(app_id)
    await orchestrator.uninstall(app_id, delete_appdata)
    return Response(status_code=204)


@protected.post("/apps/{app_id}/install", response_model=AcceptedOut, status_code=202)
async def install_app(
    app_id: int,
    background: BackgroundTasks,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AcceptedOut:
    """Install in the background."""
    await services.apps.get(app_id)
    background.add_task(_guarded, services, app_id, "install", lambda: orchestrator.install(app_id))
    return AcceptedOut(app_id=app_id)


@protected.post("/apps/{app_id}/update", response_model=AcceptedOut, status_code=202)
async def update_app(
    *,
    app_id: int,
    background: BackgroundTasks,
    body: AppUpdateIn | None = None,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AcceptedOut:
    """Update in the background (template, image, or both)."""
    await services.apps.get(app_id)
    options = body or AppUpdateIn()
    background.add_task(
        _guarded,
        services,
        app_id,
        "update",
        lambda: orchestrator.update(app_id, options.apply_template, options.apply_image),
    )
    return AcceptedOut(app_id=app_id)


async def _lifecycle(
    app_id: int, services: ApiServices, action: Callable[[int], Awaitable[object]]
) -> AppOut:
    await services.apps.get(app_id)
    await action(app_id)
    return AppOut.from_domain(await services.apps.get(app_id))


@protected.post("/apps/{app_id}/start", response_model=AppOut)
async def start_app(
    app_id: int,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AppOut:
    """Start the container."""
    return await _lifecycle(app_id, services, orchestrator.start)


@protected.post("/apps/{app_id}/stop", response_model=AppOut)
async def stop_app(
    app_id: int,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AppOut:
    """Stop the container."""
    return await _lifecycle(app_id, services, orchestrator.stop)


@protected.post("/apps/{app_id}/restart", response_model=AppOut)
async def restart_app(
    app_id: int,
    services: Annotated[ApiServices, Depends(get_services)],
    orchestrator: Annotated[OrchestratorService, Depends(get_orchestrator)],
) -> AppOut:
    """Restart the container."""
    return await _lifecycle(app_id, services, orchestrator.restart)


@protected.post("/apps/{app_id}/check", response_model=UpdateCheckOut)
async def check_app(
    app_id: int,
    services: Annotated[ApiServices, Depends(get_services)],
    checker: Annotated[UpdateCheckerService, Depends(get_checker)],
) -> UpdateCheckOut:
    """Run an update check now."""
    await services.apps.get(app_id)
    r = await checker.check_now(app_id)
    return UpdateCheckOut(
        app_id=r.app_id,
        template_changed=r.template_changed,
        new_template_commit=r.new_template_commit,
        image_changed=r.image_changed,
        new_image_digest=r.new_image_digest,
        needs_attention=r.needs_attention,
        checked_at=r.checked_at,
    )


@protected.get("/apps/{app_id}/logs", response_model=LogsOut)
async def app_logs(
    *,
    app_id: int,
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
    services: Annotated[ApiServices, Depends(get_services)],
) -> LogsOut:
    """Last `tail` log lines of the app's container, with token-like strings masked."""
    app = await services.apps.get(app_id)
    text = await container_logs(services, app, tail)
    if text is None:
        raise ConflictError("app has no container (see its events for the output of failed attempts)")
    return LogsOut(app_id=app_id, tail=tail, logs=redact(text))


@protected.get("/apps/{app_id}/events", response_model=list[dict[str, Any]])
async def app_events(
    *,
    app_id: int,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    services: Annotated[ApiServices, Depends(get_services)],
) -> list[dict[str, Any]]:
    """Recent events of one app."""
    await services.apps.get(app_id)
    return await services.events.recent(app_id, limit)


@protected.get("/events", response_model=list[dict[str, Any]])
async def all_events(
    *,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    services: Annotated[ApiServices, Depends(get_services)],
) -> list[dict[str, Any]]:
    """Recent events across all apps."""
    return await services.events.recent(None, limit)


# ---------------------------------------------------------------- ports / npm
@protected.get("/ports", response_model=PortsOut)
async def get_ports(services: Annotated[ApiServices, Depends(get_services)]) -> PortsOut:
    """Port allocations plus every host port currently in use."""
    settings = await services.settings.get()
    in_use = sorted(await services.ports.in_use())
    return PortsOut(
        range_start=settings.port_range_start,
        range_end=settings.port_range_end,
        reserved=list(settings.reserved_ports),
        allocations=[PortAllocationOut.from_domain(a) for a in await services.ports.allocations()],
        in_use=[PortInUseOut(host_port=p, protocol=proto) for p, proto in in_use],
    )


def _proxy(services: ApiServices) -> Any:
    if services.proxy is None:
        raise ValidationError("Nginx Proxy Manager is not configured (see Settings)")
    return services.proxy


@protected.get("/npm/certificates", response_model=list[NamedIdOut])
async def npm_certificates(services: Annotated[ApiServices, Depends(get_services)]) -> list[NamedIdOut]:
    """Certificates known to Nginx Proxy Manager."""
    return [NamedIdOut(id=i, name=n) for i, n in await _proxy(services).list_certificates()]


@protected.get("/npm/access-lists", response_model=list[NamedIdOut])
async def npm_access_lists(services: Annotated[ApiServices, Depends(get_services)]) -> list[NamedIdOut]:
    """Access lists known to Nginx Proxy Manager."""
    return [NamedIdOut(id=i, name=n) for i, n in await _proxy(services).list_access_lists()]


# ---------------------------------------------------------------- template export
@protected.post("/templates/export/plan", response_model=list[ExportPlanModel])
async def export_plan(
    *,
    body: ExportPlanRequest | None = None,
    exporter: Annotated[TemplateExporterService, Depends(get_exporter)],
) -> list[ExportPlanModel]:
    """Preview the flash templates that would be written (diff included)."""
    plans = await exporter.plan(body.app_ids if body else None)
    return [ExportPlanModel.from_domain(p) for p in plans]


@protected.post("/templates/export/apply", response_model=ExportApplyOut)
async def export_apply(
    body: ExportApplyRequest, exporter: Annotated[TemplateExporterService, Depends(get_exporter)]
) -> ExportApplyOut:
    """Write previously planned templates (409 if a file changed since the plan)."""
    written = await exporter.apply([p.to_domain() for p in body.plans], body.include_secrets)
    return ExportApplyOut(written=written)


# ---------------------------------------------------------------- imports
@protected.get("/imports/candidates", response_model=list[ImportCandidate])
async def import_candidates(services: Annotated[ApiServices, Depends(get_services)]) -> list[ImportCandidate]:
    """Running containers that could be imported."""
    return await services.importer.scan()


@protected.post("/imports/plan", response_model=ImportPlan)
async def import_plan(
    body: ImportPlanRequest, services: Annotated[ApiServices, Depends(get_services)]
) -> ImportPlan:
    """Build an import plan for review."""
    return await services.importer.plan(
        body.container_name, body.source_id, body.new_source, body.template_path, body.options
    )


@protected.post("/imports/apply", response_model=AcceptedOut, status_code=202)
async def import_apply(
    plan: ImportPlan, services: Annotated[ApiServices, Depends(get_services)]
) -> AcceptedOut:
    """Apply a plan returned by /imports/plan. The importer creates the app row, so this awaits it."""
    app = await services.importer.apply(plan)
    return AcceptedOut(app_id=app.id)
