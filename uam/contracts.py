"""Protocols between UAM modules. See docs/contracts.md; only the orchestrator edits this file.

Methods named `list` are declared last in each class so that `list[...]` annotations
above them still refer to the builtin.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from uam.domain import (
    App,
    ContainerInfo,
    ContainerSpec,
    Credential,
    CredentialKind,
    DiscoveredTemplate,
    ImportCandidate,
    ImportOptions,
    ImportPlan,
    NewSource,
    PortAllocation,
    Protocol_,
    ProxyHost,
    ProxySettings,
    RegistryAuth,
    ResolvedApp,
    Source,
    UnraidTemplate,
)
from uam.settings import Settings


@runtime_checkable
class GitHubClient(Protocol):
    async def discover_templates(self, source: Source, token: str | None) -> list[DiscoveredTemplate]: ...

    async def get_template(self, source: Source, path: str, token: str | None) -> DiscoveredTemplate: ...

    async def latest_commit_for_path(
        self, source: Source, path: str, token: str | None
    ) -> tuple[str, datetime] | None:
        """Conditional (ETag) request. Returns None when unchanged since the last call (HTTP 304)."""
        ...

    async def rate_limit_remaining_fraction(self) -> float: ...

    async def validate_token(self, token: str) -> list[str]:
        """Return the token's scopes (empty for fine-grained tokens)."""
        ...


@runtime_checkable
class RegistryClient(Protocol):
    async def get_digest(self, image_ref: str, auth: RegistryAuth | None) -> str:
        """HEAD manifest → Docker-Content-Digest (manifest-list digest for multi-arch)."""
        ...


@runtime_checkable
class DockerEngine(Protocol):
    async def pull(self, image_ref: str, auth: RegistryAuth | None) -> str:
        """Pull the image and return its repo digest."""
        ...

    async def create(self, spec: ContainerSpec) -> str:
        """Create a container and return its id."""
        ...

    async def start(self, container_id: str) -> None: ...

    async def stop(self, container_id: str, timeout: int = 20) -> None: ...

    async def remove(self, container_id: str, force: bool = False) -> None: ...

    async def rename(self, container_id: str, new_name: str) -> None: ...

    async def inspect(self, name_or_id: str) -> ContainerInfo | None: ...

    async def list_containers(self, all: bool = True) -> list[ContainerInfo]: ...

    async def wait_healthy(self, container_id: str, timeout_s: int = 90, stable_s: int = 15) -> bool: ...

    async def logs(self, container_id: str, tail: int = 200) -> str: ...

    async def host_ip(self) -> str:
        """Best guess of the Unraid LAN IP (`Settings.host_ip` overrides it at the caller)."""
        ...


@runtime_checkable
class PortAllocator(Protocol):
    async def allocate(
        self, app_id: int, requests: Sequence[tuple[int, Protocol_, int | None]]
    ) -> list[PortAllocation]:
        """requests = (container_port, protocol, preferred_host_port|None).

        Stable: existing allocations (including pinned ones) are returned unchanged.
        """
        ...

    async def pin(
        self,
        app_id: int,
        bindings: Sequence[tuple[int, Protocol_, int]],
        ignore_container_id: str | None = None,
    ) -> list[PortAllocation]:
        """Record exact host ports (may be outside the range).

        Ports published by `ignore_container_id` don't count as in use. ConflictError if another
        app holds one of them.
        """
        ...

    async def release(self, app_id: int) -> None: ...

    async def in_use(self) -> set[tuple[int, Protocol_]]: ...

    async def allocations(self) -> list[PortAllocation]:
        """Every stored allocation, ordered by host port."""
        ...


@runtime_checkable
class ProxyManager(Protocol):
    async def ensure_proxy_host(
        self,
        domain: str,
        forward_host: str,
        forward_port: int,
        settings: ProxySettings,
        existing_id: int | None,
    ) -> int: ...

    async def delete_proxy_host(self, host_id: int) -> None:
        """Idempotent: a missing host is not an error."""
        ...

    async def find_by_domain(self, domain: str) -> ProxyHost | None: ...

    async def list_proxy_hosts(self) -> list[ProxyHost]: ...

    async def claim_proxy_host(self, host_id: int, app_name: str) -> None:
        """Mark an existing (foreign) host as UAM-managed by adding the marker to advanced_config."""
        ...

    async def list_certificates(self) -> list[tuple[int, str]]: ...

    async def list_access_lists(self) -> list[tuple[int, str]]: ...


@runtime_checkable
class Vault(Protocol):
    async def store(
        self, name: str, kind: CredentialKind, username: str, secret: str, registry_host: str = ""
    ) -> Credential: ...

    async def update_secret(self, credential_id: int, secret: str) -> None: ...

    async def update_username(self, credential_id: int, username: str) -> None: ...

    async def reveal(self, credential_id: int) -> str: ...

    async def get(self, credential_id: int) -> Credential: ...

    async def delete(self, credential_id: int) -> None: ...

    async def registry_auth_for(self, image_ref: str) -> RegistryAuth | None:
        """Match the image host to a REGISTRY credential. For ghcr.io, fall back to a GITHUB_PAT."""
        ...

    async def list(self) -> list[Credential]: ...


@runtime_checkable
class FieldCipher(Protocol):
    """Symmetric encryption for secret columns (e.g. app overrides). T02 provides the Fernet one."""

    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


@runtime_checkable
class TemplateCodec(Protocol):
    def parse(self, xml: str) -> UnraidTemplate: ...

    def render(self, template: UnraidTemplate) -> str: ...


@runtime_checkable
class SpecResolver(Protocol):
    async def resolve(self, app: App, template: UnraidTemplate) -> ResolvedApp: ...


@runtime_checkable
class AppRepository(Protocol):
    async def get(self, app_id: int) -> App: ...

    async def get_by_name(self, name: str) -> App | None: ...

    async def due_for_check(self, now: datetime) -> list[App]:
        """Installed apps (running, stopped or failed_update) whose next_check_at is unset or <= now."""
        ...

    async def add(self, app: App) -> App:
        """Insert; `app.id` is ignored and the stored app (with its new id) is returned."""
        ...

    async def save(self, app: App) -> None: ...

    async def delete(self, app_id: int) -> None: ...

    async def list(self) -> list[App]: ...


@runtime_checkable
class SourceRepository(Protocol):
    async def get(self, source_id: int) -> Source: ...

    async def add(self, source: Source) -> Source: ...

    async def update(self, source: Source) -> None:
        """Replace ref, template path and credential of an existing source (owner/repo stay)."""
        ...

    async def delete(self, source_id: int) -> None: ...

    async def list(self) -> list[Source]: ...


@runtime_checkable
class SettingsStore(Protocol):
    async def get(self) -> Settings: ...

    async def save(self, settings: Settings) -> None: ...


@runtime_checkable
class KeyValueStore(Protocol):
    """Small internal values that must never show up in Settings (e.g. the admin password hash)."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...


@runtime_checkable
class HttpCache(Protocol):
    """ETag cache for conditional HTTP requests (T03)."""

    async def get(self, key: str) -> tuple[str, str] | None:
        """Return (etag, body) or None."""
        ...

    async def put(self, key: str, etag: str, body: str) -> None: ...


@runtime_checkable
class Importer(Protocol):
    async def scan(self) -> list[ImportCandidate]: ...

    async def plan(
        self,
        container_name: str,
        source_id: int | None,
        new_source: NewSource | None,
        template_path: str | None,
        options: ImportOptions,
    ) -> ImportPlan: ...

    async def apply(self, plan: ImportPlan) -> App: ...


@runtime_checkable
class EventLog(Protocol):
    async def record(self, app_id: int | None, kind: str, message: str, level: str = "info") -> None: ...

    async def recent(self, app_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]: ...


NotifyTopic = Literal["update", "crash"]


@runtime_checkable
class Notifier(Protocol):
    """Push notifications (Pushover). Off until a credential is chosen in Settings."""

    async def notify(self, topic: NotifyTopic, title: str, message: str, priority: int = 0) -> None:
        """Best effort: send when configured and `topic` is enabled. Never raises."""
        ...

    async def send_test(self, credential_id: int | None = None) -> None:
        """Send a test message with `credential_id` (default: the saved one). Raises UamError on failure."""
        ...
