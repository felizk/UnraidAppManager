"""The REST endpoints and HTML panel for UAM's own updates."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.unit.web.test_api_support import ApiEnv, build_env
from tests.unit.web.test_ui_support import HX, UiEnv, build_ui_env
from uam.errors import ConflictError, UamError
from uam.self_update import SelfStatus

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


class StubSelfUpdater:
    def __init__(self) -> None:
        self.state = SelfStatus(
            supported=True,
            container_name="unraid-app-manager",
            image="ghcr.io/felizk/unraid-app-manager:latest",
            revision="abcdef1234",
            current_digest=OLD,
        )
        self.checks = 0
        self.starts = 0
        self.fail: UamError | None = None

    async def status(self) -> SelfStatus:
        return self.state

    async def check(self) -> SelfStatus:
        self.checks += 1
        self.state.latest_digest = NEW
        return self.state

    async def start_update(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.starts += 1
        self.state.updating = True


@pytest.fixture
async def api() -> AsyncIterator[tuple[ApiEnv, StubSelfUpdater]]:
    e = build_env()
    stub = StubSelfUpdater()
    e.services.self_updater = stub
    await e.login()
    yield e, stub
    await e.client.aclose()


@pytest.fixture
async def ui() -> AsyncIterator[tuple[UiEnv, StubSelfUpdater]]:
    e = build_ui_env()
    stub = StubSelfUpdater()
    e.api.services.self_updater = stub
    await e.login()
    yield e, stub
    await e.client.aclose()


async def test_api_status_check_and_update(api: tuple[ApiEnv, StubSelfUpdater]) -> None:
    env, stub = api
    body = (await env.client.get("/api/self")).json()
    assert body["supported"] and body["current_digest"] == OLD and body["update_available"] is False
    body = (await env.client.post("/api/self/check")).json()
    assert body["latest_digest"] == NEW and body["update_available"] is True
    r = await env.client.post("/api/self/update")
    assert r.status_code == 202 and stub.starts == 1
    stub.fail = ConflictError("an update of UAM is already running")
    assert (await env.client.post("/api/self/update")).status_code == 409


async def test_api_without_self_updater() -> None:
    env = build_env()
    await env.login()
    assert (await env.client.get("/api/self")).status_code == 422
    await env.client.aclose()


async def test_panel_offers_the_update(ui: tuple[UiEnv, StubSelfUpdater]) -> None:
    env, stub = ui
    r = await env.client.get("/fragments/self", headers=HX)
    assert "Check now" in r.text and "Update UAM" not in r.text and "abcdef1" in r.text
    r = await env.client.post("/self/check", headers=HX)
    assert stub.checks == 1 and "update available" in r.text and "Update UAM" in r.text
    r = await env.client.post("/self/update", headers=HX)
    assert stub.starts == 1 and "UAM restarts in a minute" in r.text and 'hx-trigger="every 5s"' in r.text


async def test_panel_shows_start_errors_and_unsupported(ui: tuple[UiEnv, StubSelfUpdater]) -> None:
    env, stub = ui
    stub.fail = ConflictError("an update of UAM is already running")
    r = await env.client.post("/self/update", headers=HX)
    assert r.status_code == 200 and "already running" in r.text
    stub.state = SelfStatus(supported=False, reason="the Docker socket isn't mounted at /var/run/docker.sock")
    r = await env.client.get("/fragments/self", headers=HX)
    assert (
        "mounted at /var/run/docker.sock" in r.text
        and "Unraid's Docker tab" in r.text
        and "Check now" not in r.text
    )


async def test_banner_and_pages(ui: tuple[UiEnv, StubSelfUpdater]) -> None:
    env, stub = ui
    assert (await env.client.get("/fragments/self/banner", headers=HX)).text.strip() == ""
    stub.state.latest_digest = NEW
    assert "A new version of UAM is available" in (await env.client.get("/fragments/self/banner")).text
    stub.state.updating = True
    assert "updating itself" in (await env.client.get("/fragments/self/banner")).text
    assert 'hx-get="/fragments/self/banner"' in (await env.client.get("/")).text
    assert 'hx-get="/fragments/self"' in (await env.client.get("/settings")).text


async def test_panel_without_self_updater() -> None:
    env = build_ui_env()
    await env.login()
    assert "isn't available in this mode" in (await env.client.get("/fragments/self", headers=HX)).text
    assert "not available" in (await env.client.post("/self/update", headers=HX)).text
    await env.client.aclose()
