"""Application wiring: build every component, mount the web layer, run the scheduler."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from fastapi import FastAPI

from uam.web.api import create_api_router
from uam.web.deps import ApiServices

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AppConfig:
    """Process-level configuration, read from the environment."""

    data_dir: Path = Path("/data")
    db_url: str | None = None
    fakes: bool = False
    api_token: str | None = None
    proc_root: str = "/host/proc"
    start_scheduler: bool = True
    github_api_url: str = "https://api.github.com"

    @property
    def database_url(self) -> str:
        """The SQLAlchemy URL (defaults to `<data_dir>/uam.sqlite3`)."""
        return self.db_url or f"sqlite+aiosqlite:///{self.data_dir / 'uam.sqlite3'}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        """Read the `UAM_*` environment variables (see README, "Environment")."""
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("UAM_DATA_DIR", "/data")),
            db_url=env.get("UAM_DB_URL") or None,
            fakes=_flag(env.get("UAM_FAKES")),
            api_token=env.get("UAM_API_TOKEN") or None,
            proc_root=env.get("UAM_PROC_ROOT", "/host/proc"),
            start_scheduler=env.get("UAM_SCHEDULER", "1") != "0",
            github_api_url=env.get("UAM_GITHUB_API_URL") or "https://api.github.com",
        )


class Scheduler(Protocol):
    """What the lifespan needs from the update scheduler."""

    def start(self) -> None: ...

    def shutdown(self) -> None: ...


@dataclass
class Runtime:
    """Everything built for one process: the API services plus what must be started and closed."""

    services: ApiServices
    scheduler: Scheduler | None = None
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def close(self) -> None:
        """Release HTTP clients and the database."""
        for close in reversed(self.closers):
            try:
                await close()
            except Exception:  # shutdown must not fail half-way
                log.exception("error while shutting down")


async def build_runtime(config: AppConfig) -> Runtime:
    """Build the production components (or the in-memory fakes when `config.fakes`)."""
    if config.fakes:
        return await _build_fake_runtime(config)

    from uam.clients.docker_engine import SdkDockerEngine
    from uam.clients.github import HttpGitHubClient
    from uam.clients.pushover import PushoverNotifier
    from uam.clients.registry import HttpRegistryClient
    from uam.db import (
        SqlAppRepository,
        SqlEventLog,
        SqlHttpCache,
        SqlKeyValueStore,
        SqlSettingsStore,
        SqlSourceRepository,
        init,
    )
    from uam.ports.allocator import DbPortAllocator
    from uam.ports.probe import HostListenerProbe
    from uam.proxy_switch import SettingsBackedProxyManager
    from uam.unraid_template import XmlTemplateCodec
    from uam.vault import FernetFieldCipher, FernetVault, load_fernet

    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = await init(config.database_url)
    fernet = load_fernet(config.data_dir)
    sf = db.session_factory
    apps = SqlAppRepository(sf, FernetFieldCipher(fernet))
    sources = SqlSourceRepository(sf)
    settings = SqlSettingsStore(sf)
    kv = SqlKeyValueStore(sf)
    events = SqlEventLog(sf)
    vault = FernetVault(sf, fernet)
    http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    github = HttpGitHubClient(SqlHttpCache(sf), base_url=config.github_api_url, client=http)
    registry = HttpRegistryClient(httpx.AsyncClient(timeout=30.0, follow_redirects=True))
    docker = SdkDockerEngine(proc_root=config.proc_root)
    proc_net = Path(config.proc_root) / "net"
    probe = HostListenerProbe(str(proc_net)) if proc_net.is_dir() else None
    ports = DbPortAllocator(sf, settings, docker, probe)
    proxy = SettingsBackedProxyManager(settings, vault, http)

    runtime = _assemble(
        config,
        apps=apps,
        sources=sources,
        settings=settings,
        kv=kv,
        events=events,
        vault=vault,
        github=github,
        registry=registry,
        docker=docker,
        ports=ports,
        proxy=proxy,
        codec=XmlTemplateCodec(),
        notifier=PushoverNotifier(settings, vault, http),
    )
    runtime.closers += [db.dispose, http.aclose, registry.aclose]
    return runtime


async def _build_fake_runtime(config: AppConfig) -> Runtime:
    """In-memory stand-ins for local UI work (`make dev`); needs the repo's `tests` package."""
    from tests.fakes import FakeBundle

    f = FakeBundle()
    log.warning("running with in-memory fakes (UAM_FAKES=1); nothing is persisted or deployed")
    return _assemble(
        config,
        apps=f.apps,
        sources=f.sources,
        settings=f.settings,
        kv=f.kv,
        events=f.events,
        vault=f.vault,
        github=f.github,
        registry=f.registry,
        docker=f.docker,
        ports=f.ports,
        proxy=f.proxy,
        codec=f.codec,
        notifier=f.notifier,
    )


def _assemble(config: AppConfig, **c: object) -> Runtime:
    """Wire the services on top of the leaf components (shared by real and fake runtimes)."""
    from uam.services.importer import DefaultImporter
    from uam.services.locks import AppLocks
    from uam.services.orchestrator import Orchestrator
    from uam.services.scheduler import UpdateScheduler
    from uam.services.spec_resolver import DefaultSpecResolver
    from uam.services.template_export import TemplateExporter
    from uam.services.update_checker import UpdateChecker

    k = cast(dict[str, Any], c)
    clock = _utcnow
    resolver = DefaultSpecResolver(k["ports"], k["settings"])
    orchestrator = Orchestrator(
        k["apps"],
        k["sources"],
        k["github"],
        k["codec"],
        resolver,
        k["docker"],
        k["proxy"],
        k["ports"],
        k["vault"],
        k["settings"],
        k["events"],
        clock,
        AppLocks(),
        notifier=k["notifier"],
    )
    checker = UpdateChecker(
        k["apps"],
        k["sources"],
        k["github"],
        k["registry"],
        k["vault"],
        k["codec"],
        k["settings"],
        orchestrator,
        k["events"],
        clock,
    )
    backups = config.data_dir / "template-backups"
    exporter = TemplateExporter(
        k["apps"],
        k["sources"],
        k["github"],
        k["codec"],
        resolver,
        k["vault"],
        k["settings"],
        backup_dir=backups,
        clock=clock,
    )
    importer = DefaultImporter(
        k["docker"],
        k["codec"],
        k["github"],
        k["sources"],
        k["apps"],
        k["ports"],
        k["proxy"],
        k["vault"],
        k["settings"],
        k["events"],
        orchestrator,
        archive_dir=backups / "imported",
        clock=clock,
    )
    services = ApiServices(
        apps=k["apps"],
        sources=k["sources"],
        settings=k["settings"],
        kv=k["kv"],
        events=k["events"],
        vault=k["vault"],
        github=k["github"],
        codec=k["codec"],
        docker=k["docker"],
        ports=k["ports"],
        importer=importer,
        orchestrator=orchestrator,
        checker=checker,
        exporter=exporter,
        proxy=k["proxy"],
        api_token=config.api_token,
        clock=clock,
        notifier=k["notifier"],
    )
    scheduler = UpdateScheduler(checker, k["apps"], orchestrator, clock) if config.start_scheduler else None
    return Runtime(services=services, scheduler=scheduler)


def _mount_ui(app: FastAPI) -> None:
    """Mount the HTML UI and its static files."""
    from uam.web.ui import create_ui_router, mount_static

    app.include_router(create_ui_router())
    mount_static(app)


def create_app(
    config: AppConfig | None = None,
    runtime_factory: Callable[[AppConfig], Awaitable[Runtime]] = build_runtime,
) -> FastAPI:
    """The FastAPI application. Components are built in the lifespan, so import has no side effects."""
    config = config or AppConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = await runtime_factory(config)
        app.state.services = runtime.services
        app.state.runtime = runtime
        if runtime.scheduler is not None:
            runtime.scheduler.start()
        try:
            yield
        finally:
            if runtime.scheduler is not None:
                runtime.scheduler.shutdown()
            await runtime.close()

    app = FastAPI(title="Unraid App Manager", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.include_router(create_api_router())
    _mount_ui(app)
    return app
