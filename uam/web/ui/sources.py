"""Template sources: list, add (owner/repo or pasted URL), discover templates."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response

from uam.domain import CredentialKind, Source
from uam.errors import UamError, ValidationError
from uam.web.deps import ApiServices
from uam.web.ui.support import (
    Services,
    UiRoute,
    error_text,
    form_value,
    optional_int,
    redirect,
    render,
)

router = APIRouter(route_class=UiRoute)

_URL = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|github\.com/)"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>.+?))?)?/?$"
)
_SHORT = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?$")


def parse_repo(raw: str) -> tuple[str, str, str | None, str | None]:
    """Split `owner/repo` or a pasted GitHub URL into owner, repo, ref (if given) and a template path."""
    text = raw.strip()
    m = _URL.match(text) or _SHORT.match(text)
    if m is None:
        raise ValidationError("enter owner/repo or a GitHub URL such as https://github.com/owner/repo")
    groups = m.groupdict()
    path = groups.get("path")
    return (
        groups["owner"],
        groups["repo"],
        groups.get("ref"),
        path if path and path.endswith(".xml") else None,
    )


@router.get("/sources")
async def sources_page(request: Request, services: Services) -> Response:
    """Sources list plus the add form."""
    return render(
        request,
        "sources.html",
        sources=await services.sources.list(),
        credentials=[c for c in await services.vault.list() if c.kind == CredentialKind.GITHUB_PAT],
        credential_names={c.id: c.name for c in await services.vault.list()},
        active="sources",
    )


@router.post("/sources")
async def add_source(request: Request, services: Services) -> Response:
    """Add a source from the form."""
    form = await request.form()
    owner, repo, url_ref, url_path = parse_repo(form_value(form, "repo"))
    ref = form_value(form, "ref").strip() or url_ref or "main"
    path = form_value(form, "template_path").strip() or url_path
    credential_id = optional_int(form_value(form, "credential_id"), "credential")
    if credential_id is not None:
        await services.vault.get(credential_id)
    await services.sources.add(Source(0, owner, repo, ref, path or None, credential_id))
    return redirect(request, "/sources")


async def _source_row(request: Request, services: ApiServices, source: Source, template: str) -> Response:
    creds = await services.vault.list()
    return render(
        request,
        template,
        s=source,
        credentials=[c for c in creds if c.kind == CredentialKind.GITHUB_PAT],
        credential_names={c.id: c.name for c in creds},
    )


@router.get("/sources/{source_id}/row")
async def source_row(source_id: int, request: Request, services: Services) -> Response:
    """The plain table row (used by Cancel)."""
    return await _source_row(request, services, await services.sources.get(source_id), "_source_row.html")


@router.get("/sources/{source_id}/edit")
async def source_edit_row(source_id: int, request: Request, services: Services) -> Response:
    """The row as inline inputs for branch, template path and credential."""
    return await _source_row(
        request, services, await services.sources.get(source_id), "_source_edit_row.html"
    )


@router.post("/sources/{source_id}/update")
async def update_source(source_id: int, request: Request, services: Services) -> Response:
    """Change a source's branch, template path or credential; returns the updated row."""
    source = await services.sources.get(source_id)
    form = await request.form()
    credential_id = optional_int(form_value(form, "credential_id"), "credential")
    if credential_id is not None:
        await services.vault.get(credential_id)
    await services.sources.update(
        Source(
            source.id,
            source.owner,
            source.repo,
            form_value(form, "ref").strip() or source.ref,
            form_value(form, "template_path").strip() or None,
            credential_id,
        )
    )
    return await _source_row(request, services, await services.sources.get(source_id), "_source_row.html")


@router.delete("/sources/{source_id}")
async def delete_source(source_id: int, services: Services) -> Response:
    """Delete a source; the row disappears from the table."""
    await services.sources.get(source_id)
    await services.sources.delete(source_id)
    return Response(status_code=200)


async def _token(services: ApiServices, source: Source) -> str | None:
    if source.credential_id is None:
        return None
    return await services.vault.reveal(source.credential_id)


@router.get("/sources/{source_id}/templates")
async def discover(source_id: int, request: Request, services: Services) -> Response:
    """Templates found in the source, each with an Add app link."""
    source = await services.sources.get(source_id)
    found = await services.github.discover_templates(source, await _token(services, source))
    rows: list[dict[str, str]] = []
    for tpl in found:
        row = {"path": tpl.path, "name": tpl.path.rsplit("/", 1)[-1], "icon": "", "error": ""}
        try:
            parsed = services.codec.parse(tpl.xml)
        except UamError as exc:
            row["error"] = error_text(exc)
        else:
            row["name"], row["icon"] = parsed.name or row["name"], parsed.icon
        rows.append(row)
    return render(request, "_templates.html", source=source, rows=rows)
