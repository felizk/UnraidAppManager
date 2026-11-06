"""Template sources: list, add (owner/repo or pasted URL), discover templates."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Request, Response

from uam.domain import Credential, CredentialKind, Source, normalize_forge_url
from uam.errors import UamError, ValidationError
from uam.services.source_move import MovePlan, SourceMover
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
_SSH = re.compile(
    r"^(?:ssh://)?[\w.-]+@(?P<host>[\w.-]+)(?::\d+)?[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_NAME = re.compile(r"^[\w.-]+$")
_FORGE_VIEWS = ("src", "raw", "tree", "blob")
_FORGE_REF_KINDS = ("branch", "tag", "commit")

RepoRef = tuple[str, str, str | None, str | None, str]


def _xml_path(path: str | None) -> str | None:
    return path if path and path.endswith(".xml") else None


def _parse_forge_url(text: str) -> RepoRef | None:
    """Split a Forgejo/Gitea web URL (`https://host[/prefix]/owner/repo[/src/branch/ref/path]`)."""
    if "://" not in text:
        first = text.split("/", 1)[0]
        if "." not in first and ":" not in first:
            return None
        text = f"https://{text}"
    scheme, rest = text.split("://", 1)
    if scheme not in ("http", "https"):
        return None
    host, _, tail = rest.partition("/")
    parts = [p for p in tail.split("/") if p]
    view = next((i for i, p in enumerate(parts) if p in _FORGE_VIEWS and i >= 2), len(parts))
    if view < 2:
        return None
    prefix, (owner, repo) = parts[: view - 2], parts[view - 2 : view]
    repo = repo.removesuffix(".git")
    if not (_NAME.match(owner) and _NAME.match(repo)):
        return None
    ref: str | None = None
    path: str | None = None
    after = parts[view + 1 :]
    if after and after[0] in _FORGE_REF_KINDS:
        after = after[1:]
    if after:
        ref, path = after[0], "/".join(after[1:]) or None
    forge_url = "/".join([f"{scheme}://{host}", *prefix])
    return owner, repo, ref, _xml_path(path), forge_url


def parse_repo(raw: str) -> RepoRef:
    """Split `owner/repo` or a pasted GitHub/Forgejo URL into owner, repo, ref, template path, forge URL.

    The forge URL is empty for GitHub.
    """
    text = raw.strip()
    m = _URL.match(text) or _SHORT.match(text)
    if m is not None:
        g = m.groupdict()
        return g["owner"], g["repo"], g.get("ref"), _xml_path(g.get("path")), ""
    ssh = _SSH.match(text)
    if ssh is not None:
        return ssh["owner"], ssh["repo"], None, None, f"https://{ssh['host']}"
    parsed = _parse_forge_url(text)
    if parsed is None:
        raise ValidationError(
            "enter owner/repo, a GitHub URL such as https://github.com/owner/repo, "
            "or a Forgejo URL such as https://git.example.com/owner/repo"
        )
    return parsed


def _token_credentials(creds: list[Credential]) -> list[Credential]:
    return [c for c in creds if c.kind in (CredentialKind.GITHUB_PAT, CredentialKind.FORGEJO_TOKEN)]


@router.get("/sources")
async def sources_page(request: Request, services: Services) -> Response:
    """Sources list plus the add form."""
    return render(
        request,
        "sources.html",
        sources=await services.sources.list(),
        credentials=_token_credentials(await services.vault.list()),
        credential_names={c.id: c.name for c in await services.vault.list()},
        active="sources",
    )


@router.post("/sources")
async def add_source(request: Request, services: Services) -> Response:
    """Add a source from the form."""
    form = await request.form()
    owner, repo, url_ref, url_path, forge_url = parse_repo(form_value(form, "repo"))
    ref = form_value(form, "ref").strip() or url_ref or "main"
    path = form_value(form, "template_path").strip() or url_path
    credential_id = optional_int(form_value(form, "credential_id"), "credential")
    if credential_id is not None:
        await services.vault.get(credential_id)
    await services.sources.add(Source(0, owner, repo, ref, path or None, credential_id, forge_url))
    return redirect(request, "/sources")


def _mover(services: ApiServices) -> SourceMover:
    return SourceMover(services.sources, services.apps, services.github, services.vault)


async def _move_all_plans(services: ApiServices, form: Any) -> tuple[list[MovePlan], dict[str, str]]:
    forge_url = normalize_forge_url(form_value(form, "forge_url"))
    if not forge_url:
        raise ValidationError("enter the Forgejo URL, such as https://git.example.com")
    owner = form_value(form, "owner").strip()
    credential_id = optional_int(form_value(form, "credential_id"), "credential")
    mover = _mover(services)
    plans = [
        await mover.plan(
            s.id, replace(s, forge_url=forge_url, owner=owner or s.owner, credential_id=credential_id)
        )
        for s in await services.sources.list()
        if not s.forge_url
    ]
    fields = {"forge_url": forge_url, "owner": owner, "credential_id": str(credential_id or "")}
    return plans, fields


@router.post("/sources/move-all/check")
async def move_all_check(request: Request, services: Services) -> Response:
    """Preview moving every GitHub source to the same-named repo on a Forgejo server."""
    form = await request.form()
    try:
        plans, fields = await _move_all_plans(services, form)
    except UamError as exc:
        return render(request, "_move_preview.html", plans=[], error=error_text(exc))
    return render(request, "_move_preview.html", plans=plans, action="/sources/move-all", fields=fields)


@router.post("/sources/move-all")
async def move_all(request: Request, services: Services) -> Response:
    """Move every GitHub source whose checks pass; the others stay on GitHub."""
    plans, _ = await _move_all_plans(services, await request.form())
    for plan in plans:
        if plan.ok:
            await services.sources.update(plan.target)
    return redirect(request, "/sources")


async def _move_target(services: ApiServices, source: Source, form: Any) -> tuple[Source, dict[str, str]]:
    raw = form_value(form, "repo")
    owner, repo, url_ref, url_path, forge_url = parse_repo(raw)
    ref = form_value(form, "ref").strip() or url_ref or source.ref
    credential_id = optional_int(form_value(form, "credential_id"), "credential")
    target = replace(
        source,
        owner=owner,
        repo=repo,
        forge_url=forge_url,
        ref=ref,
        template_path=url_path or source.template_path,
        credential_id=credential_id,
    )
    return target, {"repo": raw, "ref": ref, "credential_id": str(credential_id or "")}


@router.get("/sources/{source_id}/move")
async def move_form(source_id: int, request: Request, services: Services) -> Response:
    """The form for moving one source to another repo."""
    return render(
        request,
        "_move_form.html",
        source=await services.sources.get(source_id),
        credentials=_token_credentials(await services.vault.list()),
    )


@router.post("/sources/{source_id}/move/check")
async def move_check(source_id: int, request: Request, services: Services) -> Response:
    """Preview moving one source: is every template its apps use present at the target?"""
    source = await services.sources.get(source_id)
    try:
        target, fields = await _move_target(services, source, await request.form())
        plan = await _mover(services).plan(source_id, target)
    except UamError as exc:
        return render(request, "_move_preview.html", plans=[], error=error_text(exc))
    return render(
        request, "_move_preview.html", plans=[plan], action=f"/sources/{source_id}/move", fields=fields
    )


@router.post("/sources/{source_id}/move")
async def move_source(source_id: int, request: Request, services: Services) -> Response:
    """Apply a move after re-checking it."""
    source = await services.sources.get(source_id)
    target, _ = await _move_target(services, source, await request.form())
    await _mover(services).move(source_id, target)
    return redirect(request, "/sources")


async def _source_row(request: Request, services: ApiServices, source: Source, template: str) -> Response:
    creds = await services.vault.list()
    return render(
        request,
        template,
        s=source,
        credentials=_token_credentials(creds),
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
            source.forge_url,
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
