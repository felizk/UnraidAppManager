"""In-memory fakes for every Protocol in `uam.contracts`. Only the orchestrator edits these."""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.fakes.cipher import PlainCipher, XorCipher
from tests.fakes.factories import make_app, make_source
from tests.fakes.fake_docker import FakeDocker
from tests.fakes.fake_local import FakeCodec, FakeImporter, FakePortAllocator, FakeSpecResolver, FakeVault
from tests.fakes.fake_remote import FakeGitHub, FakeNotifier, FakeProxyManager, FakeRegistry
from tests.fakes.memory_repos import (
    InMemoryAppRepository,
    InMemoryEventLog,
    InMemoryHttpCache,
    InMemoryKeyValueStore,
    InMemorySettingsStore,
    InMemorySourceRepository,
)

__all__ = [
    "FakeBundle",
    "FakeCodec",
    "FakeDocker",
    "FakeGitHub",
    "FakeImporter",
    "FakeNotifier",
    "FakePortAllocator",
    "FakeProxyManager",
    "FakeRegistry",
    "FakeSpecResolver",
    "FakeVault",
    "InMemoryAppRepository",
    "InMemoryEventLog",
    "InMemoryHttpCache",
    "InMemoryKeyValueStore",
    "InMemorySettingsStore",
    "InMemorySourceRepository",
    "PlainCipher",
    "XorCipher",
    "make_app",
    "make_source",
]


@dataclass
class FakeBundle:
    """One of each fake, wired together the way production wiring (T14) does."""

    apps: InMemoryAppRepository = field(default_factory=InMemoryAppRepository)
    settings: InMemorySettingsStore = field(default_factory=InMemorySettingsStore)
    kv: InMemoryKeyValueStore = field(default_factory=InMemoryKeyValueStore)
    http_cache: InMemoryHttpCache = field(default_factory=InMemoryHttpCache)
    events: InMemoryEventLog = field(default_factory=InMemoryEventLog)
    github: FakeGitHub = field(default_factory=FakeGitHub)
    registry: FakeRegistry = field(default_factory=FakeRegistry)
    docker: FakeDocker = field(default_factory=FakeDocker)
    proxy: FakeProxyManager = field(default_factory=FakeProxyManager)
    vault: FakeVault = field(default_factory=FakeVault)
    codec: FakeCodec = field(default_factory=FakeCodec)
    ports: FakePortAllocator = field(default_factory=FakePortAllocator)
    importer: FakeImporter = field(default_factory=FakeImporter)
    notifier: FakeNotifier = field(default_factory=FakeNotifier)
    sources: InMemorySourceRepository = field(init=False)
    spec_resolver: FakeSpecResolver = field(init=False)

    def __post_init__(self) -> None:
        self.sources = InMemorySourceRepository(self.apps)
        self.spec_resolver = FakeSpecResolver(self.ports)
