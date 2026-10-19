"""Shared builders for the API tests: service stubs and a ready-wired app + client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from tests.fakes import FakeBundle, make_app
from uam.domain import App, AppState, PortAllocation, ProxySettings, UpdateCheckResult
from uam.web.api import create_api_app
from uam.web.deps import ApiServices, ExportPlan

CSRF = {"X-Requested-With": "uam"}
PASSWORD = "correct horse battery"


class StubOrchestrator:
    """Records calls; `fail` maps a method name to an exception to raise."""

    def __init__(self, fakes: FakeBundle) -> None:
        self.fakes = fakes
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail: dict[str, BaseException] = {}

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    async def add_app(
        self,
        source_id: int,
        template_path: str,
        name: str | None,
        overrides: dict[str, str],
        proxy: ProxySettings,
        auto_ports: bool = True,
    ) -> App:
        self._record("add_app", source_id, template_path, name, overrides, proxy, auto_ports)
        return await self.fakes.apps.add(
            make_app(
                source_id=source_id,
                template_path=template_path,
                name=name or "new-app",
                overrides=overrides,
                proxy=proxy,
                auto_ports=auto_ports,
            )
        )

    async def install(self, app_id: int) -> App:
        self._record("install", app_id)
        app = await self.fakes.apps.get(app_id)
        app.state = AppState.RUNNING
        await self.fakes.apps.save(app)
        return app

    async def update(self, app_id: int, apply_template: bool, apply_image: bool) -> App:
        self._record("update", app_id, apply_template, apply_image)
        return await self.fakes.apps.get(app_id)

    async def reconfigure(
        self,
        app_id: int,
        overrides: dict[str, str] | None,
        proxy: ProxySettings | None,
        image_tag_override: str | None,
    ) -> App:
        self._record("reconfigure", app_id, overrides, proxy, image_tag_override)
        return await self.fakes.apps.get(app_id)

    async def uninstall(self, app_id: int, delete_appdata: bool = False) -> None:
        self._record("uninstall", app_id, delete_appdata)
        await self.fakes.apps.delete(app_id)

    async def _set_state(self, name: str, app_id: int, state: AppState) -> None:
        self._record(name, app_id)
        app = await self.fakes.apps.get(app_id)
        app.state = state
        await self.fakes.apps.save(app)

    async def start(self, app_id: int) -> None:
        await self._set_state("start", app_id, AppState.RUNNING)

    async def stop(self, app_id: int) -> None:
        await self._set_state("stop", app_id, AppState.STOPPED)

    async def restart(self, app_id: int) -> None:
        await self._set_state("restart", app_id, AppState.RUNNING)


class StubChecker:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def check_now(self, app_id: int) -> UpdateCheckResult:
        self.calls.append(app_id)
        return UpdateCheckResult(
            app_id, True, "abc123", False, None, None, datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        )


class StubExporter:
    def __init__(self) -> None:
        self.applied: list[tuple[list[ExportPlan], bool | None]] = []
        self.apply_error: BaseException | None = None

    async def plan(self, app_ids: list[int] | None) -> list[ExportPlan]:
        ids = app_ids if app_ids is not None else [1]
        return [
            ExportPlan(i, f"/unraid-templates/my-app{i}.xml", "<new/>", None, "+<new/>", True) for i in ids
        ]

    async def apply(self, plans: list[ExportPlan], include_secrets: bool | None = None) -> list[str]:
        if self.apply_error:
            raise self.apply_error
        self.applied.append((plans, include_secrets))
        return [p.path for p in plans if p.changed]


class PortView:
    """Adapts FakePortAllocator to the API's `PortReader`."""

    def __init__(self, fakes: FakeBundle) -> None:
        self.fakes = fakes

    async def allocations(self) -> list[PortAllocation]:
        return [PortAllocation(a, c, p, h) for (a, c, p), h in self.fakes.ports.allocs.items()]

    async def in_use(self) -> set[tuple[int, str]]:
        return set(await self.fakes.ports.in_use())


@dataclass
class ApiEnv:
    fakes: FakeBundle
    services: ApiServices
    orchestrator: StubOrchestrator
    checker: StubChecker
    exporter: StubExporter
    client: httpx.AsyncClient
    extra: dict[str, Any] = field(default_factory=dict)

    async def login(self) -> None:
        """Run first-time setup (which signs the client in)."""
        r = await self.client.post("/api/setup", json={"password": PASSWORD})
        assert r.status_code == 201, r.text


def build_env(api_token: str | None = None, with_proxy: bool = True) -> ApiEnv:
    """An API app over the fakes, plus an httpx client that sends the CSRF header by default."""
    fakes = FakeBundle()
    orchestrator = StubOrchestrator(fakes)
    checker = StubChecker()
    exporter = StubExporter()
    services = ApiServices(
        apps=fakes.apps,
        sources=fakes.sources,
        settings=fakes.settings,
        kv=fakes.kv,
        events=fakes.events,
        vault=fakes.vault,
        github=fakes.github,
        codec=fakes.codec,
        docker=fakes.docker,
        ports=PortView(fakes),
        importer=fakes.importer,
        orchestrator=orchestrator,
        checker=checker,
        exporter=exporter,
        proxy=fakes.proxy if with_proxy else None,
        api_token=api_token,
        notifier=fakes.notifier,
    )
    app = create_api_app(services)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=dict(CSRF)
    )
    return ApiEnv(fakes, services, orchestrator, checker, exporter, client)
