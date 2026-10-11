"""Registry client: resolves the current manifest digest of an image tag without pulling."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from uam.domain import RegistryAuth
from uam.errors import AuthError, NotFound, RateLimited, RemoteError

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

_DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
_DOCKER_HUB_API = "registry-1.docker.io"
_DEFAULT_TOKEN_TTL = 60
_DEFAULT_RETRY_AFTER = 60
_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')

_TokenKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ImageRef:
    """A parsed image reference."""

    registry: str  # API host, e.g. registry-1.docker.io
    repository: str
    tag: str
    digest: str | None


def parse_image_ref(image_ref: str) -> ImageRef:
    """Split an image reference into API host, repository, tag and optional digest."""
    rest = image_ref.strip()
    digest: str | None = None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
    first, sep, remainder = rest.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        host, path = first.lower(), remainder
    else:
        host, path = "docker.io", rest
    tag = "latest"
    if ":" in path.rsplit("/", 1)[-1]:
        path, tag = path.rsplit(":", 1)
    if host in _DOCKER_HUB_ALIASES:
        host = _DOCKER_HUB_API
        if "/" not in path:
            path = f"library/{path}"
    return ImageRef(registry=host, repository=path, tag=tag, digest=digest)


def _parse_challenge(header: str) -> dict[str, str]:
    """Parse the parameters of a `WWW-Authenticate: Bearer ...` header."""
    if not header.lower().startswith("bearer"):
        return {}
    return {k.lower(): v for k, v in _PARAM_RE.findall(header)}


class HttpRegistryClient:
    """RegistryClient implementation speaking the Docker Registry HTTP API v2."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        insecure_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
    ) -> None:
        # Hosts (without port) reached over plain HTTP, like Docker's insecure-registries default.
        self._insecure_hosts = insecure_hosts
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._monotonic = monotonic
        self._now = now
        self._tokens: dict[_TokenKey, tuple[str, float]] = {}

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get_digest(self, image_ref: str, auth: RegistryAuth | None) -> str:
        """Return the manifest digest (manifest-list digest for multi-arch) of an image ref."""
        ref = parse_image_ref(image_ref)
        if ref.digest:
            return ref.digest
        scheme = "http" if ref.registry.split(":", 1)[0] in self._insecure_hosts else "https"
        url = f"{scheme}://{ref.registry}/v2/{ref.repository}/manifests/{ref.tag}"
        key = (ref.registry, ref.repository, auth.username if auth else "")

        resp = await self._request("HEAD", url, key, auth)
        digest = resp.headers.get("Docker-Content-Digest")
        if digest:
            return str(digest)
        resp = await self._request("GET", url, key, auth)
        digest = resp.headers.get("Docker-Content-Digest")
        if digest:
            return str(digest)
        return "sha256:" + hashlib.sha256(resp.content).hexdigest()

    async def _request(
        self, method: str, url: str, key: _TokenKey, auth: RegistryAuth | None
    ) -> httpx.Response:
        resp = await self._send(method, url, self._cached_token(key))
        if resp.status_code == 401:
            params = _parse_challenge(resp.headers.get("WWW-Authenticate", ""))
            if not params.get("realm"):
                raise AuthError(f"registry denied access to {url}")
            self._tokens.pop(key, None)
            token = await self._fetch_token(params, key, auth)
            resp = await self._send(method, url, token)
            if resp.status_code == 401:
                raise AuthError(f"registry rejected credentials for {url}")
        self._raise_for_status(resp)
        return resp

    async def _send(self, method: str, url: str, token: str | None) -> httpx.Response:
        headers = {"Accept": MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await self._client.request(method, url, headers=headers)

    def _cached_token(self, key: _TokenKey) -> str | None:
        entry = self._tokens.get(key)
        if entry is None:
            return None
        token, expires = entry
        if self._monotonic() >= expires:
            del self._tokens[key]
            return None
        return token

    async def _fetch_token(self, params: dict[str, str], key: _TokenKey, auth: RegistryAuth | None) -> str:
        query: dict[str, str] = {k: params[k] for k in ("service", "scope") if k in params}
        if auth:
            resp = await self._client.get(
                params["realm"], params=query, auth=httpx.BasicAuth(auth.username, auth.password)
            )
        else:
            resp = await self._client.get(params["realm"], params=query)
        if resp.status_code in (401, 403):
            raise AuthError(f"token endpoint refused credentials ({resp.status_code})")
        self._raise_for_status(resp)
        try:
            data = resp.json()
            token = data.get("token") or data.get("access_token")
            ttl = int(data.get("expires_in") or _DEFAULT_TOKEN_TTL)
        except (ValueError, AttributeError, TypeError) as exc:
            raise RemoteError(resp.status_code, "invalid token response") from exc
        if not token:
            raise RemoteError(resp.status_code, "token response without token")
        self._tokens[key] = (str(token), self._monotonic() + ttl)
        return str(token)

    def _raise_for_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status == 404:
            raise NotFound(f"not found: {resp.request.url}")
        if status == 429:
            try:
                delay = int(resp.headers.get("Retry-After", ""))
            except ValueError:
                delay = _DEFAULT_RETRY_AFTER
            raise RateLimited(self._now() + timedelta(seconds=delay), "registry rate limited")
        if status in (401, 403):
            raise AuthError(f"registry denied access ({status})")
        if status >= 400:
            raise RemoteError(status, resp.text)
