from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from tests.fakes import InMemoryHttpCache
from uam.clients.github import HttpGitHubClient, _is_template_path, _root_is_container
from uam.domain import Source
from uam.errors import AuthError, NotFound, RateLimited, RemoteError

BASE = "https://gh.test"
REPO = f"{BASE}/repos/me/app"
SRC = Source(id=1, owner="me", repo="app", ref="main")
CONTAINER_XML = '<?xml version="1.0"?>\n<Container version="2"><Name>x</Name></Container>'


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def commit_json(sha: str = "c1", date: str = "2026-01-02T03:04:05Z") -> list[dict[str, Any]]:
    return [{"sha": sha, "commit": {"committer": {"date": date}}}]


@pytest.fixture
def cache() -> InMemoryHttpCache:
    return InMemoryHttpCache()


@pytest.fixture
def sleep_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", mock)
    return mock


@pytest.fixture
async def client(cache: InMemoryHttpCache, sleep_mock: AsyncMock) -> AsyncIterator[HttpGitHubClient]:
    c = HttpGitHubClient(cache, base_url=BASE)
    yield c
    await c.aclose()


def test_template_path_rules() -> None:
    assert _is_template_path("a.xml")
    assert _is_template_path("unraid/a.xml")
    assert _is_template_path(".unraid/sub/dir/a.xml")
    assert _is_template_path("templates/a.xml")
    assert _is_template_path("other/a.xml")
    assert not _is_template_path("other/deep/a.xml")
    assert not _is_template_path("unraid/a.txt")


def test_root_check() -> None:
    assert _root_is_container(CONTAINER_XML)
    assert _root_is_container("﻿<!-- c --><Container>")
    assert not _root_is_container("<Other><Container/></Other>")
    assert not _root_is_container("<ContainerX/>")


@respx.mock
async def test_headers_with_and_without_token(client: HttpGitHubClient) -> None:
    route = respx.get(f"{BASE}/user").respond(200, headers={"x-oauth-scopes": "repo, read:org"})
    assert await client.validate_token("tok") == ["repo", "read:org"]
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["x-github-api-version"] == "2022-11-28"

    respx.get(f"{REPO}/commits").respond(200, json=commit_json(), headers={"etag": '"e"'})
    await client.latest_commit_for_path(SRC, "a.xml", None)
    assert "authorization" not in respx.calls.last.request.headers


@respx.mock
async def test_validate_token_fine_grained_empty(client: HttpGitHubClient) -> None:
    respx.get(f"{BASE}/user").respond(200)
    assert await client.validate_token("t") == []


@respx.mock
async def test_get_template(client: HttpGitHubClient) -> None:
    respx.get(f"{REPO}/contents/unraid/app.xml").respond(
        200, json={"sha": "blob1", "encoding": "base64", "content": b64(CONTAINER_XML)}
    )
    commits = respx.get(f"{REPO}/commits").respond(200, json=commit_json("abc"))
    t = await client.get_template(SRC, "unraid/app.xml", None)
    assert (t.path, t.sha, t.commit_sha, t.xml) == ("unraid/app.xml", "blob1", "abc", CONTAINER_XML)
    assert t.committed_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    q = commits.calls.last.request.url.params
    assert (q["path"], q["sha"], q["per_page"]) == ("unraid/app.xml", "main", "1")


@respx.mock
async def test_discover_with_explicit_path(client: HttpGitHubClient) -> None:
    src = Source(id=1, owner="me", repo="app", template_path="x/t.xml")
    respx.get(f"{REPO}/contents/x/t.xml").respond(
        200, json={"sha": "b", "encoding": "base64", "content": b64(CONTAINER_XML)}
    )
    respx.get(f"{REPO}/commits").respond(200, json=commit_json())
    tree = respx.get(f"{REPO}/git/trees/main")
    found = await client.discover_templates(src, None)
    assert [t.path for t in found] == ["x/t.xml"]
    assert not tree.called


@respx.mock
async def test_discover_via_tree(client: HttpGitHubClient) -> None:
    tree = [
        {"path": "unraid/a.xml", "type": "blob"},
        {"path": "unraid/notes.txt", "type": "blob"},
        {"path": "templates/b.xml", "type": "blob"},
        {"path": "root.xml", "type": "blob"},
        {"path": "src/deep/c.xml", "type": "blob"},
        {"path": "unraid", "type": "tree"},
    ]
    respx.get(f"{REPO}/git/trees/main").respond(200, json={"tree": tree})
    bodies = {
        "unraid/a.xml": CONTAINER_XML,
        "templates/b.xml": "<Other/>",
        "root.xml": CONTAINER_XML,
    }
    for p, xml in bodies.items():
        respx.get(f"{REPO}/contents/{p}").respond(
            200, json={"sha": "s-" + p, "encoding": "base64", "content": b64(xml)}
        )
    respx.get(f"{REPO}/commits").respond(200, json=commit_json())
    found = await client.discover_templates(SRC, "tok")
    assert [t.path for t in found] == ["root.xml", "unraid/a.xml"]
    assert respx.calls[0].request.url.params["recursive"] == "1"


@respx.mock
async def test_latest_commit_etag_round_trip(client: HttpGitHubClient, cache: InMemoryHttpCache) -> None:
    route = respx.get(f"{REPO}/commits")
    route.side_effect = [
        httpx.Response(200, json=commit_json("c9"), headers={"etag": '"e1"'}),
        httpx.Response(304),
    ]
    first = await client.latest_commit_for_path(SRC, "a.xml", "secret-token")
    assert first == ("c9", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert "if-none-match" not in route.calls[0].request.headers
    assert len(cache.data) == 1
    key = next(iter(cache.data))
    assert "secret-token" not in key
    assert cache.data[key][0] == '"e1"'

    assert await client.latest_commit_for_path(SRC, "a.xml", "secret-token") is None
    assert route.calls[1].request.headers["if-none-match"] == '"e1"'


@respx.mock
async def test_etag_cache_is_per_token(client: HttpGitHubClient, cache: InMemoryHttpCache) -> None:
    respx.get(f"{REPO}/commits").respond(200, json=commit_json(), headers={"etag": '"e"'})
    await client.latest_commit_for_path(SRC, "a.xml", "t1")
    await client.latest_commit_for_path(SRC, "a.xml", "t2")
    await client.latest_commit_for_path(SRC, "a.xml", None)
    assert len(cache.data) == 3


@respx.mock
async def test_latest_commit_empty_is_not_found(client: HttpGitHubClient) -> None:
    respx.get(f"{REPO}/commits").respond(200, json=[])
    with pytest.raises(NotFound):
        await client.latest_commit_for_path(SRC, "a.xml", None)


@respx.mock
async def test_rate_limit_tracking(client: HttpGitHubClient) -> None:
    assert await client.rate_limit_remaining_fraction() == 1.0
    respx.get(f"{BASE}/user").respond(
        200,
        headers={"x-ratelimit-remaining": "15", "x-ratelimit-limit": "60", "x-ratelimit-reset": "1"},
    )
    await client.validate_token("t")
    assert await client.rate_limit_remaining_fraction() == 0.25


@respx.mock
@pytest.mark.parametrize("status", [403, 429])
async def test_rate_limited(client: HttpGitHubClient, status: int) -> None:
    respx.get(f"{BASE}/user").respond(
        status,
        headers={
            "x-ratelimit-remaining": "0",
            "x-ratelimit-limit": "60",
            "x-ratelimit-reset": "1800000000",
        },
    )
    with pytest.raises(RateLimited) as ei:
        await client.validate_token("t")
    assert ei.value.reset_at == datetime.fromtimestamp(1800000000, UTC)
    assert await client.rate_limit_remaining_fraction() == 0.0


@respx.mock
async def test_plain_403_is_remote_error(client: HttpGitHubClient) -> None:
    respx.get(f"{BASE}/user").respond(
        403, json={"message": "forbidden"}, headers={"x-ratelimit-remaining": "5"}
    )
    with pytest.raises(RemoteError) as ei:
        await client.validate_token("t")
    assert ei.value.status == 403


@respx.mock
async def test_401_auth_error(client: HttpGitHubClient) -> None:
    respx.get(f"{BASE}/user").respond(401)
    with pytest.raises(AuthError):
        await client.validate_token("bad")


@respx.mock
async def test_404_message_mentions_private(client: HttpGitHubClient) -> None:
    respx.get(f"{REPO}/git/trees/main").respond(404)
    with pytest.raises(NotFound, match="private"):
        await client.discover_templates(SRC, None)
    with pytest.raises(NotFound) as ei:
        await client.discover_templates(SRC, "tok")
    assert "private" not in str(ei.value)


@respx.mock
async def test_5xx_is_remote_error(client: HttpGitHubClient) -> None:
    respx.get(f"{BASE}/user").respond(500, text="boom")
    with pytest.raises(RemoteError) as ei:
        await client.validate_token("t")
    assert ei.value.status == 500
    assert respx.calls.call_count == 1


@respx.mock
async def test_retry_then_success(client: HttpGitHubClient, sleep_mock: AsyncMock) -> None:
    route = respx.get(f"{BASE}/user")
    route.side_effect = [httpx.Response(502), httpx.Response(503), httpx.Response(200)]
    assert await client.validate_token("t") == []
    assert route.call_count == 3
    assert sleep_mock.await_count == 2


@respx.mock
async def test_retry_exhausted(client: HttpGitHubClient) -> None:
    route = respx.get(f"{BASE}/user").respond(504)
    with pytest.raises(RemoteError) as ei:
        await client.validate_token("t")
    assert ei.value.status == 504
    assert route.call_count == 4


@respx.mock
async def test_transport_error_is_remote_error(client: HttpGitHubClient) -> None:
    respx.get(f"{BASE}/user").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(RemoteError):
        await client.validate_token("t")
