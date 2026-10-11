"""Import existing containers: candidate table, per-container wizard and bulk import."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response

from uam.domain import (
    ImportCandidate,
    ImportOptions,
    ImportPlan,
    NewSource,
    Source,
    SourceMatch,
)
from uam.errors import NotFound, UamError, ValidationError
from uam.web.deps import ApiServices
from uam.web.ui.appform import override_key
from uam.web.ui.support import (
    Services,
    UiRoute,
    error_text,
    form_value,
    optional_int,
    render,
    shown_value,
)

router = APIRouter(route_class=UiRoute)

_RANK = {"high": 0, "medium": 1, "low": 2}
_MAX_PLANS = 50


def best_match(candidate: ImportCandidate) -> SourceMatch | None:
    """The match with the highest confidence (first one wins on ties)."""
    if not candidate.source_matches:
        return None
    return min(candidate.source_matches, key=lambda m: _RANK[m.confidence])


def bulk_eligible(candidate: ImportCandidate) -> bool:
    """High-confidence match with a known template and nothing blocking."""
    m = best_match(candidate)
    return bool(m and m.confidence == "high" and m.template_path and not candidate.blockers)


async def _candidate(services: ApiServices, name: str) -> ImportCandidate:
    for c in await services.importer.scan():
        if c.container_name == name:
            return c
    raise NotFound(f"container {name}")


async def _subdomain_hint(services: ApiServices, c: ImportCandidate) -> str:
    if c.proxy_host is None or not c.proxy_host.domain_names:
        return ""
    domain = c.proxy_host.domain_names[0]
    base = (await services.settings.get()).base_domain
    if base and domain.endswith("." + base):
        return domain.removesuffix("." + base)
    return domain


# ---------------------------------------------------------------- plan store
def _store(request: Request) -> dict[str, list[ImportPlan]]:
    state = request.app.state
    if not hasattr(state, "ui_plans"):
        state.ui_plans = {}
    plans: dict[str, list[ImportPlan]] = state.ui_plans
    return plans


def _keep(request: Request, plans: list[ImportPlan]) -> str:
    store = _store(request)
    token = secrets.token_urlsafe(16)
    store[token] = plans
    while len(store) > _MAX_PLANS:
        store.pop(next(iter(store)))
    return token


def _take(request: Request, token: str) -> list[ImportPlan]:
    plans = _store(request).pop(token, None)
    if plans is None:
        raise NotFound("this import review has expired; start the import again")
    return plans


# ---------------------------------------------------------------- page
@router.get("/import")
async def import_page(request: Request, services: Services, select: Annotated[str, Query()] = "") -> Response:
    """Candidate containers with their best source match."""
    rows = []
    for c in await services.importer.scan():
        rows.append(
            {
                "c": c,
                "best": best_match(c),
                "subdomain": await _subdomain_hint(services, c),
                "eligible": bulk_eligible(c),
                "selected": select == "high" and bulk_eligible(c),
            }
        )
    return render(request, "import.html", rows=rows, active="import")


# ---------------------------------------------------------------- wizard
@dataclass
class SourceChoice:
    """Step 1 result, decoded from the form."""

    source_id: int | None
    new_source: NewSource | None
    template_path: str | None


def _decode_source(form: Any, candidate: ImportCandidate, sources: list[Source]) -> SourceChoice:
    choice = form_value(form, "source_choice")
    path = form_value(form, "template_path").strip() or None
    if choice.startswith("match:"):
        try:
            m = candidate.source_matches[int(choice[6:])]
        except (ValueError, IndexError):
            raise ValidationError("choose a source") from None
        if m.source_id is not None:
            return SourceChoice(m.source_id, None, path or m.template_path)
        return SourceChoice(None, NewSource(m.owner, m.repo), path or m.template_path)
    if choice.startswith("source:"):
        sid = optional_int(choice[7:], "source")
        if sid is None or all(s.id != sid for s in sources):
            raise ValidationError("choose a source")
        return SourceChoice(sid, None, path)
    if choice == "new":
        owner, repo = form_value(form, "new_owner").strip(), form_value(form, "new_repo").strip()
        if not owner or not repo:
            raise ValidationError("owner and repo are required for a new source")
        cred = optional_int(form_value(form, "new_credential_id"), "credential")
        return SourceChoice(
            None, NewSource(owner, repo, form_value(form, "new_ref").strip() or "main", cred), path
        )
    raise ValidationError("choose a source")


def _carry(form: Any) -> dict[str, str]:
    keys = (
        "source_choice",
        "template_path",
        "new_owner",
        "new_repo",
        "new_ref",
        "new_credential_id",
    )
    return {k: form_value(form, k) for k in keys}


async def _step1(
    request: Request,
    services: ApiServices,
    c: ImportCandidate,
    values: dict[str, str],
    error: str = "",
    status_code: int = 200,
) -> Response:
    sources = await services.sources.list()
    matched_ids = {m.source_id for m in c.source_matches}
    first = best_match(c)
    guess_owner, guess_repo = (first.owner, first.repo) if first else _guess_from_image(c.image)
    defaults = {
        "source_choice": "match:0" if c.source_matches else "new",
        "template_path": first.template_path or "" if first else "",
        "new_owner": guess_owner,
        "new_repo": guess_repo,
        "new_ref": "main",
    }
    creds = [x for x in await services.vault.list() if x.kind.value == "github_pat"]
    return render(
        request,
        "_import_step1.html",
        status_code=status_code,
        c=c,
        v={**defaults, **{k: x for k, x in values.items() if x}},
        other_sources=[s for s in sources if s.id not in matched_ids],
        credentials=creds,
        error=error,
    )


def _guess_from_image(image: str) -> tuple[str, str]:
    parts = image.split("@")[0].rsplit(":", 1)[0].split("/")
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


@router.get("/import/wizard/{name}")
async def wizard_start(name: str, request: Request, services: Services) -> Response:
    """Step 1: choose the source and template."""
    return await _step1(request, services, await _candidate(services, name), {})


@router.post("/import/wizard/{name}/options")
async def wizard_options(name: str, request: Request, services: Services) -> Response:
    """Step 2: options (after validating step 1)."""
    form = await request.form()
    c = await _candidate(services, name)
    try:
        choice = _decode_source(form, c, await services.sources.list())
        if not choice.template_path:
            raise ValidationError("choose the template to import from")
    except ValidationError as exc:
        return await _step1(request, services, c, _carry(form), error_text(exc), status_code=422)
    return render(
        request,
        "_import_step2.html",
        c=c,
        carry=_carry(form),
        subdomain=await _subdomain_hint(services, c),
        has_proxy_host=c.proxy_host is not None,
    )


def _options(form: Any) -> ImportOptions:
    return ImportOptions(
        keep_host_ports=bool(form.get("keep_host_ports")),
        claim_proxy_host=bool(form.get("claim_proxy_host")),
        subdomain=form_value(form, "subdomain").strip() or None,
        recreate_now=bool(form.get("recreate_now")),
        auto_update_image=bool(form.get("auto_update_image")),
        auto_update_template=bool(form.get("auto_update_template")),
        archive_flash_template=bool(form.get("archive_flash_template")),
    )


def _masked_keys(plans: list[ImportPlan]) -> set[str]:
    keys: set[str] = set()
    for p in plans:
        tpl = p.candidate.flash_template
        if tpl is not None:
            keys |= {override_key(cfg) for cfg in tpl.configs if cfg.mask}
    return keys


def _review_rows(plans: list[ImportPlan]) -> list[dict[str, Any]]:
    masked = _masked_keys(plans)
    return [
        {
            "plan": p,
            "overrides": [(k, shown_value(k, v, masked)) for k, v in sorted(p.overrides.items())],
        }
        for p in plans
    ]


@router.post("/import/wizard/{name}/review")
async def wizard_review(name: str, request: Request, services: Services) -> Response:
    """Step 3: build the plan and show it for confirmation."""
    form = await request.form()
    c = await _candidate(services, name)
    choice = _decode_source(form, c, await services.sources.list())
    options = _options(form)
    try:
        plan = await services.importer.plan(
            name, choice.source_id, choice.new_source, choice.template_path, options
        )
    except UamError as exc:
        return render(
            request,
            "_import_step2.html",
            status_code=422,
            c=c,
            carry=_carry(form),
            subdomain=form_value(form, "subdomain"),
            has_proxy_host=c.proxy_host is not None,
            error=error_text(exc),
            o=options,
        )
    token = _keep(request, [plan])
    return render(
        request,
        "_import_review.html",
        rows=_review_rows([plan]),
        token=token,
        bulk=False,
        skipped=[],
    )


@router.post("/import/apply")
async def apply_import(request: Request, services: Services) -> Response:
    """Apply the plan that was reviewed (single or bulk)."""
    form = await request.form()
    plans = _take(request, form_value(form, "token"))
    results: list[dict[str, Any]] = []
    for plan in plans:
        name = plan.candidate.container_name
        try:
            app = await services.importer.apply(plan)
        except UamError as exc:
            results.append({"name": name, "app_id": None, "error": error_text(exc)})
        else:
            results.append({"name": name, "app_id": app.id, "error": ""})
    return render(request, "_import_done.html", results=results)


@router.post("/import/bulk")
async def bulk_review(request: Request, services: Services) -> Response:
    """Plan every selected high-confidence candidate with default options and show one combined review."""
    form = await request.form()
    names = [v for v in form.getlist("names") if isinstance(v, str)]
    if not names:
        raise ValidationError("select at least one container")
    by_name = {c.container_name: c for c in await services.importer.scan()}
    plans: list[ImportPlan] = []
    skipped: list[tuple[str, str]] = []
    for name in names:
        c = by_name.get(name)
        if c is None:
            skipped.append((name, "container not found"))
            continue
        m = best_match(c)
        if not bulk_eligible(c) or m is None:
            reason = "; ".join(c.blockers) if c.blockers else "no high-confidence match"
            skipped.append((name, reason))
            continue
        try:
            plans.append(
                await services.importer.plan(
                    name,
                    m.source_id,
                    None if m.source_id is not None else NewSource(m.owner, m.repo),
                    m.template_path,
                    ImportOptions(),
                )
            )
        except UamError as exc:
            skipped.append((name, error_text(exc)))
    if not plans:
        return render(
            request, "_import_review.html", status_code=422, rows=[], token="", bulk=True, skipped=skipped
        )
    return render(
        request,
        "_import_review.html",
        rows=_review_rows(plans),
        token=_keep(request, plans),
        bulk=True,
        skipped=skipped,
    )
