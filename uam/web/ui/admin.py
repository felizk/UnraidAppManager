"""Credentials, settings and template export pages."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import ValidationError as PydanticValidationError

from uam.domain import CredentialKind
from uam.errors import ConflictError, UamError, ValidationError
from uam.logging import redact
from uam.settings import Settings
from uam.web.api.schemas import SettingsModel
from uam.web.deps import ApiServices, ExportPlan, pat_check
from uam.web.ui.support import (
    Services,
    UiRoute,
    error_text,
    form_value,
    optional_int,
    render,
    status_for,
)

router = APIRouter(route_class=UiRoute)


# ---------------------------------------------------------------- credentials
async def _credentials_page(
    request: Request,
    services: ApiServices,
    *,
    error: str = "",
    notice: str = "",
    values: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "credentials.html",
        status_code=status_code,
        credentials=await services.vault.list(),
        kinds=[k.value for k in CredentialKind],
        error=error,
        notice=notice,
        values=values or {},
        active="credentials",
    )


@router.get("/credentials")
async def credentials_page(request: Request, services: Services) -> Response:
    """Credential list and the add form (secrets are write-only)."""
    return await _credentials_page(request, services)


@router.post("/credentials")
async def add_credential(request: Request, services: Services) -> Response:
    """Store a credential; errors are shown above the form."""
    form = await request.form()
    values = {k: form_value(form, k) for k in ("name", "kind", "username", "registry_host")}
    secret = form_value(form, "secret")
    try:
        if not values["name"].strip():
            raise ValidationError("name is required")
        if not secret:
            raise ValidationError("secret is required")
        try:
            kind = CredentialKind(values["kind"])
        except ValueError:
            raise ValidationError("choose a credential kind") from None
        await services.vault.store(
            values["name"].strip(), kind, values["username"].strip(), secret, values["registry_host"].strip()
        )
    except UamError as exc:
        return await _credentials_page(
            request, services, error=error_text(exc), values=values, status_code=status_for(exc)
        )
    return await _credentials_page(request, services)


@router.post("/credentials/{credential_id}/update")
async def update_credential(credential_id: int, request: Request, services: Services) -> Response:
    """Replace a credential's username and/or secret; blank fields keep their value."""
    cred = await services.vault.get(credential_id)
    form = await request.form()
    username, secret = form_value(form, "username").strip(), form_value(form, "secret")
    if username and username != cred.username:
        await services.vault.update_username(credential_id, username)
    if secret:
        await services.vault.update_secret(credential_id, secret)
    changed = [
        what
        for what, new in (("username", username != cred.username and username), ("secret", secret))
        if new
    ]
    notice = f"Updated {' and '.join(changed)} of {cred.name}." if changed else "Nothing to change."
    return await _credentials_page(request, services, notice=notice)


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: int, services: Services) -> Response:
    """Delete a credential; its table row disappears."""
    await services.vault.get(credential_id)
    await services.vault.delete(credential_id)
    return Response(status_code=200)


@router.post("/credentials/{credential_id}/test")
async def test_credential(credential_id: int, request: Request, services: Services) -> Response:
    """Check a stored credential where that can be tested; shows the result inline."""
    cred = await services.vault.get(credential_id)
    ok, detail = True, "registry credentials are checked on first pull"
    try:
        if cred.kind == CredentialKind.GITHUB_PAT:
            token = await services.vault.reveal(credential_id)
            ok, detail = pat_check(token, cred.username, await services.github.validate_token(token))
        elif cred.kind == CredentialKind.NPM:
            if services.proxy is None:
                ok, detail = False, "Nginx Proxy Manager is not configured"
            else:
                await services.proxy.list_certificates()
                detail = "logged in to Nginx Proxy Manager"
    except UamError as exc:
        ok, detail = False, redact(str(exc))
    return render(request, "_test_result.html", ok=ok, detail=detail)


# ---------------------------------------------------------------- settings
_TEXT_FIELDS = ("appdata_root", "template_export_dir", "unraid_managed_label")
_OPTIONAL_TEXT = ("host_ip", "npm_url", "base_domain")
_INT_FIELDS = (
    "port_range_start",
    "port_range_end",
    "hot_window_hours",
    "hot_interval_minutes",
    "cold_interval_hours",
)
_OPTIONAL_INT = ("npm_credential_id", "default_certificate_id")


def _settings_values(s: Settings) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in s.to_dict().items():
        if isinstance(value, list):
            out[name] = ", ".join(str(v) for v in value)
        elif isinstance(value, bool):
            out[name] = "1" if value else ""
        else:
            out[name] = "" if value is None else str(value)
    return out


async def _settings_page(
    request: Request,
    services: ApiServices,
    values: dict[str, str],
    errors: dict[str, str],
    *,
    saved: bool = False,
    status_code: int = 200,
) -> Response:
    certs: list[tuple[int, str]] = []
    if services.proxy is not None:
        try:
            certs = await services.proxy.list_certificates()
        except UamError:
            certs = []
    return render(
        request,
        "settings.html",
        status_code=status_code,
        v=values,
        errors=errors,
        saved=saved,
        certs=certs,
        npm_credentials=[c for c in await services.vault.list() if c.kind == CredentialKind.NPM],
        active="settings",
    )


@router.get("/settings")
async def settings_page(request: Request, services: Services) -> Response:
    """Every setting in one form."""
    return await _settings_page(request, services, _settings_values(await services.settings.get()), {})


def _collect_settings(form: Any) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Convert the posted strings to typed values. Returns (data, raw values, errors)."""
    raw = {n: form_value(form, n) for n in (*_TEXT_FIELDS, *_OPTIONAL_TEXT, *_INT_FIELDS, *_OPTIONAL_INT)}
    raw["reserved_ports"] = form_value(form, "reserved_ports")
    raw["template_export_include_secrets"] = "1" if form.get("template_export_include_secrets") else ""
    errors: dict[str, str] = {}
    data: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        data[name] = raw[name].strip()
    for name in _OPTIONAL_TEXT:
        data[name] = raw[name].strip() or None
    for name in _INT_FIELDS:
        try:
            data[name] = int(raw[name])
        except ValueError:
            errors[name] = "must be a number"
    for name in _OPTIONAL_INT:
        try:
            data[name] = optional_int(raw[name], name)
        except ValidationError:
            errors[name] = "must be a number"
    ports: list[int] = []
    for part in raw["reserved_ports"].replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            errors["reserved_ports"] = "comma-separated port numbers"
            break
        ports.append(int(part))
    data["reserved_ports"] = ports
    data["template_export_include_secrets"] = bool(raw["template_export_include_secrets"])
    return data, raw, errors


@router.post("/settings")
async def save_settings(request: Request, services: Services) -> Response:
    """Validate with the same model as the REST API, then save."""
    data, raw, errors = _collect_settings(await request.form())
    if not errors:
        try:
            model = SettingsModel(**data)
        except PydanticValidationError as exc:
            for err in exc.errors():
                loc = str(err["loc"][0]) if err["loc"] else "_general"
                errors[loc] = str(err["msg"]).removeprefix("Value error, ")
        else:
            await services.settings.save(model.to_settings())
            return await _settings_page(
                request, services, _settings_values(model.to_settings()), {}, saved=True
            )
    return await _settings_page(request, services, raw, errors, status_code=422)


@router.post("/settings/test-npm")
async def test_npm(request: Request, services: Services) -> Response:
    """Try the saved Nginx Proxy Manager connection."""
    if services.proxy is None:
        return render(request, "_test_result.html", ok=False, detail="Nginx Proxy Manager is not configured")
    try:
        certs = await services.proxy.list_certificates()
    except UamError as exc:
        return render(request, "_test_result.html", ok=False, detail=error_text(exc))
    return render(
        request, "_test_result.html", ok=True, detail=f"connected; {len(certs)} certificate(s) available"
    )


# ---------------------------------------------------------------- export
def _digest(plans: list[ExportPlan]) -> str:
    h = hashlib.sha256()
    for p in plans:
        h.update(f"{p.app_id}|{p.path}|{p.changed}|{p.old_xml}|{p.new_xml}\n".encode())
    return h.hexdigest()


def _selected_ids(form: Any) -> list[int]:
    ids: list[int] = []
    for raw in form.getlist("app_ids"):
        if isinstance(raw, str) and raw.isdigit():
            ids.append(int(raw))
    if not ids:
        raise ValidationError("select at least one app")
    return ids


@router.get("/export")
async def export_page(request: Request, services: Services) -> Response:
    """Pick apps to export."""
    return render(request, "export.html", apps=await services.apps.list(), active="export")


@router.post("/export/preview")
async def export_preview(request: Request, services: Services) -> Response:
    """Show the diffs that would be written."""
    ids = _selected_ids(await request.form())
    plans = await services.exporter.plan(ids)
    settings = await services.settings.get()
    return render(
        request,
        "_export_preview.html",
        plans=plans,
        app_ids=ids,
        digest=_digest(plans),
        include_secrets=settings.template_export_include_secrets,
        any_changed=any(p.changed for p in plans),
    )


@router.post("/export/apply")
async def export_apply(request: Request, services: Services) -> Response:
    """Write the previewed templates to flash (refuses if they changed since the preview)."""
    form = await request.form()
    ids = _selected_ids(form)
    plans = await services.exporter.plan(ids)
    if _digest(plans) != form_value(form, "digest"):
        raise ConflictError("the templates changed since the preview; preview again")
    written = await services.exporter.apply(
        [p for p in plans if p.changed], bool(form.get("include_secrets"))
    )
    return render(request, "_export_done.html", written=written)
