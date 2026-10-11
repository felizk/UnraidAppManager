"""Service Protocols the REST API depends on, and the FastAPI dependencies that hand them out.

The Orchestrator (T09), UpdateChecker (T10) and TemplateExporter (T11) are built separately; the
Protocols here copy the method names and signatures from their briefs. `ApiServices` bundles
everything the API needs; the app wiring (T14) builds one and stores it on `app.state.services`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import Depends, Request

from uam.contracts import (
    AppRepository,
    DockerEngine,
    EventLog,
    GitHubClient,
    Importer,
    KeyValueStore,
    ProxyManager,
    SettingsStore,
    SourceRepository,
    TemplateCodec,
    Vault,
)
from uam.domain import App, PortAllocation, ProxySettings, UpdateCheckResult

if TYPE_CHECKING:
    from uam.web.auth import AuthManager


@dataclass(frozen=True)
class ExportPlan:
    """Mirror of T11's `ExportPlan` (same fields)."""

    app_id: int
    path: str
    new_xml: str
    old_xml: str | None
    diff: str
    changed: bool


class OrchestratorService(Protocol):
    """The subset of T09's `Orchestrator` the API calls."""

    async def add_app(
        self,
        source_id: int,
        template_path: str,
        name: str | None,
        overrides: dict[str, str],
        proxy: ProxySettings,
        auto_ports: bool = True,
    ) -> App: ...

    async def install(self, app_id: int) -> App: ...

    async def update(self, app_id: int, apply_template: bool, apply_image: bool) -> App: ...

    async def reconfigure(
        self,
        app_id: int,
        overrides: dict[str, str] | None,
        proxy: ProxySettings | None,
        image_tag_override: str | None,
    ) -> App: ...

    async def uninstall(self, app_id: int, delete_appdata: bool = False) -> None: ...

    async def start(self, app_id: int) -> object: ...

    async def stop(self, app_id: int) -> object: ...

    async def restart(self, app_id: int) -> object: ...


class UpdateCheckerService(Protocol):
    """The subset of T10's `UpdateChecker` the API calls."""

    async def check_now(self, app_id: int) -> UpdateCheckResult: ...


class TemplateExporterService(Protocol):
    """T11's `TemplateExporter`."""

    async def plan(self, app_ids: list[int] | None) -> list[ExportPlan]: ...

    async def apply(self, plans: list[ExportPlan], include_secrets: bool | None = None) -> list[str]: ...


class PortReader(Protocol):
    """Read-only view of the port allocation table (`PortAllocator` has no way to list them)."""

    async def allocations(self) -> list[PortAllocation]: ...

    async def in_use(self) -> set[tuple[int, str]]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ApiServices:
    """Everything the REST API needs, injected by the app wiring or by tests."""

    apps: AppRepository
    sources: SourceRepository
    settings: SettingsStore
    kv: KeyValueStore
    events: EventLog
    vault: Vault
    github: GitHubClient
    codec: TemplateCodec
    docker: DockerEngine
    ports: PortReader
    importer: Importer
    orchestrator: OrchestratorService
    checker: UpdateCheckerService
    exporter: TemplateExporterService
    proxy: ProxyManager | None = None
    api_token: str | None = None
    clock: Callable[[], datetime] = field(default=_utcnow)
    auth: AuthManager | None = None


def get_services(request: Request) -> ApiServices:
    """The `ApiServices` stored on the app by the wiring."""
    services: ApiServices = request.app.state.services
    return services


def get_auth(services: Annotated[ApiServices, Depends(get_services)]) -> AuthManager:
    """The (lazily created) `AuthManager`."""
    from uam.web.auth import AuthManager

    if services.auth is None:
        services.auth = AuthManager(services.kv, services.api_token)
    return services.auth


def get_orchestrator(services: Annotated[ApiServices, Depends(get_services)]) -> OrchestratorService:
    """The deployment orchestrator."""
    return services.orchestrator


def get_checker(services: Annotated[ApiServices, Depends(get_services)]) -> UpdateCheckerService:
    """The update checker."""
    return services.checker


def get_exporter(services: Annotated[ApiServices, Depends(get_services)]) -> TemplateExporterService:
    """The template exporter."""
    return services.exporter
