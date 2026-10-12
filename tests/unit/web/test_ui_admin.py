"""Credentials, settings and export pages."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.fakes import make_app
from tests.unit.web.test_ui_support import HX, UiEnv, build_ui_env
from uam.domain import CredentialKind
from uam.errors import RemoteError

SECRET = "ghp_SUPERSECRETTOKEN1234567890"


@pytest.fixture
async def env() -> AsyncIterator[UiEnv]:
    e = build_ui_env()
    await e.login()
    yield e
    await e.client.aclose()


# ---------------------------------------------------------------- credentials
async def test_credentials_add_list_never_shows_secret(env: UiEnv) -> None:
    r = await env.client.post(
        "/credentials",
        data={"name": "gh", "kind": "github_pat", "username": "me", "secret": SECRET, "registry_host": ""},
        headers=HX,
    )
    assert r.status_code == 200 and "gh" in r.text
    assert SECRET not in r.text
    assert SECRET not in (await env.client.get("/credentials")).text
    (cred,) = await env.fakes.vault.list()
    assert await env.fakes.vault.reveal(cred.id) == SECRET


async def test_credentials_validation_and_duplicate(env: UiEnv) -> None:
    missing = await env.client.post(
        "/credentials", data={"name": "x", "kind": "npm", "secret": ""}, headers=HX
    )
    assert missing.status_code == 422 and "secret is required" in missing.text
    bad_kind = await env.client.post(
        "/credentials", data={"name": "x", "kind": "weird", "secret": "s"}, headers=HX
    )
    assert bad_kind.status_code == 422 and "choose a credential kind" in bad_kind.text
    data = {"name": "dup", "kind": "npm", "username": "u", "secret": "s"}
    await env.client.post("/credentials", data=data, headers=HX)
    again = await env.client.post("/credentials", data=data, headers=HX)
    assert again.status_code == 409 and "exists" in again.text
    assert 'value="dup"' in again.text


async def test_credentials_test_and_delete(env: UiEnv) -> None:
    env.fakes.github.token_scopes["good"] = ["repo"]
    ok = await env.fakes.vault.store("ok", CredentialKind.GITHUB_PAT, "u", "good")
    bad = await env.fakes.vault.store("bad", CredentialKind.GITHUB_PAT, "u", "ghp_bad123")
    r = await env.client.post(f"/credentials/{ok.id}/test", headers=HX)
    assert "test-result ok" in r.text and "repo" in r.text
    r = await env.client.post(f"/credentials/{bad.id}/test", headers=HX)
    assert "test-result fail" in r.text and "ghp_bad123" not in r.text
    assert (await env.client.delete(f"/credentials/{ok.id}", headers=HX)).status_code == 200
    assert (await env.client.delete(f"/credentials/{ok.id}", headers=HX)).status_code == 404


async def test_credentials_test_npm_and_registry(env: UiEnv) -> None:
    npm = await env.fakes.vault.store("npm", CredentialKind.NPM, "a@b.c", "pw")
    reg = await env.fakes.vault.store("reg", CredentialKind.REGISTRY, "u", "pw", "ghcr.io")
    assert "logged in" in (await env.client.post(f"/credentials/{npm.id}/test", headers=HX)).text
    assert "first pull" in (await env.client.post(f"/credentials/{reg.id}/test", headers=HX)).text


# ---------------------------------------------------------------- settings
def _form(**over: str) -> dict[str, str]:
    base = {
        "port_range_start": "30000",
        "port_range_end": "30999",
        "reserved_ports": "",
        "host_ip": "",
        "appdata_root": "/mnt/user/appdata",
        "npm_url": "",
        "npm_credential_id": "",
        "base_domain": "",
        "default_certificate_id": "",
        "hot_window_hours": "168",
        "hot_interval_minutes": "5",
        "cold_interval_hours": "24",
        "template_export_dir": "/unraid-templates",
        "template_export_include_secrets": "1",
        "unraid_managed_label": "uam",
    }
    base.update(over)
    return base


async def test_settings_page_shows_every_field(env: UiEnv) -> None:
    r = await env.client.get("/settings")
    assert r.status_code == 200
    fields = [
        "port_range_start",
        "port_range_end",
        "reserved_ports",
        "host_ip",
        "appdata_root",
        "npm_url",
        "npm_credential_id",
        "base_domain",
        "default_certificate_id",
        "hot_window_hours",
        "hot_interval_minutes",
        "cold_interval_hours",
        "template_export_dir",
        "template_export_include_secrets",
        "unraid_managed_label",
    ]
    for f in fields:
        assert f'name="{f}"' in r.text, f
    assert "Test NPM connection" in r.text


async def test_settings_save(env: UiEnv) -> None:
    cred = await env.fakes.vault.store("npm", CredentialKind.NPM, "a@b.c", "pw")
    r = await env.client.post(
        "/settings",
        data=_form(
            port_range_start="31000",
            port_range_end="31100",
            reserved_ports="31010, 31020",
            base_domain="example.com",
            npm_url="http://npm:81",
            npm_credential_id=str(cred.id),
            default_certificate_id="1",
            template_export_include_secrets="",
        ),
        headers=HX,
    )
    assert r.status_code == 200 and "Settings saved" in r.text
    s = await env.fakes.settings.get()
    assert (s.port_range_start, s.reserved_ports, s.base_domain) == (31000, [31010, 31020], "example.com")
    assert s.npm_credential_id == cred.id and s.default_certificate_id == 1
    assert s.template_export_include_secrets is False and s.host_ip is None


async def test_settings_validation_errors_inline(env: UiEnv) -> None:
    r = await env.client.post(
        "/settings", data=_form(port_range_start="40000", port_range_end="30000"), headers=HX
    )
    assert r.status_code == 422 and "must not be below" in r.text
    r = await env.client.post(
        "/settings",
        data=_form(port_range_start="abc", reserved_ports="x,y", hot_window_hours="0"),
        headers=HX,
    )
    assert r.status_code == 422
    assert r.text.count("must be a number") >= 1 and "comma-separated port numbers" in r.text
    assert 'value="abc"' in r.text
    assert (await env.fakes.settings.get()).port_range_start == 30000


async def test_test_npm_connection(env: UiEnv) -> None:
    ok = await env.client.post("/settings/test-npm", headers=HX)
    assert "test-result ok" in ok.text and "1 certificate" in ok.text
    env.api.services.proxy = None
    assert "not configured" in (await env.client.post("/settings/test-npm", headers=HX)).text


async def test_test_npm_failure(env: UiEnv) -> None:
    async def boom() -> list[tuple[int, str]]:
        raise RemoteError(502, "bad gateway")

    env.fakes.proxy.list_certificates = boom  # type: ignore[method-assign]
    r = await env.client.post("/settings/test-npm", headers=HX)
    assert "test-result fail" in r.text and "502" in r.text


# ---------------------------------------------------------------- export
async def test_export_flow_preview_then_write(env: UiEnv) -> None:
    a = await env.fakes.apps.add(make_app(name="one"))
    b = await env.fakes.apps.add(make_app(name="two"))
    page = await env.client.get("/export")
    assert "one" in page.text and "two" in page.text

    preview = await env.client.post("/export/preview", data={"app_ids": [str(a.id), str(b.id)]}, headers=HX)
    assert preview.status_code == 200
    assert 'class="d-add"' in preview.text and "Write to flash" in preview.text
    assert "include_secrets" in preview.text and "checked" in preview.text
    digest = preview.text.split('name="digest" value="')[1].split('"')[0]

    done = await env.client.post(
        "/export/apply",
        data={"app_ids": [str(a.id), str(b.id)], "digest": digest, "include_secrets": ""},
        headers=HX,
    )
    assert done.status_code == 200 and "Wrote 2 template" in done.text
    plans, include = env.api.exporter.applied[0]
    assert [p.app_id for p in plans] == [a.id, b.id] and include is False


async def test_export_apply_refuses_when_changed_since_preview(env: UiEnv) -> None:
    a = await env.fakes.apps.add(make_app(name="one"))
    r = await env.client.post("/export/apply", data={"app_ids": [str(a.id)], "digest": "stale"}, headers=HX)
    assert r.status_code == 409 and "preview again" in r.text
    assert env.api.exporter.applied == []


async def test_export_requires_selection(env: UiEnv) -> None:
    r = await env.client.post("/export/preview", data={}, headers=HX)
    assert r.status_code == 422 and "select at least one app" in r.text


async def test_diff_helper_colours_and_escapes() -> None:
    from uam.web.ui.support import diff_html

    out = str(diff_html("--- a\n+++ b\n@@ -1 +1 @@\n-<old>\n+<new>\n ctx"))
    assert 'class="d-del">-&lt;old&gt;' in out and 'class="d-add">+&lt;new&gt;' in out
    assert 'class="d-hunk"' in out and 'class="d-meta"' in out
