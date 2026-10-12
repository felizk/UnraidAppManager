"""Import page: candidate table, three-step wizard, bulk import."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.unit.web.test_ui_support import HX, UiEnv, build_ui_env, make_candidate, make_plan
from uam.domain import ImportCandidate, ProxyHost, SourceMatch
from uam.errors import ConflictError


@pytest.fixture
async def env() -> AsyncIterator[UiEnv]:
    e = build_ui_env()
    await e.login()
    await e.seed_source()
    yield e
    await e.client.aclose()


def _step1_form(**over: str) -> dict[str, str]:
    base = {"source_choice": "match:0", "template_path": "unraid/notes.xml"}
    base.update(over)
    return base


async def test_import_page_lists_candidates(env: UiEnv) -> None:
    base = make_candidate("notes")
    host = ProxyHost(7, ("notes.example.com",), "http", "10.0.0.2", 8080)
    other = ImportCandidate(
        **{
            **{f: getattr(make_candidate("blocked"), f) for f in ImportCandidate.__dataclass_fields__},
            "blockers": ("container is not running",),
            "proxy_host": host,
            "source_matches": (SourceMatch(None, "x", "y", None, "low", "name"),),
        }
    )
    env.fakes.importer.candidates = [base, other]
    r = await env.client.get("/import")
    assert r.status_code == 200
    assert "notes" in r.text and "felizk/notes" in r.text and "high" in r.text
    assert "dockerman" in r.text
    assert "container is not running" in r.text
    assert "notes.example.com" in r.text
    assert "/import/wizard/notes" in r.text
    assert "Import selected with defaults" in r.text
    assert 'name="names" value="notes"' in r.text
    assert 'name="names" value="blocked"' not in r.text


async def test_select_high_preselects_eligible_rows(env: UiEnv) -> None:
    env.fakes.importer.candidates = [make_candidate("notes")]
    plain = await env.client.get("/import")
    picked = await env.client.get("/import", params={"select": "high"})
    assert (
        'value="notes" aria-label="Select notes" >' in plain.text
        or "checked" not in plain.text.split('name="names"')[1].split(">")[0]
    )
    assert "checked" in picked.text.split('name="names"')[1].split(">")[0]


async def test_wizard_step1_offers_matches_and_new_source(env: UiEnv) -> None:
    env.fakes.importer.candidates = [make_candidate("notes")]
    r = await env.client.get("/import/wizard/notes", headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert 'value="match:0"' in r.text and "felizk/notes" in r.text
    assert 'value="new"' in r.text and 'name="new_owner" value="felizk"' in r.text
    assert 'value="unraid/notes.xml"' in r.text


async def test_wizard_unknown_container_is_404(env: UiEnv) -> None:
    assert (await env.client.get("/import/wizard/ghost", headers=HX)).status_code == 404


async def test_wizard_step1_validation(env: UiEnv) -> None:
    env.fakes.importer.candidates = [make_candidate("notes")]
    r = await env.client.post(
        "/import/wizard/notes/options", data=_step1_form(source_choice="new", new_owner=""), headers=HX
    )
    assert r.status_code == 422 and "owner and repo are required" in r.text
    r = await env.client.post(
        "/import/wizard/notes/options",
        data=_step1_form(source_choice="new", new_owner="a", new_repo="b", template_path=""),
        headers=HX,
    )
    assert r.status_code == 422 and "choose the template" in r.text


async def test_wizard_full_flow(env: UiEnv) -> None:
    cand = make_candidate("notes")
    env.fakes.importer.candidates = [cand]
    env.fakes.importer.plans["notes"] = make_plan(cand)

    step2 = await env.client.post("/import/wizard/notes/options", data=_step1_form(), headers=HX)
    assert step2.status_code == 200 and "step 2 of 3" in step2.text
    for name in ("keep_host_ports", "recreate_now", "auto_update_image", "auto_update_template"):
        assert f'name="{name}"' in step2.text
    assert 'name="source_choice" value="match:0"' in step2.text

    review = await env.client.post(
        "/import/wizard/notes/review",
        data={
            **_step1_form(),
            "keep_host_ports": "1",
            "recreate_now": "1",
            "auto_update_image": "1",
            "subdomain": "notes",
        },
        headers=HX,
    )
    assert review.status_code == 200 and "step 3 of 3" in review.text
    assert 'class="dropped"' in review.text and "env FOO=***" in review.text
    assert "supersecretvalue" not in review.text and "***" in review.text
    assert "<code>Variable:TZ</code></td><td>UTC" in review.text
    assert "<td>80</td><td>tcp</td><td>8080</td>" in review.text
    assert 'class="d-del">-old line' in review.text
    assert "port 80 was published on all interfaces" in review.text
    token = review.text.split('name="token" value="')[1].split('"')[0]

    done = await env.client.post("/import/apply", data={"token": token}, headers=HX)
    assert done.status_code == 200 and "Import finished" in done.text and 'href="/apps/1"' in done.text
    assert len(env.fakes.importer.applied) == 1
    again = await env.client.post("/import/apply", data={"token": token}, headers=HX)
    assert again.status_code == 404 and "expired" in again.text
    assert len(env.fakes.importer.applied) == 1


async def test_wizard_plan_options_are_passed_through(env: UiEnv) -> None:
    cand = make_candidate("notes")
    env.fakes.importer.candidates = [cand]
    seen: dict[str, Any] = {}

    async def plan(name, source_id, new_source, template_path, options):
        seen.update(source_id=source_id, new_source=new_source, path=template_path, options=options)
        return make_plan(cand)

    env.fakes.importer.plan = plan  # type: ignore[method-assign]
    await env.client.post(
        "/import/wizard/notes/review",
        data={
            "source_choice": "new",
            "new_owner": "a",
            "new_repo": "b",
            "new_ref": "dev",
            "template_path": "x.xml",
            "claim_proxy_host": "1",
            "subdomain": "hello",
            "archive_flash_template": "1",
        },
        headers=HX,
    )
    assert seen["source_id"] is None
    assert (seen["new_source"].owner, seen["new_source"].repo, seen["new_source"].ref) == ("a", "b", "dev")
    o = seen["options"]
    assert (o.keep_host_ports, o.claim_proxy_host, o.subdomain, o.recreate_now) == (
        False,
        True,
        "hello",
        False,
    )
    assert o.archive_flash_template is True


async def test_wizard_review_error_returns_to_options(env: UiEnv) -> None:
    env.fakes.importer.candidates = [make_candidate("notes")]
    r = await env.client.post("/import/wizard/notes/review", data=_step1_form(), headers=HX)
    assert r.status_code == 422 and "step 2 of 3" in r.text and "notes" in r.text


async def test_apply_failure_is_reported(env: UiEnv) -> None:
    cand = make_candidate("notes")
    env.fakes.importer.candidates = [cand]
    env.fakes.importer.plans["notes"] = make_plan(cand)

    def boom(plan):
        raise ConflictError("stale plan")

    env.fakes.importer.on_apply = boom
    review = await env.client.post("/import/wizard/notes/review", data=_step1_form(), headers=HX)
    token = review.text.split('name="token" value="')[1].split('"')[0]
    done = await env.client.post("/import/apply", data={"token": token}, headers=HX)
    assert "stale plan" in done.text and "failed" in done.text


async def test_bulk_import_combined_review_then_apply(env: UiEnv) -> None:
    good, risky = make_candidate("notes"), make_candidate("risky", confidence="medium")
    blocked = make_candidate("blocked", blockers=("not running",))
    env.fakes.importer.candidates = [good, risky, blocked]
    env.fakes.importer.plans["notes"] = make_plan(good)
    env.fakes.importer.plans["risky"] = make_plan(risky)

    review = await env.client.post("/import/bulk", data={"names": ["notes", "risky", "blocked"]}, headers=HX)
    assert review.status_code == 200
    assert "Review: import selected containers" in review.text
    assert "<h3>notes" in review.text and "<h3>risky" not in review.text
    assert "risky: no high-confidence match" in review.text and "blocked: not running" in review.text
    assert 'class="dropped"' in review.text
    assert env.fakes.importer.applied == []
    token = review.text.split('name="token" value="')[1].split('"')[0]

    done = await env.client.post("/import/apply", data={"token": token}, headers=HX)
    assert "notes" in done.text and len(env.fakes.importer.applied) == 1


async def test_bulk_import_requires_selection_and_eligible(env: UiEnv) -> None:
    env.fakes.importer.candidates = [make_candidate("blocked", blockers=("x",))]
    none = await env.client.post("/import/bulk", data={}, headers=HX)
    assert none.status_code == 422 and "select at least one" in none.text
    r = await env.client.post("/import/bulk", data={"names": ["blocked"]}, headers=HX)
    assert r.status_code == 422 and "blocked: x" in r.text and "Confirm" not in r.text


async def test_bulk_uses_new_source_for_unregistered_match(env: UiEnv) -> None:
    cand = make_candidate("notes")
    cand = ImportCandidate(
        **{
            **{f: getattr(cand, f) for f in ImportCandidate.__dataclass_fields__},
            "source_matches": (SourceMatch(None, "o", "r", "t.xml", "high", "image"),),
        }
    )
    env.fakes.importer.candidates = [cand]
    seen: dict[str, Any] = {}

    async def plan(name, source_id, new_source, template_path, options):
        seen.update(source_id=source_id, new_source=new_source, path=template_path)
        return make_plan(cand)

    env.fakes.importer.plan = plan  # type: ignore[method-assign]
    await env.client.post("/import/bulk", data={"names": ["notes"]}, headers=HX)
    assert seen["source_id"] is None and seen["new_source"].owner == "o" and seen["path"] == "t.xml"
