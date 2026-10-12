"""Page auth: redirects, login/setup flows, CSRF and static files."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.unit.web.test_api_support import PASSWORD
from tests.unit.web.test_ui_support import HX, UiEnv, build_ui_env

PROTECTED = ["/", "/sources", "/credentials", "/settings", "/export", "/import", "/apps/1", "/apps/new"]


@pytest.fixture
async def env() -> AsyncIterator[UiEnv]:
    e = build_ui_env()
    yield e
    await e.client.aclose()


@pytest.mark.parametrize("path", PROTECTED)
async def test_pages_redirect_to_setup_when_no_password(env: UiEnv, path: str) -> None:
    r = await env.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/setup"


@pytest.mark.parametrize("path", PROTECTED)
async def test_pages_redirect_to_login_when_not_signed_in(env: UiEnv, path: str) -> None:
    await env.api.services.kv.set("admin_password_hash", "x")
    r = await env.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_htmx_request_gets_hx_redirect(env: UiEnv) -> None:
    r = await env.client.get("/", headers=HX)
    assert r.status_code == 200 and r.headers["hx-redirect"] == "/setup"


async def test_setup_page_and_flow(env: UiEnv) -> None:
    page = await env.client.get("/setup")
    assert page.status_code == 200 and 'name="confirm"' in page.text
    mismatch = await env.client.post("/setup", data={"password": PASSWORD, "confirm": "different"})
    assert mismatch.status_code == 422 and "do not match" in mismatch.text
    short = await env.client.post("/setup", data={"password": "abc", "confirm": "abc"})
    assert short.status_code == 422 and "at least 8" in short.text
    ok = await env.client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD}, headers=HX)
    assert ok.status_code == 200 and ok.headers["hx-redirect"] == "/"
    assert "uam_session" in ok.headers["set-cookie"]
    assert (await env.client.get("/")).status_code == 200
    again = await env.client.get("/setup")
    assert again.status_code == 303 and again.headers["location"] == "/login"


async def test_login_wrong_and_right_password(env: UiEnv) -> None:
    await env.login()
    await env.client.post("/logout")
    env.client.cookies.clear()
    page = await env.client.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.text
    bad = await env.client.post("/login", data={"password": "nope nope nope"})
    assert bad.status_code == 401 and "invalid password" in bad.text
    good = await env.client.post("/login", data={"password": PASSWORD})
    assert good.status_code == 303 and good.headers["location"] == "/"
    assert (await env.client.get("/")).status_code == 200


async def test_logout_clears_session(env: UiEnv) -> None:
    await env.login()
    r = await env.client.post("/logout", headers=HX)
    assert r.headers["hx-redirect"] == "/login"
    env.client.cookies.clear()
    assert (await env.client.get("/")).headers["location"] == "/login"


async def test_post_without_csrf_header_is_rejected(env: UiEnv) -> None:
    await env.login()
    r = await env.client.post("/logout", headers={"X-Requested-With": ""})
    assert r.status_code == 403


async def test_body_carries_csrf_header_for_htmx(env: UiEnv) -> None:
    await env.login()
    html = (await env.client.get("/")).text
    assert 'hx-headers=\'{"X-Requested-With": "uam"}\'' in html
    assert "/static/vendor/htmx-2.0.4.min.js" in html


async def test_static_assets_are_served(env: UiEnv) -> None:
    for path in ("/static/vendor/htmx-2.0.4.min.js", "/static/vendor/pico-2.0.6.min.css", "/static/app.css"):
        r = await env.client.get(path)
        assert r.status_code == 200 and len(r.content) > 100


async def test_bearer_token_opens_pages() -> None:
    import httpx

    from tests.unit.web.test_api_support import build_env
    from uam.web.ui import create_ui_app

    api = build_env(api_token="tok-123")
    await api.services.kv.set("admin_password_hash", "x")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_ui_app(api.services)), base_url="http://test"
    )
    r = await client.get("/", headers={"Authorization": "Bearer tok-123"})
    assert r.status_code == 200
    await client.aclose()
    await api.client.aclose()
