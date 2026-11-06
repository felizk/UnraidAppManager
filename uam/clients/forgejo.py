"""Forgejo/Gitea REST client, plus a router that picks GitHub or Forgejo per source."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from uam.contracts import GitHubClient, HttpCache
from uam.domain import DiscoveredTemplate, Source, normalize_forge_url
from uam.errors import ValidationError

from .github import HttpGitHubClient

_MAX_TREE_PAGES = 20
_TREE_PAGE_SIZE = "1000"


class HttpForgejoClient(HttpGitHubClient):
    """`GitHubClient` for Forgejo and Gitea servers (`/api/v1`), addressed by `Source.forge_url`.

    The API mirrors GitHub's closely: only the base URL, the auth header, commit paging
    (`limit` instead of `per_page`) and tree paging differ.
    """

    _product = "Forgejo"

    def __init__(
        self,
        cache: HttpCache,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(cache, base_url="", client=client, timeout=timeout)

    async def validate_token(self, token: str, forge_url: str = "") -> list[str]:
        """Call `GET /api/v1/user` on `forge_url`; Forgejo does not report scopes, so this returns []."""
        if not forge_url:
            raise ValidationError("a Forgejo token needs the server URL (set it as the credential's host)")
        await self._request("GET", f"{self._api_root(forge_url)}/user", token, what="token")
        return []

    @staticmethod
    def _api_root(forge_url: str) -> str:
        return f"{normalize_forge_url(forge_url)}/api/v1"

    def _api_base(self, source: Source) -> str:
        if not source.forge_url:
            raise ValidationError(f"source {source.owner}/{source.repo} has no Forgejo URL")
        return self._api_root(source.forge_url)

    def _commit_params(self, source: Source, path: str) -> dict[str, str]:
        return {
            "path": path,
            "sha": source.ref,
            "limit": "1",
            "stat": "false",
            "verification": "false",
            "files": "false",
        }

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"token {token}"}

    def _default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    async def _list_tree(self, source: Source, token: str | None) -> list[dict[str, Any]]:
        url = f"{self._repo_url(source)}/git/trees/{quote(source.ref, safe='')}"
        entries: list[dict[str, Any]] = []
        for page in range(1, _MAX_TREE_PAGES + 1):
            resp = await self._request(
                "GET",
                url,
                token,
                params={"recursive": "true", "per_page": _TREE_PAGE_SIZE, "page": str(page)},
                what=f"{source.owner}/{source.repo}",
            )
            data = resp.json()
            entries.extend(data.get("tree") or [])
            if not data.get("truncated"):
                break
        return entries


class ForgeRouter:
    """`GitHubClient` that sends each call to GitHub or Forgejo depending on `Source.forge_url`."""

    def __init__(self, github: GitHubClient, forgejo: GitHubClient) -> None:
        self._github = github
        self._forgejo = forgejo

    def _for(self, source: Source) -> GitHubClient:
        return self._forgejo if source.forge_url else self._github

    async def discover_templates(self, source: Source, token: str | None) -> list[DiscoveredTemplate]:
        """Find templates in the source's repo."""
        return await self._for(source).discover_templates(source, token)

    async def get_template(self, source: Source, path: str, token: str | None) -> DiscoveredTemplate:
        """Fetch one template file."""
        return await self._for(source).get_template(source, path, token)

    async def latest_commit_for_path(
        self, source: Source, path: str, token: str | None
    ) -> tuple[str, datetime] | None:
        """Newest commit for `path`, or None when unchanged (GitHub ETag)."""
        return await self._for(source).latest_commit_for_path(source, path, token)

    async def rate_limit_remaining_fraction(self) -> float:
        """GitHub's remaining quota; Forgejo servers are not rate limited the same way."""
        return await self._github.rate_limit_remaining_fraction()

    async def validate_token(self, token: str, forge_url: str = "") -> list[str]:
        """Check a token against GitHub, or against `forge_url` when given."""
        if forge_url:
            return await self._forgejo.validate_token(token, forge_url)
        return await self._github.validate_token(token)
