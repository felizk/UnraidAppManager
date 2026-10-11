"""GitHub REST client used to discover and fetch Unraid templates."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from uam.contracts import HttpCache
from uam.domain import DiscoveredTemplate, Source
from uam.errors import AuthError, NotFound, RateLimited, RemoteError

DEFAULT_BASE_URL = "https://api.github.com"
_TEMPLATE_DIRS = frozenset({"unraid", ".unraid", "templates"})
_MAX_ROOT_DEPTH = 2
_RETRY_STATUSES = frozenset({502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5
_CONTAINER_RE = re.compile(r"<Container(?=[\s>/])")
_XML_PROLOG_RE = re.compile(r"(?:\s*<\?xml[^>]*\?>|\s*<!--.*?-->|\s*<!DOCTYPE[^>]*>)*\s*", re.S)


def _is_template_path(path: str) -> bool:
    """Return True for `*.xml` paths in a template directory or near the repo root."""
    if not path.endswith(".xml"):
        return False
    parts = path.split("/")
    return (parts[0] in _TEMPLATE_DIRS and len(parts) > 1) or len(parts) <= _MAX_ROOT_DEPTH


def _root_is_container(xml: str) -> bool:
    """Cheap check that the document's root element is `<Container>`."""
    body = xml.lstrip("﻿")
    prolog = _XML_PROLOG_RE.match(body)
    start = prolog.end() if prolog else 0
    return _CONTAINER_RE.match(body, start) is not None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class HttpGitHubClient:
    """`GitHubClient` implementation backed by httpx and an injected ETag cache."""

    def __init__(
        self,
        cache: HttpCache,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._cache = cache
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._remaining: int | None = None
        self._limit: int | None = None
        self._reset: datetime | None = None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    # ---------- public API ----------

    async def discover_templates(self, source: Source, token: str | None) -> list[DiscoveredTemplate]:
        """Find template XML files in the source repo and return those that are Containers."""
        if source.template_path:
            return [await self.get_template(source, source.template_path, token)]
        resp = await self._request(
            "GET",
            f"{self._repo_url(source)}/git/trees/{quote(source.ref, safe='')}",
            token,
            params={"recursive": "1"},
            what=f"{source.owner}/{source.repo}",
        )
        tree = resp.json().get("tree", [])
        paths = sorted(e["path"] for e in tree if e.get("type") == "blob" and _is_template_path(e["path"]))
        found: list[DiscoveredTemplate] = []
        for path in paths:
            sha, xml = await self._fetch_blob(source, path, token)
            if not _root_is_container(xml):
                continue
            commit_sha, committed_at = await self._fetch_commit(source, path, token)
            found.append(DiscoveredTemplate(path, sha, commit_sha, committed_at, xml))
        return found

    async def get_template(self, source: Source, path: str, token: str | None) -> DiscoveredTemplate:
        """Fetch one template file and the commit that last touched it."""
        sha, xml = await self._fetch_blob(source, path, token)
        commit_sha, committed_at = await self._fetch_commit(source, path, token)
        return DiscoveredTemplate(path, sha, commit_sha, committed_at, xml)

    async def latest_commit_for_path(
        self, source: Source, path: str, token: str | None
    ) -> tuple[str, datetime] | None:
        """Return the newest commit for `path`, or None when unchanged since the last call."""
        url = f"{self._repo_url(source)}/commits"
        params = {"path": path, "sha": source.ref, "per_page": "1"}
        key = self._cache_key(token, url, params)
        cached = await self._cache.get(key)
        headers = {"If-None-Match": cached[0]} if cached else {}
        resp = await self._request(
            "GET",
            url,
            token,
            params=params,
            headers=headers,
            what=f"{source.owner}/{source.repo}:{path}",
        )
        if resp.status_code == 304:
            return None
        etag = resp.headers.get("etag")
        if etag:
            await self._cache.put(key, etag, resp.text)
        return self._commit_from_list(resp.json(), path)

    async def rate_limit_remaining_fraction(self) -> float:
        """Return remaining/limit from the last response seen, or 1.0 if none."""
        if self._remaining is None or not self._limit:
            return 1.0
        return max(0.0, min(1.0, self._remaining / self._limit))

    async def validate_token(self, token: str) -> list[str]:
        """Call `GET /user` and return the token's OAuth scopes."""
        resp = await self._request("GET", f"{self._base_url}/user", token, what="token")
        raw = resp.headers.get("x-oauth-scopes", "")
        return [s.strip() for s in raw.split(",") if s.strip()]

    # ---------- helpers ----------

    def _repo_url(self, source: Source) -> str:
        return f"{self._base_url}/repos/{quote(source.owner)}/{quote(source.repo)}"

    @staticmethod
    def _cache_key(token: str | None, url: str, params: dict[str, str]) -> str:
        who = hashlib.sha256(token.encode()).hexdigest()[:16] if token else "anon"
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"github:{who}:{url}?{query}"

    async def _fetch_blob(self, source: Source, path: str, token: str | None) -> tuple[str, str]:
        resp = await self._request(
            "GET",
            f"{self._repo_url(source)}/contents/{quote(path)}",
            token,
            params={"ref": source.ref},
            what=f"{source.owner}/{source.repo}:{path}",
        )
        data = resp.json()
        if isinstance(data, list) or data.get("encoding") != "base64":
            raise RemoteError(resp.status_code, f"unexpected contents response for {path}")
        xml = base64.b64decode(data["content"]).decode("utf-8")
        return str(data["sha"]), xml

    async def _fetch_commit(self, source: Source, path: str, token: str | None) -> tuple[str, datetime]:
        resp = await self._request(
            "GET",
            f"{self._repo_url(source)}/commits",
            token,
            params={"path": path, "sha": source.ref, "per_page": "1"},
            what=f"{source.owner}/{source.repo}:{path}",
        )
        return self._commit_from_list(resp.json(), path)

    @staticmethod
    def _commit_from_list(commits: Any, path: str) -> tuple[str, datetime]:
        if not isinstance(commits, list) or not commits:
            raise NotFound(f"no commits found for {path}")
        first = commits[0]
        info = first["commit"]
        stamp = (info.get("committer") or info.get("author") or {}).get("date")
        if not stamp:
            raise RemoteError(200, f"commit for {path} has no date")
        return str(first["sha"]), _parse_time(stamp)

    def _track_rate_limit(self, resp: httpx.Response) -> None:
        h = resp.headers
        try:
            if "x-ratelimit-remaining" in h:
                self._remaining = int(h["x-ratelimit-remaining"])
            if "x-ratelimit-limit" in h:
                self._limit = int(h["x-ratelimit-limit"])
            if "x-ratelimit-reset" in h:
                self._reset = datetime.fromtimestamp(int(h["x-ratelimit-reset"]), UTC)
        except ValueError:
            pass

    async def _request(
        self,
        method: str,
        url: str,
        token: str | None,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        what: str = "",
    ) -> httpx.Response:
        all_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            all_headers["Authorization"] = f"Bearer {token}"
        all_headers.update(headers or {})

        attempt = 0
        while True:
            try:
                resp = await self._client.request(method, url, params=params, headers=all_headers)
            except httpx.HTTPError as exc:
                raise RemoteError(0, f"{type(exc).__name__}: {exc}") from exc
            self._track_rate_limit(resp)
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                await asyncio.sleep(_BACKOFF_BASE_S * 2**attempt)
                attempt += 1
                continue
            break
        return self._check(resp, what, token is None)

    def _check(self, resp: httpx.Response, what: str, anonymous: bool) -> httpx.Response:
        status = resp.status_code
        if status < 400:
            return resp
        if status in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = self._reset or datetime.now(UTC)
            raise RateLimited(reset, "GitHub rate limit exceeded")
        if status == 401:
            raise AuthError("GitHub rejected the token (401)")
        if status == 404:
            hint = " (a private repository looks like a 404 when no token is supplied)" if anonymous else ""
            raise NotFound(f"GitHub 404 for {what}{hint}")
        body = resp.text
        try:
            body = str(json.loads(body).get("message", body))
        except (ValueError, AttributeError):
            pass
        raise RemoteError(status, body)
