"""Dashboard, app detail, add/edit app form and sources pages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_app
from tests.unit.web.test_ui_support import HX, UiEnv, build_ui_env
from uam.domain import (
    LABEL_UNRAID_ICON,
    LABEL_UNRAID_WEBUI,
    AppState,
    ContainerInfo,
    CredentialKind,
    ProxySettings,
)
from uam.errors import ConflictError
from uam.settings import Settings


@pytest.fixture
async def env() -> AsyncIterator[UiEnv]:
    e = build_ui_env()
    await e.login()
    yield e
    await e.client.aclose()


# ---------------------------------------------------------------- dashboard
async def test_dashboard_empty(env: UiEnv) -> None:
    r = await env.client.get("/")
    assert r.status_code == 200 and "No apps yet" in r.text
    assert 'hx-trigger="every 10s"' in r.text


async def test_dashboard_card_content(env: UiEnv) -> None:
    now = datetime.now(UTC)
    await env.fakes.settings.save(Settings(base_domain="example.com"))
    app = await env.fakes.apps.add(
        make_app(
            name="notes",
            state=AppState.RUNNING,
            container_id="c1",
            installed_template_commit="aaa",
            available_template_commit="bbb",
            installed_image_digest="sha256:1",
            available_image_digest="sha256:2",
            needs_attention="new required variable FOO",
            last_checked_at=now - timedelta(minutes=5),
        )
    )
    r = await env.client.get("/")
    assert "notes" in r.text and "running" in r.text
    assert "template update" in r.text and "image update" in r.text
    assert "new required variable FOO" in r.text
    assert "5m ago" in r.text
    assert f"/apps/{app.id}/restart" in r.text and f"/apps/{app.id}/check" in r.text
    assert f"/apps/{app.id}/update" in r.text


async def test_card_shows_webui_link_from_container_label(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(name="notes", state=AppState.RUNNING, container_id="cid"))
    env.fakes.docker.seed_container(
        ContainerInfo(
            "cid",
            "notes",
            "img",
            None,
            "running",
            None,
            {LABEL_UNRAID_WEBUI: "http://[IP]:[PORT:3000]/", LABEL_UNRAID_ICON: "http://x/icon.png"},
            (),
        )
    )
    await env.fakes.ports.pin(app.id, [(3000, "tcp", 31000)])
    r = await env.client.get(f"/fragments/apps/{app.id}/card")
    assert 'href="http://192.168.1.10:31000/"' in r.text
    assert 'src="http://x/icon.png"' in r.text
    assert 'data-icon-fallback="N"' in r.text


async def test_card_polls_fast_while_installing(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.INSTALLING))
    r = await env.client.get(f"/fragments/apps/{app.id}/card")
    assert 'hx-trigger="every 2s"' in r.text


async def test_card_idle_does_not_poll_fast(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.RUNNING))
    r = await env.client.get(f"/fragments/apps/{app.id}/card")
    assert "every 2s" not in r.text


async def test_cards_fragment(env: UiEnv) -> None:
    await env.fakes.apps.add(make_app(name="a1", state=AppState.STOPPED))
    await env.fakes.apps.add(make_app(name="a2", state=AppState.RUNNING))
    r = await env.client.get("/fragments/cards", headers=HX)
    assert "a1" in r.text and "a2" in r.text and "<html" not in r.text


# ---------------------------------------------------------------- actions
@pytest.mark.parametrize(
    ("action", "state"),
    [("start", AppState.RUNNING), ("stop", AppState.STOPPED), ("restart", AppState.RUNNING)],
)
async def test_lifecycle_actions_return_card(env: UiEnv, action: str, state: AppState) -> None:
    app = await env.fakes.apps.add(
        make_app(state=AppState.STOPPED if action == "start" else AppState.RUNNING)
    )
    r = await env.client.post(f"/apps/{app.id}/{action}", headers=HX)
    assert r.status_code == 200 and f'id="card-{app.id}"' in r.text
    assert [c[0] for c in env.api.orchestrator.calls] == [action]
    assert (await env.fakes.apps.get(app.id)).state == state


async def test_check_action_runs_checker(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.RUNNING))
    r = await env.client.post(f"/apps/{app.id}/check", headers=HX)
    assert r.status_code == 200
    assert env.api.checker.calls == [app.id]


async def test_install_and_update_run_in_background(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.NOT_INSTALLED))
    r = await env.client.post(f"/apps/{app.id}/install", headers=HX)
    assert r.status_code == 200
    assert ("install", (app.id,)) in env.api.orchestrator.calls
    r = await env.client.post(
        f"/apps/{app.id}/update", data={"apply_template": "0", "apply_image": "1"}, headers=HX
    )
    assert r.status_code == 200
    assert ("update", (app.id, False, True)) in env.api.orchestrator.calls


async def test_action_error_is_flashed(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.RUNNING))
    env.api.orchestrator.fail["stop"] = ConflictError("busy with ghp_abcdefghijklmnop123456")
    r = await env.client.post(f"/apps/{app.id}/stop", headers=HX)
    assert r.status_code == 409 and r.headers["hx-retarget"] == "#flash"
    assert "busy" in r.text and "ghp_abcdefghijklmnop123456" not in r.text


async def test_unknown_action_and_missing_app(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    assert (await env.client.post(f"/apps/{app.id}/explode", headers=HX)).status_code == 409
    assert (await env.client.post("/apps/99/start", headers=HX)).status_code == 404


async def test_uninstall_redirects(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    r = await env.client.post(f"/apps/{app.id}/uninstall", data={"delete_appdata": "1"}, headers=HX)
    assert r.headers["hx-redirect"] == "/"
    assert ("uninstall", (app.id, True)) in env.api.orchestrator.calls


# ---------------------------------------------------------------- detail
async def test_app_detail(env: UiEnv) -> None:
    src = await env.seed_source()
    await env.fakes.settings.save(Settings(base_domain="example.com"))
    app = await env.fakes.apps.add(
        make_app(
            name="notes",
            source_id=src.id,
            state=AppState.RUNNING,
            container_id="c1",
            overrides={"Variable:TZ": "UTC", "Variable:API_TOKEN": "hunter2-secret"},
            proxy=ProxySettings(subdomain="notes"),
            installed_template_commit="aaaaaaaaaaaaaaaa",
            available_template_commit="bbbbbbbbbbbbbbbb",
        )
    )
    await env.fakes.ports.pin(app.id, [(3000, "tcp", 31000)])
    await env.fakes.events.record(app.id, "installed", "installed notes")
    r = await env.client.get(f"/apps/{app.id}")
    assert r.status_code == 200
    assert "31000" in r.text and "https://notes.example.com" in r.text
    assert "hunter2-secret" not in r.text and "***" in r.text
    assert "compare/aaaaaaaaaaaaaaaa...bbbbbbbbbbbbbbbb" in r.text
    assert f"/fragments/apps/{app.id}/logs" in r.text
    events = await env.client.get(f"/fragments/apps/{app.id}/events")
    assert "installed notes" in events.text


async def test_logs_fragment(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(container_id="cid"))
    env.fakes.docker.seed_container(ContainerInfo("cid", "notes", "img", None, "running", None, {}, ()))
    env.fakes.docker.log_text["cid"] = "hello from the app\nusing ghp_abcdefghijklmnop123456"
    r = await env.client.get(f"/fragments/apps/{app.id}/logs")
    assert r.status_code == 200 and "<pre" in r.text and "hello from the app" in r.text
    assert "ghp_abcdefghijklmnop123456" not in r.text
    none = await env.fakes.apps.add(make_app(name="other", container_id=None))
    assert "no container" in (await env.client.get(f"/fragments/apps/{none.id}/logs")).text


async def test_summary_fragment_has_detail_actions(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.RUNNING))
    r = await env.client.get(f"/fragments/apps/{app.id}/summary")
    assert "?view=detail" in r.text and 'id="summary"' in r.text


# ---------------------------------------------------------------- add / edit form
async def test_new_app_form_is_generated_from_template(env: UiEnv) -> None:
    src = await env.seed_source("masked-extra.xml")
    r = await env.client.get("/apps/new", params={"source": src.id, "path": "unraid/notes.xml"})
    assert r.status_code == 200
    for legend in ("Ports", "Paths", "Variables", "Devices"):
        assert f"<legend>{legend}</legend>" in r.text
    assert 'name="ov:Port:5000/tcp"' in r.text
    assert "auto (next free: 30000)" in r.text
    assert 'name="ov:Variable:CLOUD_PASSWORD" type="password"' in r.text
    assert "<details" in r.text and "Advanced variables" in r.text
    assert "*.example.com</option>" in r.text
    assert "LAN only" in r.text
    assert 'id="sub-preview"' in r.text and "/fragments/subdomain-preview" in r.text


async def test_next_free_port_skips_ports_in_use(env: UiEnv) -> None:
    src = await env.seed_source()
    other = await env.fakes.apps.add(make_app(name="other"))
    await env.fakes.ports.allocate(other.id, [(80, "tcp", 30000)])
    r = await env.client.get("/apps/new", params={"source": src.id, "path": "unraid/notes.xml"})
    assert "auto (next free: 30001)" in r.text


async def test_subdomain_preview(env: UiEnv) -> None:
    await env.fakes.settings.save(Settings(base_domain="example.com"))
    r = await env.client.get("/fragments/subdomain-preview", params={"subdomain": "Notes"})
    assert "notes.example.com" in r.text
    assert "no subdomain" in (await env.client.get("/fragments/subdomain-preview")).text


async def test_create_app_posts_and_redirects(env: UiEnv) -> None:
    src = await env.seed_source()
    await env.fakes.settings.save(Settings(base_domain="example.com"))
    r = await env.client.post(
        "/apps/new",
        data={
            "source_id": str(src.id),
            "template_path": "unraid/notes.xml",
            "name": "my-notes",
            "ov:Port:3000/tcp": "",
            "ov:Path:/data": "/mnt/user/appdata/notes",
            "ov:Variable:TZ": "UTC",
            "subdomain": "notes",
            "forward_mode": "host_port",
            "certificate_id": "1",
            "auto_ports": "1",
            "ssl_forced": "1",
            "auto_update_image": "1",
            "install": "1",
        },
        headers=HX,
    )
    assert r.status_code == 200 and r.headers["hx-redirect"] == "/apps/1"
    call = next(c for c in env.api.orchestrator.calls if c[0] == "add_app")
    _, (source_id, path, name, overrides, proxy, auto_ports) = call
    assert (source_id, path, name) == (src.id, "unraid/notes.xml", "my-notes")
    assert overrides == {"Variable:TZ": "UTC"}
    assert proxy.subdomain == "notes" and proxy.certificate_id == 1 and proxy.websockets is False
    assert auto_ports is True
    assert ("install", (1,)) in env.api.orchestrator.calls
    saved = await env.fakes.apps.get(1)
    assert saved.auto_update_image is True and saved.auto_update_template is False


async def test_create_app_without_install(env: UiEnv) -> None:
    src = await env.seed_source()
    await env.client.post("/apps/new", data={"source_id": str(src.id), "template_path": "unraid/notes.xml"})
    assert not any(c[0] == "install" for c in env.api.orchestrator.calls)


async def test_create_app_validation_errors_are_inline(env: UiEnv) -> None:
    src = await env.seed_source("masked-extra.xml")
    r = await env.client.post(
        "/apps/new",
        data={
            "source_id": str(src.id),
            "template_path": "unraid/notes.xml",
            "ov:Port:5000/tcp": "99999",
            "ov:Variable:CLOUD_PASSWORD": "",
            "ov:Variable:CLOUD_USER": "me@example.com",
            "subdomain": "Bad_Name!",
            "name": "ok-name",
        },
        headers=HX,
    )
    assert r.status_code == 422
    assert "<html" not in r.text
    assert "must be a port number between 1 and 65535" in r.text
    assert "required" in r.text
    assert "one DNS label" in r.text or "set the base domain" in r.text
    assert 'value="me@example.com"' in r.text
    assert not any(c[0] == "add_app" for c in env.api.orchestrator.calls)


async def test_create_app_orchestrator_error_shown_inline(env: UiEnv) -> None:
    src = await env.seed_source()
    env.api.orchestrator.fail["add_app"] = ConflictError("name already in use")
    r = await env.client.post(
        "/apps/new", data={"source_id": str(src.id), "template_path": "unraid/notes.xml"}, headers=HX
    )
    assert r.status_code == 409 and "name already in use" in r.text and "<html" not in r.text


async def test_edit_form_never_sends_masked_values(env: UiEnv) -> None:
    src = await env.seed_source("masked-extra.xml")
    app = await env.fakes.apps.add(
        make_app(
            source_id=src.id,
            template_path="unraid/notes.xml",
            overrides={"Variable:CLOUD_PASSWORD": "s3cret-pw", "Variable:CLOUD_USER": "me@x.org"},
        )
    )
    r = await env.client.get(f"/apps/{app.id}/edit")
    assert r.status_code == 200
    assert "s3cret-pw" not in r.text
    assert 'name="ov:Variable:CLOUD_PASSWORD" type="password" value=""' in r.text
    assert 'placeholder="unchanged"' in r.text
    assert 'value="me@x.org"' in r.text


async def test_edit_empty_masked_field_keeps_existing_value(env: UiEnv) -> None:
    src = await env.seed_source("masked-extra.xml")
    app = await env.fakes.apps.add(
        make_app(
            source_id=src.id,
            template_path="unraid/notes.xml",
            overrides={"Variable:CLOUD_PASSWORD": "s3cret-pw", "Variable:CLOUD_USER": "me@x.org"},
        )
    )
    r = await env.client.post(
        f"/apps/{app.id}/edit",
        data={
            "source_id": str(src.id),
            "template_path": "unraid/notes.xml",
            "ov:Variable:CLOUD_PASSWORD": "",
            "ov:Variable:CLOUD_USER": "new@x.org",
            "ov:Variable:API_TOKEN": "tok-1",
            "auto_ports": "1",
            "auto_update_image": "1",
        },
        headers=HX,
    )
    assert r.status_code == 200 and r.headers["hx-redirect"] == f"/apps/{app.id}"
    _, (aid, overrides, _proxy, _tag) = next(c for c in env.api.orchestrator.calls if c[0] == "reconfigure")
    assert aid == app.id
    assert overrides["Variable:CLOUD_PASSWORD"] == "s3cret-pw"
    assert overrides["Variable:CLOUD_USER"] == "new@x.org"
    assert overrides["Variable:API_TOKEN"] == "tok-1"


async def test_edit_replaces_masked_value_when_given(env: UiEnv) -> None:
    src = await env.seed_source("masked-extra.xml")
    app = await env.fakes.apps.add(
        make_app(
            source_id=src.id,
            template_path="unraid/notes.xml",
            overrides={"Variable:CLOUD_PASSWORD": "old", "Variable:CLOUD_USER": "me@x.org"},
        )
    )
    await env.client.post(
        f"/apps/{app.id}/edit",
        data={
            "ov:Variable:CLOUD_PASSWORD": "brand-new",
            "ov:Variable:CLOUD_USER": "me@x.org",
            "auto_ports": "1",
            "auto_update_image": "1",
        },
    )
    _, (_aid, overrides, _p, _t) = next(c for c in env.api.orchestrator.calls if c[0] == "reconfigure")
    assert overrides["Variable:CLOUD_PASSWORD"] == "brand-new"


async def test_edit_flag_change_without_config_change_does_not_reconfigure(env: UiEnv) -> None:
    src = await env.seed_source()
    app = await env.fakes.apps.add(make_app(source_id=src.id, template_path="unraid/notes.xml"))
    await env.client.post(
        f"/apps/{app.id}/edit",
        data={"auto_ports": "1", "auto_update_template": "1", "ssl_forced": "1", "websockets": "1"},
    )
    assert not any(c[0] == "reconfigure" for c in env.api.orchestrator.calls)
    assert (await env.fakes.apps.get(app.id)).auto_update_template is True


# ---------------------------------------------------------------- sources
async def test_sources_page_lists_and_adds(env: UiEnv) -> None:
    r = await env.client.get("/sources")
    assert r.status_code == 200 and "No sources yet" in r.text
    added = await env.client.post(
        "/sources",
        data={"repo": "https://github.com/felizk/notes.git", "ref": "", "credential_id": ""},
        headers=HX,
    )
    assert added.headers["hx-redirect"] == "/sources"
    (src,) = await env.fakes.sources.list()
    assert (src.owner, src.repo, src.ref) == ("felizk", "notes", "main")
    assert "felizk/notes" in (await env.client.get("/sources")).text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("felizk/notes", ("felizk", "notes", None, None)),
        ("https://github.com/felizk/notes", ("felizk", "notes", None, None)),
        ("https://github.com/felizk/notes/tree/dev", ("felizk", "notes", "dev", None)),
        (
            "https://github.com/felizk/notes/blob/dev/unraid/notes.xml",
            ("felizk", "notes", "dev", "unraid/notes.xml"),
        ),
        ("git@github.com:felizk/notes.git", ("felizk", "notes", None, None)),
    ],
)
def test_parse_repo(raw: str, expected: tuple[str, str, str | None, str | None]) -> None:
    from uam.web.ui.sources import parse_repo

    assert parse_repo(raw) == expected


async def test_add_source_rejects_garbage(env: UiEnv) -> None:
    r = await env.client.post("/sources", data={"repo": "not a repo"}, headers=HX)
    assert r.status_code == 422 and "owner/repo" in r.text


async def test_add_source_with_url_branch_and_credential(env: UiEnv) -> None:
    cred = await env.fakes.vault.store("gh", CredentialKind.GITHUB_PAT, "u", "tok")
    await env.client.post(
        "/sources",
        data={"repo": "https://github.com/a/b/tree/dev", "credential_id": str(cred.id)},
    )
    (src,) = await env.fakes.sources.list()
    assert (src.ref, src.credential_id) == ("dev", cred.id)


async def test_discover_templates_offers_add_app(env: UiEnv) -> None:
    src = await env.seed_source()
    r = await env.client.get(f"/sources/{src.id}/templates", headers=HX)
    assert r.status_code == 200
    assert f"/apps/new?source={src.id}&amp;path=unraid/notes.xml" in r.text or "unraid%2Fnotes.xml" in r.text
    assert "Add app" in r.text


async def test_delete_source(env: UiEnv) -> None:
    src = await env.seed_source()
    assert (await env.client.delete(f"/sources/{src.id}", headers=HX)).status_code == 200
    assert await env.fakes.sources.list() == []
    assert (await env.client.delete(f"/sources/{src.id}", headers=HX)).status_code == 404


async def test_logs_fragment_finds_starting_container_by_name(env: UiEnv) -> None:
    """During an install the new container isn't recorded on the app yet; it is found by name."""
    src = await env.seed_source()
    app = await env.fakes.apps.add(
        make_app(name="starting", source_id=src.id, container_id=None, state=AppState.INSTALLING)
    )
    env.fakes.docker.seed_container(ContainerInfo("new1", "starting", "img", None, "running", None, {}, ()))
    env.fakes.docker.log_text["new1"] = "waiting for database..."
    r = await env.client.get(f"/fragments/apps/{app.id}/logs")
    assert "waiting for database" in r.text
    detail = await env.client.get(f"/apps/{app.id}")
    assert "every 3s" in detail.text


async def test_multiline_events_render_as_pre(env: UiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    await env.fakes.events.record(app.id, "install.logs", "Last 100 log lines:\nboom", "error")
    r = await env.client.get(f"/fragments/apps/{app.id}/events")
    assert '<pre class="logs">Last 100 log lines:\nboom</pre>' in r.text


async def test_sources_edit_changes_credential(env: UiEnv) -> None:
    from uam.domain import CredentialKind

    src = await env.seed_source()
    cred = await env.fakes.vault.store("gh", CredentialKind.GITHUB_PAT, "me", "tok")
    page = await env.client.get("/sources")
    assert f'hx-get="/sources/{src.id}/edit"' in page.text and 'name="ref" value' not in page.text
    edit = await env.client.get(f"/sources/{src.id}/edit", headers=HX)
    assert 'class="editing"' in edit.text and f'<option value="{cred.id}"' in edit.text
    r = await env.client.post(
        f"/sources/{src.id}/update",
        data={"ref": "dev", "template_path": "", "credential_id": str(cred.id)},
        headers=HX,
    )
    assert r.text.lstrip().startswith(f'<tr id="source-{src.id}">') and ">gh<" in r.text and ">dev<" in r.text
    stored = await env.fakes.sources.get(src.id)
    assert (stored.ref, stored.template_path, stored.credential_id) == ("dev", None, cred.id)
    cancel = await env.client.get(f"/sources/{src.id}/row", headers=HX)
    assert "editing" not in cancel.text


async def test_edit_path_round_trip(env: UiEnv) -> None:
    src = await env.seed_source("simple-web.xml")
    app = await env.fakes.apps.add(make_app(source_id=src.id, template_path="unraid/notes.xml"))
    await env.client.post(
        f"/apps/{app.id}/edit",
        data={"ov:Path:/data": "/mnt/user/appdata/notes-moved", "auto_ports": "1", "auto_update_image": "1"},
        headers=HX,
    )
    _, (_aid, overrides, _p, _t) = next(c for c in env.api.orchestrator.calls if c[0] == "reconfigure")
    assert overrides["Path:/data"] == "/mnt/user/appdata/notes-moved"
    # once saved, the edit form shows the new path
    stored = await env.fakes.apps.get(app.id)
    stored.overrides = dict(overrides)
    await env.fakes.apps.save(stored)
    page = await env.client.get(f"/apps/{app.id}/edit")
    assert 'value="/mnt/user/appdata/notes-moved"' in page.text
