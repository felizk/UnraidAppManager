"""Auth: setup, login, sessions, bearer token, CSRF header."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.unit.web.test_api_support import PASSWORD, ApiEnv, build_env
from uam.web.auth import COOKIE_NAME, PASSWORD_KEY, SECRET_KEY


@pytest.fixture
async def env() -> AsyncIterator[ApiEnv]:
    e = build_env()
    yield e
    await e.client.aclose()


async def test_health_is_public(env: ApiEnv) -> None:
    r = await env.client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_protected_routes_need_session(env: ApiEnv) -> None:
    for path in ("/api/settings", "/api/apps", "/api/credentials", "/api/events"):
        assert (await env.client.get(path)).status_code == 401, path


async def test_status_before_and_after_setup(env: ApiEnv) -> None:
    assert (await env.client.get("/api/auth/status")).json() == {
        "setup_required": True,
        "authenticated": False,
    }
    await env.login()
    assert (await env.client.get("/api/auth/status")).json() == {
        "setup_required": False,
        "authenticated": True,
    }


async def test_setup_stores_hash_and_secret_in_kv(env: ApiEnv) -> None:
    await env.login()
    stored = await env.fakes.kv.get(PASSWORD_KEY)
    assert stored is not None and stored.startswith("$argon2")
    assert PASSWORD not in stored
    assert await env.fakes.kv.get(SECRET_KEY)
    assert "password" not in (await env.fakes.settings.get()).to_dict()
    assert (await env.client.get("/api/settings")).status_code == 200


async def test_setup_only_once_and_password_length(env: ApiEnv) -> None:
    assert (await env.client.post("/api/setup", json={"password": "short"})).status_code == 422
    await env.login()
    assert (await env.client.post("/api/setup", json={"password": "another password"})).status_code == 409


async def test_login_logout_flow(env: ApiEnv) -> None:
    await env.login()
    await env.client.post("/api/logout")
    assert COOKIE_NAME not in env.client.cookies
    assert (await env.client.get("/api/apps")).status_code == 401
    bad = await env.client.post("/api/login", json={"password": "nope nope nope"})
    assert bad.status_code == 401
    ok = await env.client.post("/api/login", json={"password": PASSWORD})
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and f"max-age={7 * 24 * 3600}" in cookie
    assert (await env.client.get("/api/apps")).status_code == 200


async def test_login_before_setup_is_rejected(env: ApiEnv) -> None:
    assert (await env.client.post("/api/login", json={"password": PASSWORD})).status_code == 401


async def test_forged_cookie_rejected(env: ApiEnv) -> None:
    await env.login()
    env.client.cookies.clear()
    env.client.cookies.set(COOKIE_NAME, "forged.value.here")
    assert (await env.client.get("/api/apps")).status_code == 401


async def test_cookie_from_other_secret_rejected(env: ApiEnv) -> None:
    await env.login()
    cookie = env.client.cookies[COOKIE_NAME]
    await env.fakes.kv.set(SECRET_KEY, "rotated")
    env.services.auth = None  # new AuthManager reads the rotated secret
    env.client.cookies.set(COOKIE_NAME, cookie)
    assert (await env.client.get("/api/apps")).status_code == 401


async def test_csrf_header_required_on_mutations(env: ApiEnv) -> None:
    await env.login()
    r = await env.client.post(
        "/api/sources", json={"owner": "a", "repo": "b"}, headers={"X-Requested-With": ""}
    )
    assert r.status_code == 403
    r = await env.client.put("/api/settings", json={}, headers={"X-Requested-With": "other"})
    assert r.status_code == 403
    assert (await env.client.get("/api/apps", headers={"X-Requested-With": ""})).status_code == 200


async def test_csrf_applies_to_login(env: ApiEnv) -> None:
    await env.login()
    r = await env.client.post("/api/login", json={"password": PASSWORD}, headers={"X-Requested-With": ""})
    assert r.status_code == 403


async def test_bearer_token() -> None:
    env = build_env(api_token="s3cret-token")
    try:
        assert (await env.client.get("/api/apps")).status_code == 401
        good = {"Authorization": "Bearer s3cret-token", "X-Requested-With": ""}
        assert (await env.client.get("/api/apps", headers=good)).status_code == 200
        r = await env.client.post("/api/sources", json={"owner": "a", "repo": "b"}, headers=good)
        assert r.status_code == 201
        bad = {"Authorization": "Bearer wrong"}
        assert (await env.client.get("/api/apps", headers=bad)).status_code == 401
    finally:
        await env.client.aclose()


async def test_bearer_disabled_without_env() -> None:
    env = build_env()
    try:
        r = await env.client.get("/api/apps", headers={"Authorization": "Bearer "})
        assert r.status_code == 401
    finally:
        await env.client.aclose()
