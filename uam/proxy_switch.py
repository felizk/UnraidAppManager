"""A ProxyManager that follows the NPM settings, so a settings change needs no restart."""

from __future__ import annotations

import httpx

from uam.clients.npm import NpmClient
from uam.contracts import SettingsStore, Vault
from uam.domain import ProxyHost, ProxySettings
from uam.errors import ValidationError


class SettingsBackedProxyManager:
    """Builds (and caches) an `NpmClient` from `Settings.npm_url` + `npm_credential_id` on each call.

    Read-only calls return empty results while NPM is not configured; writes raise ValidationError.
    """

    def __init__(self, settings: SettingsStore, vault: Vault, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._vault = vault
        self._http = http
        self._key: tuple[str, int, str, str] | None = None
        self._client: NpmClient | None = None

    async def _current(self) -> NpmClient | None:
        s = await self._settings.get()
        if not s.npm_url or s.npm_credential_id is None:
            return None
        cred = await self._vault.get(s.npm_credential_id)
        secret = await self._vault.reveal(s.npm_credential_id)
        key = (s.npm_url, cred.id, cred.username, secret)
        if self._client is None or key != self._key:
            self._client = NpmClient(s.npm_url, cred.username, secret, self._http)
            self._key = key
        return self._client

    async def _required(self) -> NpmClient:
        client = await self._current()
        if client is None:
            raise ValidationError("Nginx Proxy Manager is not configured (set its URL and credential)")
        return client

    async def ensure_proxy_host(
        self,
        domain: str,
        forward_host: str,
        forward_port: int,
        settings: ProxySettings,
        existing_id: int | None,
    ) -> int:
        """See `ProxyManager.ensure_proxy_host`."""
        client = await self._required()
        return await client.ensure_proxy_host(domain, forward_host, forward_port, settings, existing_id)

    async def delete_proxy_host(self, host_id: int) -> None:
        """See `ProxyManager.delete_proxy_host`."""
        await (await self._required()).delete_proxy_host(host_id)

    async def find_by_domain(self, domain: str) -> ProxyHost | None:
        """See `ProxyManager.find_by_domain`; None while NPM is not configured."""
        client = await self._current()
        return await client.find_by_domain(domain) if client else None

    async def list_proxy_hosts(self) -> list[ProxyHost]:
        """See `ProxyManager.list_proxy_hosts`; empty while NPM is not configured."""
        client = await self._current()
        return await client.list_proxy_hosts() if client else []

    async def claim_proxy_host(self, host_id: int, app_name: str) -> None:
        """See `ProxyManager.claim_proxy_host`."""
        await (await self._required()).claim_proxy_host(host_id, app_name)

    async def list_certificates(self) -> list[tuple[int, str]]:
        """See `ProxyManager.list_certificates`; empty while NPM is not configured."""
        client = await self._current()
        return await client.list_certificates() if client else []

    async def list_access_lists(self) -> list[tuple[int, str]]:
        """See `ProxyManager.list_access_lists`; empty while NPM is not configured."""
        client = await self._current()
        return await client.list_access_lists() if client else []
