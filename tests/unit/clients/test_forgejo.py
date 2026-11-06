from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from tests.fakes import FakeGitHub, InMemoryHttpCache
from uam.clients.forgejo import ForgeRouter, HttpForgejoClient
from uam.domain import Source, normalize_forge_url
from uam.errors import AuthError, NotFound, ValidationError

FORGE = "https://git.test"
REPO = f"{FORGE}/api/v1/repos/me/app"
SRC = Source(id=1, owner="me", repo="app", ref="main", forge_url=FORGE)
XML = '<?xml version="1.0"?>\n<Container version="2"><Name>x</Name></Container>'


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def commits(sha: str = "c1") -> list[dict[str, Any]]:
    return [{"sha": sha, "commit": {"committer": {"date": "2026-01-02T03:04:05Z"}}}]


@pytest.fixture
async def client() -> AsyncIterator[HttpForgejoClient]:
    c = HttpForgejoClient(InMemoryHttpCache())
    yield c
    await c.aclose()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("git.example.com", "https://git.example.com"),
        ("https://git.example.com/", "https://git.example.com"),
        ("http://10.0.0.2:3000", "http://10.0.0.2:3000"),
        ("https://example.com/forge/api/v1", "https://example.com/forge"),
    ],
)
def test_normalize_forge_url(raw: str, expected: str) -> None:
    assert normalize_forge_url(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://host", "https://", "https:///x", "not a url"])
def test_normalize_forge_url_rejects(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_forge_url(raw)


@respx.mock
async def test_discover_pages_tree_and_uses_forgejo_params(client: HttpForgejoClient) -> None:
    tree = respx.get(f"{REPO}/git/trees/main").mock(
        side_effect=[
            httpx.Response(200, json={"tree": [{"path": "unraid/a.xml", "type": "blob"}], "truncated": True}),
            httpx.Response(200, json={"tree": [{"path": "b.xml", "type": "blob"}], "truncated": False}),
        ]
    )
    respx.get(f"{REPO}/contents/unraid/a.xml").respond(
        200, json={"sha": "s1", "encoding": "base64", "content": b64(XML)}
    )
    respx.get(f"{REPO}/contents/b.xml").respond(
        200, json={"sha": "s2", "encoding": "base64", "content": b64("<x/>")}
    )
    commit_route = respx.get(f"{REPO}/commits").respond(200, json=commits("c9"))

    found = await client.discover_templates(SRC, "tok")

    assert [(t.path, t.commit_sha) for t in found] == [("unraid/a.xml", "c9")]
    assert [c.request.url.params["page"] for c in tree.calls] == ["1", "2"]
    assert tree.calls[0].request.url.params["recursive"] == "true"
    assert tree.calls[0].request.headers["authorization"] == "token tok"
    params = commit_route.calls[0].request.url.params
    assert (params["limit"], params["sha"], params["path"]) == ("1", "main", "unraid/a.xml")


@respx.mock
async def test_latest_commit_without_etag_returns_commit_each_time(client: HttpForgejoClient) -> None:
    respx.get(f"{REPO}/commits").respond(200, json=commits("c2"))
    assert (await client.latest_commit_for_path(SRC, "a.xml", None) or ("",))[0] == "c2"
    assert (await client.latest_commit_for_path(SRC, "a.xml", None) or ("",))[0] == "c2"


@respx.mock
async def test_errors_name_forgejo(client: HttpForgejoClient) -> None:
    respx.get(f"{REPO}/contents/a.xml").respond(404, json={"message": "not found"})
    with pytest.raises(NotFound, match="Forgejo 404"):
        await client.get_template(SRC, "a.xml", None)


@respx.mock
async def test_validate_token(client: HttpForgejoClient) -> None:
    route = respx.get(f"{FORGE}/api/v1/user").respond(200, json={"login": "me"})
    assert await client.validate_token("tok", forge_url=f"{FORGE}/") == []
    assert route.calls[0].request.headers["authorization"] == "token tok"
    respx.get(f"{FORGE}/api/v1/user").respond(401)
    with pytest.raises(AuthError, match="Forgejo"):
        await client.validate_token("bad", forge_url=FORGE)


async def test_validate_token_needs_url(client: HttpForgejoClient) -> None:
    with pytest.raises(ValidationError):
        await client.validate_token("tok")


async def test_source_without_forge_url_is_rejected(client: HttpForgejoClient) -> None:
    with pytest.raises(ValidationError):
        await client.get_template(Source(1, "me", "app"), "a.xml", None)


async def test_router_dispatches_on_forge_url() -> None:
    github, forgejo = FakeGitHub(), FakeGitHub()
    github.put_file("me", "app", "a.xml", XML, commit_sha="gh")
    forgejo.put_file("me", "app", "a.xml", XML, commit_sha="fj")
    forgejo.token_scopes["t"] = []
    github.rate_fraction = 0.25
    router = ForgeRouter(github, forgejo)

    assert (await router.get_template(Source(1, "me", "app"), "a.xml", None)).commit_sha == "gh"
    assert (await router.get_template(SRC, "a.xml", None)).commit_sha == "fj"
    assert [t.commit_sha for t in await router.discover_templates(SRC, None)] == ["fj"]
    assert (await router.latest_commit_for_path(SRC, "a.xml", None) or ("",))[0] == "fj"
    assert await router.rate_limit_remaining_fraction() == 0.25
    assert await router.validate_token("t", forge_url=FORGE) == []
    with pytest.raises(AuthError):
        await router.validate_token("t")
