"""Nginx Proxy Manager (v2 REST API) client implementing `ProxyManager`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from uam.domain import ProxyHost, ProxySettings
from uam.errors import AuthError, ConflictError, NotFound, RemoteError
from uam.logging import redact

MARKER_PREFIX = "# managed-by: unraid-app-manager"
_REFRESH_MARGIN = timedelta(minutes=5)


def marker_for(app_name: str) -> str:
    """Return the marker line identifying a UAM-managed host for `app_name`."""
    return f"{MARKER_PREFIX} app={app_name}"


def _is_managed(advanced_config: str) -> bool:
    return any(line.strip().startswith(MARKER_PREFIX) for line in advanced_config.splitlines())


def _to_host(raw: dict[str, Any]) -> ProxyHost:
    return ProxyHost(
        id=int(raw["id"]),
        domain_names=tuple(str(d) for d in raw.get("domain_names") or ()),
        forward_scheme=str(raw.get("forward_scheme", "http")),
        forward_host=str(raw.get("forward_host", "")),
        forward_port=int(raw.get("forward_port", 0)),
        certificate_id=int(raw.get("certificate_id") or 0) or None,
        ssl_forced=bool(raw.get("ssl_forced")),
        websockets=bool(raw.get("allow_websocket_upgrade")),
        access_list_id=int(raw.get("access_list_id") or 0) or None,
    )


def _parse_expires(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _match_domain(hosts: list[dict[str, Any]], domain: str) -> dict[str, Any] | None:
    wanted = domain.lower()
    for host in hosts:
        if any(str(d).lower() == wanted for d in host.get("domain_names") or ()):
            return host
    return None


class NpmClient:
    """Talks to NPM's REST API with a cached, auto-refreshed bearer token."""

    def __init__(
        self,
        base_url: str,
        identity: str,
        secret: str,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._identity = identity
        self._secret = secret
        self._http = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token: str | None = None
        self._expires: datetime | None = None
        self._lock = asyncio.Lock()

    # ---------- auth / transport ----------
    async def _login(self) -> str:
        try:
            resp = await self._http.post(
                f"{self._base}/api/tokens",
                json={"identity": self._identity, "secret": self._secret},
            )
        except httpx.HTTPError as exc:
            raise RemoteError(0, redact(str(exc))) from exc
        if resp.status_code in (400, 401, 403):
            raise AuthError("NPM rejected the credentials")
        if resp.status_code != 200:
            raise RemoteError(resp.status_code, redact(resp.text))
        data = resp.json()
        token = data.get("token")
        if not token:
            raise AuthError("NPM did not return a token (2FA challenge?)")
        self._token = str(token)
        try:
            self._expires = _parse_expires(str(data["expires"]))
        except (KeyError, ValueError):
            self._expires = self._clock() + timedelta(hours=1)
        return self._token

    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if (
                not force
                and self._token is not None
                and self._expires is not None
                and self._clock() < self._expires - _REFRESH_MARGIN
            ):
                return self._token
            return await self._login()

    async def _request(
        self, method: str, path: str, json: Any = None, ok_404: bool = False
    ) -> httpx.Response:
        token = await self._get_token()
        for attempt in (0, 1):
            try:
                resp = await self._http.request(
                    method,
                    f"{self._base}/api{path}",
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise RemoteError(0, redact(str(exc))) from exc
            if resp.status_code == 401 and attempt == 0:
                token = await self._get_token(force=True)
                continue
            break
        if resp.status_code == 401:
            raise AuthError("NPM rejected the token")
        if resp.status_code == 404:
            if ok_404:
                return resp
            raise NotFound(f"{method} {path}")
        if resp.status_code >= 400:
            raise RemoteError(resp.status_code, redact(resp.text))
        return resp

    async def _raw_hosts(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/nginx/proxy-hosts")
        data: list[dict[str, Any]] = resp.json()
        return data

    # ---------- ProxyManager ----------
    async def list_proxy_hosts(self) -> list[ProxyHost]:
        """Return every proxy host configured in NPM."""
        return [_to_host(h) for h in await self._raw_hosts()]

    async def find_by_domain(self, domain: str) -> ProxyHost | None:
        """Return the host serving `domain` (case-insensitive), if any."""
        raw = _match_domain(await self._raw_hosts(), domain)
        return _to_host(raw) if raw else None

    async def ensure_proxy_host(
        self,
        domain: str,
        forward_host: str,
        forward_port: int,
        settings: ProxySettings,
        existing_id: int | None,
    ) -> int:
        """Create or update the host for `domain`; idempotent. Returns its id."""
        hosts = await self._raw_hosts()
        target: dict[str, Any] | None = None
        by_id = False
        if existing_id is not None:
            target = next((h for h in hosts if h.get("id") == existing_id), None)
            by_id = target is not None
        if target is None:
            target = _match_domain(hosts, domain)
        desired: dict[str, Any] = {
            "forward_scheme": "http",
            "forward_host": forward_host,
            "forward_port": forward_port,
            "certificate_id": settings.certificate_id or 0,
            "ssl_forced": settings.ssl_forced,
            "allow_websocket_upgrade": settings.websockets,
            "access_list_id": settings.access_list_id or 0,
        }
        if target is None:
            body = {
                "domain_names": [domain],
                **desired,
                "block_exploits": True,
                "http2_support": True,
                "caching_enabled": False,
                "meta": {},
                "advanced_config": marker_for(domain),
                "locations": [],
            }
            resp = await self._request("POST", "/nginx/proxy-hosts", json=body)
            return int(resp.json()["id"])
        if not by_id and not _is_managed(str(target.get("advanced_config") or "")):
            raise ConflictError(f"{domain} is already served by NPM host {target['id']} (not UAM)")
        changed = {k: v for k, v in desired.items() if target.get(k) != v}
        host_id = int(target["id"])
        if changed:
            await self._request("PUT", f"/nginx/proxy-hosts/{host_id}", json=changed)
        return host_id

    async def delete_proxy_host(self, host_id: int) -> None:
        """Delete a host; a missing host counts as success."""
        await self._request("DELETE", f"/nginx/proxy-hosts/{host_id}", ok_404=True)

    async def claim_proxy_host(self, host_id: int, app_name: str) -> None:
        """Prefix the UAM marker to the host's advanced_config, keeping existing config."""
        hosts = await self._raw_hosts()
        host = next((h for h in hosts if h.get("id") == host_id), None)
        if host is None:
            raise NotFound(f"NPM proxy host {host_id}")
        current = str(host.get("advanced_config") or "")
        marker = marker_for(app_name)
        if any(line.strip() == marker for line in current.splitlines()):
            return
        if _is_managed(current):
            raise ConflictError(f"NPM host {host_id} is already managed by another app")
        new = f"{marker}\n{current}" if current else marker
        await self._request("PUT", f"/nginx/proxy-hosts/{host_id}", json={"advanced_config": new})

    async def list_certificates(self) -> list[tuple[int, str]]:
        """Return `(id, nice_name)` for every certificate."""
        resp = await self._request("GET", "/nginx/certificates")
        return [(int(c["id"]), str(c.get("nice_name", ""))) for c in resp.json()]

    async def list_access_lists(self) -> list[tuple[int, str]]:
        """Return `(id, name)` for every access list."""
        resp = await self._request("GET", "/nginx/access-lists")
        return [(int(a["id"]), str(a.get("name", ""))) for a in resp.json()]


__all__ = ["MARKER_PREFIX", "NpmClient", "marker_for"]
