"""Tests for HttpRegistryClient (HTTP mocked with respx)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from uam.clients.registry import MANIFEST_ACCEPT, HttpRegistryClient, parse_image_ref
from uam.domain import RegistryAuth
from uam.errors import AuthError, NotFound, RateLimited, RemoteError

DIGEST = "sha256:" + "a" * 64
INDEX_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
async def client() -> AsyncIterator[HttpRegistryClient]:
    c = HttpRegistryClient()
    yield c
    await c.aclose()


def challenge(realm: str, service: str, scope: str) -> httpx.Response:
    header = f'Bearer realm="{realm}",service="{service}",scope="{scope}"'
    return httpx.Response(401, headers={"WWW-Authenticate": header})


@pytest.mark.parametrize(
    ("ref", "host", "repo", "tag"),
    [
        ("nginx", "registry-1.docker.io", "library/nginx", "latest"),
        ("nginx:1.25", "registry-1.docker.io", "library/nginx", "1.25"),
        ("docker.io/foo/bar:v2", "registry-1.docker.io", "foo/bar", "v2"),
        ("ghcr.io/me/app", "ghcr.io", "me/app", "latest"),
        ("localhost:5000/x/y:t", "localhost:5000", "x/y", "t"),
        ("user/app", "registry-1.docker.io", "user/app", "latest"),
    ],
)
def test_parse(ref: str, host: str, repo: str, tag: str) -> None:
    p = parse_image_ref(ref)
    assert (p.registry, p.repository, p.tag) == (host, repo, tag)


def test_parse_digest() -> None:
    assert parse_image_ref(f"ghcr.io/me/app@{DIGEST}").digest == DIGEST


async def test_digest_in_ref_short_circuits(client: HttpRegistryClient) -> None:
    with respx.mock(assert_all_called=False) as m:
        assert await client.get_digest(f"nginx@{DIGEST}", None) == DIGEST
        assert not m.calls


async def test_ghcr_public(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        manifest = m.head("https://ghcr.io/v2/me/app/manifests/latest")
        manifest.side_effect = [
            challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull"),
            httpx.Response(200, headers={"Docker-Content-Digest": DIGEST}),
        ]
        token = m.get("https://ghcr.io/token").respond(json={"token": "tok", "expires_in": 300})
        assert await client.get_digest("ghcr.io/me/app:latest", None) == DIGEST
        req = token.calls.last.request
        assert "authorization" not in req.headers
        assert req.url.params["scope"] == "repository:me/app:pull"
        assert req.url.params["service"] == "ghcr.io"
        last = manifest.calls.last.request
        assert last.headers["authorization"] == "Bearer tok"
        assert last.headers["accept"] == MANIFEST_ACCEPT


async def test_ghcr_private_basic_then_bearer(client: HttpRegistryClient) -> None:
    auth = RegistryAuth(username="me", password="pat", registry="ghcr.io")
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/priv/manifests/1").side_effect = [
            challenge("https://ghcr.io/token", "ghcr.io", "repository:me/priv:pull"),
            httpx.Response(200, headers={"Docker-Content-Digest": DIGEST}),
        ]
        token = m.get("https://ghcr.io/token").respond(json={"token": "tok"})
        assert await client.get_digest("ghcr.io/me/priv:1", auth) == DIGEST
        assert token.calls.last.request.headers["authorization"].startswith("Basic ")


async def test_docker_hub_library(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://registry-1.docker.io/v2/library/nginx/manifests/latest").side_effect = [
            challenge("https://auth.docker.io/token", "registry.docker.io", "repository:library/nginx:pull"),
            httpx.Response(200, headers={"Docker-Content-Digest": INDEX_DIGEST}),
        ]
        m.get("https://auth.docker.io/token").respond(json={"access_token": "t", "expires_in": 60})
        assert await client.get_digest("nginx", None) == INDEX_DIGEST


async def test_multiarch_index_digest_is_returned(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/multi/manifests/latest").respond(
            headers={
                "Docker-Content-Digest": INDEX_DIGEST,
                "Content-Type": "application/vnd.oci.image.index.v1+json",
            }
        )
        assert await client.get_digest("ghcr.io/me/multi", None) == INDEX_DIGEST


async def test_head_without_digest_falls_back_to_get_header(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").respond(200)
        m.get("https://ghcr.io/v2/me/app/manifests/latest").respond(
            200, headers={"Docker-Content-Digest": DIGEST}, content=b"{}"
        )
        assert await client.get_digest("ghcr.io/me/app", None) == DIGEST


async def test_head_without_digest_computes_body_hash(client: HttpRegistryClient) -> None:
    body = b'{"schemaVersion":2}'
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").respond(200)
        m.get("https://ghcr.io/v2/me/app/manifests/latest").respond(200, content=body)
        assert await client.get_digest("ghcr.io/me/app", None) == "sha256:" + hashlib.sha256(body).hexdigest()


async def test_token_cached_between_lookups(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        manifest = m.head("https://ghcr.io/v2/me/app/manifests/latest")
        manifest.side_effect = [
            challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull"),
            httpx.Response(200, headers={"Docker-Content-Digest": DIGEST}),
            httpx.Response(200, headers={"Docker-Content-Digest": DIGEST}),
        ]
        token = m.get("https://ghcr.io/token").respond(json={"token": "tok", "expires_in": 300})
        await client.get_digest("ghcr.io/me/app", None)
        await client.get_digest("ghcr.io/me/app", None)
        assert token.call_count == 1
        assert manifest.calls.last.request.headers["authorization"] == "Bearer tok"


async def test_expired_token_is_refetched() -> None:
    clock = [0.0]
    c = HttpRegistryClient(monotonic=lambda: clock[0])
    try:
        with respx.mock() as m:
            manifest = m.head("https://ghcr.io/v2/me/app/manifests/latest")
            ch = challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull")
            ok = httpx.Response(200, headers={"Docker-Content-Digest": DIGEST})
            manifest.side_effect = [ch, ok, ch, ok]
            token = m.get("https://ghcr.io/token").respond(json={"token": "tok", "expires_in": 100})
            await c.get_digest("ghcr.io/me/app", None)
            clock[0] = 101.0
            await c.get_digest("ghcr.io/me/app", None)
            assert token.call_count == 2
    finally:
        await c.aclose()


async def test_token_cache_is_per_user(client: HttpRegistryClient) -> None:
    ch = challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull")
    ok = httpx.Response(200, headers={"Docker-Content-Digest": DIGEST})
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").side_effect = [ch, ok, ch, ok]
        token = m.get("https://ghcr.io/token").respond(json={"token": "tok"})
        await client.get_digest("ghcr.io/me/app", None)
        await client.get_digest("ghcr.io/me/app", RegistryAuth("me", "pat", "ghcr.io"))
        assert token.call_count == 2


@pytest.mark.parametrize(
    ("status", "exc"),
    [(404, NotFound), (429, RateLimited), (500, RemoteError), (403, AuthError)],
)
async def test_error_table(client: HttpRegistryClient, status: int, exc: type[Exception]) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").respond(status)
        with pytest.raises(exc):
            await client.get_digest("ghcr.io/me/app", None)


async def test_429_uses_retry_after(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").respond(429, headers={"Retry-After": "120"})
        with pytest.raises(RateLimited):
            await client.get_digest("ghcr.io/me/app", None)


async def test_401_after_token_is_auth_error(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        ch = challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull")
        m.head("https://ghcr.io/v2/me/app/manifests/latest").side_effect = [ch, ch]
        m.get("https://ghcr.io/token").respond(json={"token": "tok"})
        with pytest.raises(AuthError):
            await client.get_digest("ghcr.io/me/app", None)


async def test_token_endpoint_denied_is_auth_error(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").mock(
            return_value=challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull")
        )
        m.get("https://ghcr.io/token").respond(401)
        with pytest.raises(AuthError):
            await client.get_digest("ghcr.io/me/app", RegistryAuth("me", "bad", "ghcr.io"))


async def test_401_without_bearer_challenge_is_auth_error(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").respond(401)
        with pytest.raises(AuthError):
            await client.get_digest("ghcr.io/me/app", None)


@respx.mock
async def test_localhost_registry_uses_plain_http() -> None:
    route = respx.head("http://localhost:5000/v2/app/manifests/dev").mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": "sha256:local"})
    )
    client = HttpRegistryClient()
    assert await client.get_digest("localhost:5000/app:dev", None) == "sha256:local"
    assert route.called
    await client.aclose()


async def test_refused_credential_falls_back_to_anonymous(client: HttpRegistryClient) -> None:
    """A bad stored token must not break update checks for a public image."""
    with respx.mock() as m:
        manifest = m.head("https://ghcr.io/v2/me/app/manifests/latest")
        manifest.side_effect = [
            challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull"),
            httpx.Response(200, headers={"Docker-Content-Digest": DIGEST}),
        ]
        token = m.get("https://ghcr.io/token")
        token.side_effect = [httpx.Response(403), httpx.Response(200, json={"token": "anon"})]
        auth = RegistryAuth(username="u", password="bad", registry="ghcr.io")
        assert await client.get_digest("ghcr.io/me/app:latest", auth) == DIGEST
        assert "authorization" in token.calls[0].request.headers
        assert "authorization" not in token.calls[1].request.headers


async def test_refused_credential_on_private_image_explains(client: HttpRegistryClient) -> None:
    with respx.mock() as m:
        m.head("https://ghcr.io/v2/me/app/manifests/latest").mock(
            return_value=challenge("https://ghcr.io/token", "ghcr.io", "repository:me/app:pull")
        )
        m.get("https://ghcr.io/token").respond(403)
        auth = RegistryAuth(username="u", password="bad", registry="ghcr.io")
        with pytest.raises(AuthError, match="username and token scopes"):
            await client.get_digest("ghcr.io/me/app:latest", auth)
