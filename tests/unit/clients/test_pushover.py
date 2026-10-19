"""Pushover notifier tests (HTTP mocked with respx)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from tests.fakes import FakeBundle
from uam.clients.pushover import PUSHOVER_URL, PushoverNotifier
from uam.domain import CredentialKind
from uam.errors import RemoteError, ValidationError

USER, TOKEN = "u" * 30, "a" * 30


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


async def configure(fakes: FakeBundle, **kw: object) -> int:
    cred = await fakes.vault.store("push", CredentialKind.PUSHOVER, USER, TOKEN)
    s = await fakes.settings.get()
    s.pushover_credential_id = cred.id
    for k, v in kw.items():
        setattr(s, k, v)
    await fakes.settings.save(s)
    return cred.id


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@respx.mock
async def test_notify_posts_message(fakes: FakeBundle, http: httpx.AsyncClient) -> None:
    await configure(fakes)
    route = respx.post(PUSHOVER_URL).respond(200, json={"status": 1})
    await PushoverNotifier(fakes.settings, fakes.vault, http).notify("update", "notes updated", "new image")
    assert form(route.calls.last.request) == {
        "token": TOKEN,
        "user": USER,
        "title": "notes updated",
        "message": "new image",
        "priority": "0",
    }


@respx.mock
async def test_notify_is_off_without_credential(fakes: FakeBundle, http: httpx.AsyncClient) -> None:
    route = respx.post(PUSHOVER_URL).respond(200)
    await PushoverNotifier(fakes.settings, fakes.vault, http).notify("crash", "x", "y")
    assert not route.called


@pytest.mark.parametrize(("topic", "setting"), [("update", "notify_on_update"), ("crash", "notify_on_crash")])
@respx.mock
async def test_notify_respects_topic_toggles(
    fakes: FakeBundle, http: httpx.AsyncClient, topic: str, setting: str
) -> None:
    await configure(fakes, **{setting: False})
    route = respx.post(PUSHOVER_URL).respond(200)
    notifier = PushoverNotifier(fakes.settings, fakes.vault, http)
    await notifier.notify(topic, "x", "y")  # type: ignore[arg-type]
    assert not route.called
    other = "crash" if topic == "update" else "update"
    await notifier.notify(other, "x", "y")  # type: ignore[arg-type]
    assert route.called


@respx.mock
async def test_notify_swallows_failures(fakes: FakeBundle, http: httpx.AsyncClient) -> None:
    await configure(fakes)
    respx.post(PUSHOVER_URL).respond(400, json={"errors": ["application token is invalid"], "status": 0})
    await PushoverNotifier(fakes.settings, fakes.vault, http).notify("crash", "x", "y")
    respx.post(PUSHOVER_URL).mock(side_effect=httpx.ConnectError("down"))
    await PushoverNotifier(fakes.settings, fakes.vault, http).notify("crash", "x", "y")


@respx.mock
async def test_notify_truncates_long_text(fakes: FakeBundle, http: httpx.AsyncClient) -> None:
    await configure(fakes)
    route = respx.post(PUSHOVER_URL).respond(200)
    await PushoverNotifier(fakes.settings, fakes.vault, http).notify("crash", "t" * 400, "m" * 5000)
    sent = form(route.calls.last.request)
    assert (len(sent["title"]), len(sent["message"])) == (250, 1024)


@respx.mock
async def test_send_test_reports_pushover_errors(fakes: FakeBundle, http: httpx.AsyncClient) -> None:
    cred_id = await configure(fakes)
    notifier = PushoverNotifier(fakes.settings, fakes.vault, http)
    respx.post(PUSHOVER_URL).respond(400, json={"errors": ["user identifier is not valid"], "status": 0})
    with pytest.raises(RemoteError, match="user identifier is not valid") as exc:
        await notifier.send_test()
    assert TOKEN not in str(exc.value)
    route = respx.post(PUSHOVER_URL).respond(200, json={"status": 1})
    await notifier.send_test(cred_id)
    assert "Test notification" in form(route.calls.last.request)["message"]


async def test_send_test_needs_configuration(fakes: FakeBundle, http: httpx.AsyncClient) -> None:
    notifier = PushoverNotifier(fakes.settings, fakes.vault, http)
    with pytest.raises(ValidationError, match="not configured"):
        await notifier.send_test()
    cred = await fakes.vault.store("push", CredentialKind.PUSHOVER, "", TOKEN)
    with pytest.raises(ValidationError, match="user key"):
        await notifier.send_test(cred.id)
