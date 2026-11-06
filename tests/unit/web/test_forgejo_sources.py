"""Forgejo sources and moving sources between forges, through the UI and the REST API."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.fakes import make_app, make_source
from tests.unit.web.test_api_support import ApiEnv, build_env
from tests.unit.web.test_ui_support import HX, UiEnv, build_ui_env
from uam.domain import CredentialKind

XML = "<Container><Name>notes</Name></Container>"
FORGE = "https://git.example.com"


@pytest.fixture
async def ui() -> AsyncIterator[UiEnv]:
    e = build_ui_env()
    await e.login()
    try:
        yield e
    finally:
        await e.client.aclose()


@pytest.fixture
async def api() -> AsyncIterator[ApiEnv]:
    e = build_env()
    await e.login()
    try:
        yield e
    finally:
        await e.client.aclose()


async def test_add_forgejo_source_from_url(ui: UiEnv) -> None:
    r = await ui.client.post(
        "/sources",
        data={
            "repo": f"{FORGE}/felizk/notes/src/branch/dev/unraid/notes.xml",
            "ref": "",
            "credential_id": "",
        },
        headers=HX,
    )
    assert r.headers["hx-redirect"] == "/sources"
    (src,) = await ui.fakes.sources.list()
    assert (src.owner, src.repo, src.ref, src.template_path, src.forge_url) == (
        "felizk",
        "notes",
        "dev",
        "unraid/notes.xml",
        FORGE,
    )
    page = (await ui.client.get("/sources")).text
    assert "git.example.com/felizk/notes" in page
    assert f'href="{FORGE}/felizk/notes"' in page
    assert "Move GitHub sources to Forgejo" not in page


async def test_forgejo_token_offered_for_sources(ui: UiEnv) -> None:
    await ui.fakes.vault.store("my forgejo", CredentialKind.FORGEJO_TOKEN, "me", "t", FORGE)
    assert "my forgejo" in (await ui.client.get("/sources")).text


async def test_move_one_source(ui: UiEnv) -> None:
    source = await ui.fakes.sources.add(make_source())
    await ui.fakes.apps.add(make_app(source_id=source.id, installed_template_commit="c1"))
    ui.fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c1")

    form = (await ui.client.get(f"/sources/{source.id}/move")).text
    assert 'name="repo"' in form
    data = {"repo": f"{FORGE}/mirror/notes", "ref": "", "credential_id": ""}
    preview = (await ui.client.post(f"/sources/{source.id}/move/check", data=data, headers=HX)).text
    assert "same as installed" in preview and "Move 1 source(s)" in preview
    assert (await ui.fakes.sources.get(source.id)).forge_url == ""

    r = await ui.client.post(f"/sources/{source.id}/move", data={**data, "ref": "main"}, headers=HX)
    assert r.headers["hx-redirect"] == "/sources"
    moved = await ui.fakes.sources.get(source.id)
    assert (moved.owner, moved.forge_url, moved.ref) == ("mirror", FORGE, "main")


async def test_move_check_shows_failure(ui: UiEnv) -> None:
    source = await ui.fakes.sources.add(make_source())
    await ui.fakes.apps.add(make_app(source_id=source.id))
    data = {"repo": f"{FORGE}/mirror/notes"}
    preview = (await ui.client.post(f"/sources/{source.id}/move/check", data=data, headers=HX)).text
    assert "Nothing can be moved" in preview
    bad = (await ui.client.post(f"/sources/{source.id}/move/check", data={"repo": "??"}, headers=HX)).text
    assert "field-error" in bad


async def test_move_all_github_sources(ui: UiEnv) -> None:
    notes = await ui.fakes.sources.add(make_source())
    dns = await ui.fakes.sources.add(make_source(repo="dnsbox"))
    forge = await ui.fakes.sources.add(make_source(repo="already", forge_url=FORGE))
    await ui.fakes.apps.add(make_app(source_id=notes.id, installed_template_commit="c1"))
    await ui.fakes.apps.add(make_app(source_id=dns.id, name="dns", template_path="unraid/dns.xml"))
    ui.fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c1")
    cred = await ui.fakes.vault.store("fj", CredentialKind.FORGEJO_TOKEN, "me", "t", FORGE)

    assert "Move GitHub sources to Forgejo" in (await ui.client.get("/sources")).text
    data = {"forge_url": "git.example.com/", "owner": "mirror", "credential_id": str(cred.id)}
    preview = (await ui.client.post("/sources/move-all/check", data=data, headers=HX)).text
    assert "Move 1 source(s)" in preview and "skipped" in preview
    assert f'value="{FORGE}"' in preview

    await ui.client.post("/sources/move-all", data=data, headers=HX)
    moved = await ui.fakes.sources.get(notes.id)
    assert (moved.owner, moved.forge_url, moved.credential_id) == ("mirror", FORGE, cred.id)
    assert (await ui.fakes.sources.get(dns.id)).forge_url == ""
    assert await ui.fakes.sources.get(forge.id) == forge


async def test_move_all_requires_url(ui: UiEnv) -> None:
    await ui.fakes.sources.add(make_source())
    r = await ui.client.post("/sources/move-all/check", data={"forge_url": ""}, headers=HX)
    assert "enter the Forgejo URL" in r.text


async def test_compare_link_uses_forge(ui: UiEnv) -> None:
    source = await ui.fakes.sources.add(make_source(forge_url=FORGE))
    app = await ui.fakes.apps.add(
        make_app(source_id=source.id, installed_template_commit="c1", available_template_commit="c2")
    )
    page = (await ui.client.get(f"/apps/{app.id}")).text
    assert f"{FORGE}/felizk/notes/compare/c1...c2" in page


async def test_forgejo_credential_test(ui: UiEnv) -> None:
    ui.fakes.github.token_scopes["good"] = []
    with_host = await ui.fakes.vault.store("a", CredentialKind.FORGEJO_TOKEN, "me", "good", FORGE)
    no_host = await ui.fakes.vault.store("b", CredentialKind.FORGEJO_TOKEN, "me", "good")
    assert f"token accepted by {FORGE}" in (await ui.client.post(f"/credentials/{with_host.id}/test")).text
    assert "set the credential" in (await ui.client.post(f"/credentials/{no_host.id}/test")).text
    await ui.fakes.sources.add(make_source(forge_url=FORGE, credential_id=no_host.id))
    assert "token accepted" in (await ui.client.post(f"/credentials/{no_host.id}/test")).text


async def test_api_create_forgejo_source(api: ApiEnv) -> None:
    r = await api.client.post(
        "/api/sources", json={"owner": "me", "repo": "app", "forge_url": "git.example.com/"}
    )
    assert r.status_code == 201 and r.json()["forge_url"] == FORGE


async def test_api_move_dry_run_then_apply(api: ApiEnv) -> None:
    source = await api.fakes.sources.add(make_source())
    await api.fakes.apps.add(make_app(source_id=source.id, installed_template_commit="c1"))
    api.fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c1")
    body = {"forge_url": FORGE, "owner": "mirror"}

    dry = await api.client.post(f"/api/sources/{source.id}/move", json={**body, "dry_run": True})
    assert dry.status_code == 200
    assert dry.json()["ok"] is True and dry.json()["moved"] is False
    assert dry.json()["checks"][0]["app_names"] == ["notes"]
    assert (await api.fakes.sources.get(source.id)).forge_url == ""

    done = await api.client.post(f"/api/sources/{source.id}/move", json=body)
    assert done.json()["moved"] is True
    assert (await api.fakes.sources.get(source.id)).owner == "mirror"


async def test_api_move_conflict_when_template_missing(api: ApiEnv) -> None:
    source = await api.fakes.sources.add(make_source())
    await api.fakes.apps.add(make_app(source_id=source.id))
    r = await api.client.post(f"/api/sources/{source.id}/move", json={"forge_url": FORGE, "owner": "nobody"})
    assert r.status_code == 409


async def test_api_forgejo_credential_test(api: ApiEnv) -> None:
    api.fakes.github.token_scopes["good"] = []
    cred = await api.fakes.vault.store("a", CredentialKind.FORGEJO_TOKEN, "me", "good", FORGE)
    r = await api.client.post(f"/api/credentials/{cred.id}/test")
    assert r.json()["ok"] is True
