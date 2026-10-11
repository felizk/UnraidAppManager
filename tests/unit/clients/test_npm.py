"""NpmClient tests with respx; no real network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from uam.clients.npm import NpmClient, marker_for
from uam.domain import ProxySettings
from uam.errors import AuthError, ConflictError, RemoteError

BASE = "http://npm:81"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
SETTINGS = ProxySettings(subdomain="a")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def host(id_: int = 1, adv: str = "", port: int = 8080) -> dict[str, Any]:
    return {
        "id": id_,
        "domain_names": ["a.example.com"],
        "forward_scheme": "http",
        "forward_host": "10.0.0.2",
        "forward_port": port,
        "certificate_id": 0,
        "ssl_forced": True,
        "allow_websocket_upgrade": True,
        "access_list_id": 0,
        "advanced_config": adv,
    }


def token_resp(token: str, hours: int = 1) -> httpx.Response:
    exp = (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return httpx.Response(200, json={"token": token, "expires": exp})


def token_calls(mock: respx.MockRouter) -> int:
    return sum(1 for c in mock.calls if c.request.url.path == "/api/tokens")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def mock() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.post("/api/tokens").mock(return_value=token_resp("t1"))
        yield m


@pytest.fixture
async def npm(mock: respx.MockRouter, clock: Clock) -> AsyncIterator[NpmClient]:
    async with httpx.AsyncClient() as http:
        yield NpmClient(BASE, "me@x.io", "pw", http, clock)


def hosts_route(mock: respx.MockRouter, hosts: list[dict[str, Any]]) -> respx.Route:
    return mock.get("/api/nginx/proxy-hosts").mock(return_value=httpx.Response(200, json=hosts))


async def test_token_cached(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [])
    await npm.list_proxy_hosts()
    await npm.list_proxy_hosts()
    assert token_calls(mock) == 1


async def test_token_refreshed_before_expiry(npm: NpmClient, mock: respx.MockRouter, clock: Clock) -> None:
    hosts_route(mock, [])
    await npm.list_proxy_hosts()
    clock.now = NOW + timedelta(minutes=56)
    await npm.list_proxy_hosts()
    assert token_calls(mock) == 2


async def test_refresh_once_on_401(npm: NpmClient, mock: respx.MockRouter) -> None:
    mock.post("/api/tokens").mock(side_effect=[token_resp("t1"), token_resp("t2")])
    route = mock.get("/api/nginx/proxy-hosts").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[host()])]
    )
    hosts = await npm.list_proxy_hosts()
    assert hosts[0].id == 1
    assert route.calls[1].request.headers["authorization"] == "Bearer t2"


async def test_persistent_401_raises(npm: NpmClient, mock: respx.MockRouter) -> None:
    mock.get("/api/nginx/proxy-hosts").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await npm.list_proxy_hosts()


async def test_bad_credentials(npm: NpmClient, mock: respx.MockRouter) -> None:
    mock.post("/api/tokens").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await npm.list_proxy_hosts()


async def test_server_error_maps(npm: NpmClient, mock: respx.MockRouter) -> None:
    mock.get("/api/nginx/proxy-hosts").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(RemoteError) as ei:
        await npm.list_proxy_hosts()
    assert ei.value.status == 500


async def test_find_by_domain_case_insensitive(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host()])
    found = await npm.find_by_domain("A.Example.COM")
    assert found is not None and found.id == 1
    assert await npm.find_by_domain("nope.example.com") is None


async def test_create(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [])
    post = mock.post("/api/nginx/proxy-hosts").mock(return_value=httpx.Response(201, json={"id": 7}))
    got = await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 8080, SETTINGS, None)
    assert got == 7
    body = json.loads(post.calls[0].request.content)
    assert body["domain_names"] == ["a.example.com"]
    assert body["certificate_id"] == 0 and body["access_list_id"] == 0
    assert body["advanced_config"].startswith("# managed-by: unraid-app-manager app=")
    assert body["locations"] == [] and body["block_exploits"] is True


async def test_idempotent_ensure(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(3, marker_for("a"))])
    put = mock.put("/api/nginx/proxy-hosts/3")
    got = await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 8080, SETTINGS, 3)
    assert got == 3 and not put.called


async def test_update_on_port_change(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(3, marker_for("a"))])
    put = mock.put("/api/nginx/proxy-hosts/3").mock(return_value=httpx.Response(200, json={}))
    await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 9090, SETTINGS, 3)
    assert json.loads(put.calls[0].request.content) == {"forward_port": 9090}


async def test_adopt_marked_host_by_domain(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(5, marker_for("old"))])
    assert await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 8080, SETTINGS, None) == 5


async def test_stale_existing_id_falls_back_to_domain(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(5, marker_for("a"))])
    assert await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 8080, SETTINGS, 99) == 5


async def test_foreign_host_refused(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(5, "client_max_body_size 1g;")])
    with pytest.raises(ConflictError):
        await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 8080, SETTINGS, None)


async def test_claim_keeps_config(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(5, "client_max_body_size 1g;")])
    put = mock.put("/api/nginx/proxy-hosts/5").mock(return_value=httpx.Response(200, json={}))
    await npm.claim_proxy_host(5, "app")
    body = json.loads(put.calls[0].request.content)
    assert list(body) == ["advanced_config"]
    assert body["advanced_config"] == f"{marker_for('app')}\nclient_max_body_size 1g;"


async def test_claim_same_app_noop_then_ensure_ok(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(5, marker_for("app"))])
    put = mock.put("/api/nginx/proxy-hosts/5")
    await npm.claim_proxy_host(5, "app")
    assert not put.called
    assert await npm.ensure_proxy_host("a.example.com", "10.0.0.2", 8080, SETTINGS, None) == 5


async def test_claim_other_app_conflicts(npm: NpmClient, mock: respx.MockRouter) -> None:
    hosts_route(mock, [host(5, marker_for("other"))])
    with pytest.raises(ConflictError):
        await npm.claim_proxy_host(5, "app")


async def test_delete_404_ok(npm: NpmClient, mock: respx.MockRouter) -> None:
    mock.delete("/api/nginx/proxy-hosts/9").mock(return_value=httpx.Response(404))
    await npm.delete_proxy_host(9)


async def test_list_certificates_and_access_lists(npm: NpmClient, mock: respx.MockRouter) -> None:
    mock.get("/api/nginx/certificates").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "nice_name": "wild"}])
    )
    mock.get("/api/nginx/access-lists").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "name": "lan"}])
    )
    assert await npm.list_certificates() == [(1, "wild")]
    assert await npm.list_access_lists() == [(2, "lan")]
