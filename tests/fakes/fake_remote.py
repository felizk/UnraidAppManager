"""Fakes for GitHub, the image registry, Nginx Proxy Manager and Pushover."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import replace
from datetime import UTC, datetime

from uam.contracts import NotifyTopic
from uam.domain import DiscoveredTemplate, ProxyHost, ProxySettings, RegistryAuth, Source
from uam.errors import AuthError, ConflictError, NotFound

DISCOVERY_DIRS = ("unraid", ".unraid", "templates")
DEFAULT_COMMIT_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _is_discoverable(path: str) -> bool:
    parts = path.split("/")
    return path.endswith(".xml") and (len(parts) == 1 or (len(parts) == 2 and parts[0] in DISCOVERY_DIRS))


class FakeGitHub:
    """Repos are `files[(owner, repo, ref, path)]`. Private repos need the token given to `make_private`."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str, str, str], DiscoveredTemplate] = {}
        self.private: dict[tuple[str, str], str] = {}
        self.token_scopes: dict[str, list[str]] = {}
        self.rate_fraction = 1.0
        self.calls: list[tuple[str, str]] = []
        self._seen_commit: dict[tuple[str, str, str, str], str] = {}

    def put_file(
        self,
        owner: str,
        repo: str,
        path: str,
        xml: str,
        ref: str = "main",
        commit_sha: str | None = None,
        committed_at: datetime | None = None,
    ) -> DiscoveredTemplate:
        """Create or replace a file (a new commit)."""
        blob = hashlib.sha1(xml.encode(), usedforsecurity=False).hexdigest()
        commit = commit_sha or hashlib.sha1(f"{path}{xml}".encode(), usedforsecurity=False).hexdigest()
        tpl = DiscoveredTemplate(
            path=path, sha=blob, commit_sha=commit, committed_at=committed_at or DEFAULT_COMMIT_TIME, xml=xml
        )
        self.files[(owner, repo, ref, path)] = tpl
        return tpl

    def make_private(self, owner: str, repo: str, token: str) -> None:
        self.private[(owner, repo)] = token

    def _check(self, source: Source, token: str | None) -> None:
        required = self.private.get((source.owner, source.repo))
        if required is not None and token != required:
            raise NotFound(f"{source.owner}/{source.repo} (private repo? check the token)")

    async def discover_templates(self, source: Source, token: str | None) -> list[DiscoveredTemplate]:
        self.calls.append(("discover", f"{source.owner}/{source.repo}"))
        self._check(source, token)
        if source.template_path:
            return [await self.get_template(source, source.template_path, token)]
        return [
            tpl
            for (o, r, ref, path), tpl in sorted(self.files.items())
            if (o, r, ref) == (source.owner, source.repo, source.ref)
            and _is_discoverable(path)
            and "<Container" in tpl.xml
        ]

    async def get_template(self, source: Source, path: str, token: str | None) -> DiscoveredTemplate:
        self.calls.append(("get", path))
        self._check(source, token)
        try:
            return self.files[(source.owner, source.repo, source.ref, path)]
        except KeyError:
            raise NotFound(path) from None

    async def latest_commit_for_path(
        self, source: Source, path: str, token: str | None
    ) -> tuple[str, datetime] | None:
        self.calls.append(("latest_commit", path))
        tpl = await self.get_template(source, path, token)
        key = (source.owner, source.repo, source.ref, path)
        if self._seen_commit.get(key) == tpl.commit_sha:
            return None
        self._seen_commit[key] = tpl.commit_sha
        return tpl.commit_sha, tpl.committed_at

    async def rate_limit_remaining_fraction(self) -> float:
        return self.rate_fraction

    async def validate_token(self, token: str) -> list[str]:
        if token not in self.token_scopes:
            raise AuthError("bad token")
        return self.token_scopes[token]


class FakeRegistry:
    """`digests[image_ref]` → digest. `require_auth[image_ref]` → the auth that must be presented."""

    def __init__(self) -> None:
        self.digests: dict[str, str] = {}
        self.require_auth: dict[str, RegistryAuth] = {}
        self.calls: list[str] = []

    def set_digest(self, image_ref: str, digest: str) -> None:
        self.digests[image_ref] = digest

    async def get_digest(self, image_ref: str, auth: RegistryAuth | None) -> str:
        self.calls.append(image_ref)
        if "@" in image_ref:
            return image_ref.split("@", 1)[1]
        needed = self.require_auth.get(image_ref)
        if needed is not None and auth != needed:
            raise AuthError(f"registry denied {image_ref}")
        if image_ref not in self.digests:
            raise NotFound(image_ref)
        return self.digests[image_ref]


class FakeProxyManager:
    """Proxy hosts in `hosts`; `owners[id]` is the UAM app name, "uam" for hosts made by ensure, or None
    for hosts UAM does not manage."""

    def __init__(self) -> None:
        self.hosts: dict[int, ProxyHost] = {}
        self.owners: dict[int, str | None] = {}
        self.settings: dict[int, ProxySettings] = {}
        self.certificates: list[tuple[int, str]] = [(1, "*.example.com")]
        self.access_lists: list[tuple[int, str]] = [(1, "LAN only")]
        self.calls: list[str] = []
        self._ids = itertools.count(1)

    def seed_foreign_host(self, domain: str, forward_host: str, forward_port: int) -> int:
        host_id = next(self._ids)
        self.hosts[host_id] = ProxyHost(host_id, (domain,), "http", forward_host, forward_port)
        self.owners[host_id] = None
        return host_id

    async def ensure_proxy_host(
        self,
        domain: str,
        forward_host: str,
        forward_port: int,
        settings: ProxySettings,
        existing_id: int | None,
    ) -> int:
        self.calls.append(f"ensure {domain}")
        target = existing_id if existing_id in self.hosts else None
        if target is None:
            found = await self.find_by_domain(domain)
            target = found.id if found else None
        if target is not None and self.owners[target] is None:
            raise ConflictError(f"{domain} is managed outside UAM")
        host_id = target if target is not None else next(self._ids)
        self.hosts[host_id] = ProxyHost(host_id, (domain,), "http", forward_host, forward_port)
        self.owners.setdefault(host_id, "uam")
        self.settings[host_id] = settings
        return host_id

    async def delete_proxy_host(self, host_id: int) -> None:
        self.calls.append(f"delete {host_id}")
        self.hosts.pop(host_id, None)
        self.owners.pop(host_id, None)

    async def find_by_domain(self, domain: str) -> ProxyHost | None:
        domain = domain.lower()
        return next((h for h in self.hosts.values() if domain in (d.lower() for d in h.domain_names)), None)

    async def list_proxy_hosts(self) -> list[ProxyHost]:
        return list(self.hosts.values())

    async def claim_proxy_host(self, host_id: int, app_name: str) -> None:
        self.calls.append(f"claim {host_id}")
        if host_id not in self.hosts:
            raise NotFound(f"proxy host {host_id}")
        self.owners[host_id] = app_name

    async def list_certificates(self) -> list[tuple[int, str]]:
        return list(self.certificates)

    async def list_access_lists(self) -> list[tuple[int, str]]:
        return list(self.access_lists)

    def retarget(self, host_id: int, forward_port: int) -> None:
        """Test helper: change a host's port behind UAM's back."""
        self.hosts[host_id] = replace(self.hosts[host_id], forward_port=forward_port)


class FakeNotifier:
    """Records notifications in `sent` as (topic, title, message, priority).

    Topics in `muted` are dropped, like a disabled setting. `fail_test` makes `send_test` raise.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, int]] = []
        self.tests: list[int | None] = []
        self.muted: set[str] = set()
        self.fail_test: Exception | None = None

    async def notify(self, topic: NotifyTopic, title: str, message: str, priority: int = 0) -> None:
        if topic not in self.muted:
            self.sent.append((topic, title, message, priority))

    async def send_test(self, credential_id: int | None = None) -> None:
        if self.fail_test is not None:
            raise self.fail_test
        self.tests.append(credential_id)
