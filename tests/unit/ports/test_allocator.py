import asyncio
from collections.abc import Awaitable, Callable

import pytest

from tests.fakes import FakeDocker, InMemorySettingsStore, PlainCipher, make_app, make_source
from uam.db import SqlAppRepository, SqlSourceRepository
from uam.domain import ContainerInfo, PortBinding, Protocol_
from uam.errors import ConflictError, PortRangeExhausted
from uam.ports.allocator import DbPortAllocator
from uam.settings import Settings


class StubProbe:
    def __init__(self, ports: set[tuple[int, Protocol_]]) -> None:
        self.ports = ports

    async def listening(self) -> set[tuple[int, Protocol_]]:
        return self.ports


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def settings() -> InMemorySettingsStore:
    return InMemorySettingsStore(Settings(port_range_start=30000, port_range_end=30009))


@pytest.fixture
async def apps(tmp_db) -> Callable[[str], Awaitable[int]]:
    sources = SqlSourceRepository(tmp_db.session_factory)
    repo = SqlAppRepository(tmp_db.session_factory, PlainCipher())
    source = await sources.add(make_source())

    async def new(name: str) -> int:
        return (await repo.add(make_app(name=name, source_id=source.id))).id

    return new


def make(tmp_db, settings, docker, probe=None) -> DbPortAllocator:
    return DbPortAllocator(tmp_db.session_factory, settings, docker, probe)


def foreign(docker: FakeDocker, *bindings: PortBinding) -> None:
    docker.seed_container(ContainerInfo("f1", "foreign", "img", None, "running", None, {}, bindings))


async def test_stable_and_lowest(tmp_db, settings, docker, apps) -> None:
    a = make(tmp_db, settings, docker)
    app = await apps("a")
    first = await a.allocate(app, [(80, "tcp", None), (81, "tcp", None)])
    assert [x.host_port for x in first] == [30000, 30001]
    again = await a.allocate(app, [(81, "tcp", 30005), (80, "tcp", None)])
    assert [x.host_port for x in again] == [30001, 30000]


async def test_preferred_honoured_or_skipped(tmp_db, settings, docker, apps) -> None:
    a = make(tmp_db, settings, docker)
    one, two, three = await apps("a"), await apps("b"), await apps("c")
    assert (await a.allocate(one, [(80, "tcp", 30005)]))[0].host_port == 30005
    assert (await a.allocate(two, [(80, "tcp", 30005)]))[0].host_port == 30000
    assert (await a.allocate(three, [(80, "tcp", 9999)]))[0].host_port == 30001


async def test_foreign_container_collision(tmp_db, settings, docker, apps) -> None:
    foreign(docker, PortBinding(80, "tcp", 30000), PortBinding(53, "udp", 30001))
    a = make(tmp_db, settings, docker)
    res = await a.allocate(await apps("a"), [(80, "tcp", 30000), (90, "tcp", None), (91, "udp", None)])
    assert [x.host_port for x in res] == [30001, 30002, 30000]


async def test_host_listener_and_reserved(tmp_db, settings, docker, apps) -> None:
    s = await settings.get()
    s.reserved_ports = [30001]
    await settings.save(s)
    a = make(tmp_db, settings, docker, StubProbe({(30000, "tcp")}))
    res = await a.allocate(await apps("a"), [(80, "tcp", None)])
    assert res[0].host_port == 30002
    assert (30001, "udp") in await a.in_use()


async def test_tcp_udp_pairing(tmp_db, settings, docker, apps) -> None:
    foreign(docker, PortBinding(1, "udp", 30000), PortBinding(1, "tcp", 30001))
    a = make(tmp_db, settings, docker)
    res = await a.allocate(await apps("a"), [(53, "tcp", None), (53, "udp", None)])
    assert {(x.protocol, x.host_port) for x in res} == {("tcp", 30002), ("udp", 30002)}


async def test_exhaustion(tmp_db, settings, docker, apps) -> None:
    a = make(tmp_db, settings, docker)
    with pytest.raises(PortRangeExhausted):
        await a.allocate(await apps("a"), [(i, "tcp", None) for i in range(11)])
    assert await a.allocate(await apps("b"), [(1, "tcp", None)])


async def test_stale_cleanup_and_release(tmp_db, settings, docker, apps) -> None:
    a = make(tmp_db, settings, docker)
    app = await apps("a")
    await a.allocate(app, [(80, "tcp", None), (81, "tcp", None)])
    await a.allocate(app, [(81, "tcp", None)])
    assert {p for p, _ in await a.in_use()} == {30001}
    await a.release(app)
    assert await a.in_use() == set()


async def test_pin_outside_range_and_stable(tmp_db, settings, docker, apps) -> None:
    foreign(docker, PortBinding(80, "tcp", 8080))
    a = make(tmp_db, settings, docker)
    app = await apps("a")
    pinned = await a.pin(app, [(80, "tcp", 8080)], ignore_container_id="f1")
    assert pinned[0].host_port == 8080
    res = await a.allocate(app, [(80, "tcp", None), (81, "tcp", None)])
    assert [x.host_port for x in res] == [8080, 30000]


async def test_pin_conflicts(tmp_db, settings, docker, apps) -> None:
    foreign(docker, PortBinding(80, "tcp", 8080))
    a = make(tmp_db, settings, docker)
    one, two = await apps("a"), await apps("b")
    with pytest.raises(ConflictError):
        await a.pin(one, [(80, "tcp", 8080)])
    await a.pin(one, [(80, "tcp", 9090)])
    with pytest.raises(ConflictError):
        await a.pin(two, [(80, "tcp", 9090)])
    assert (await a.pin(two, [(80, "udp", 9090)]))[0].protocol == "udp"


async def test_concurrent_allocations(tmp_db, settings, docker, apps) -> None:
    s = await settings.get()
    s.port_range_end = 30099
    await settings.save(s)
    a = make(tmp_db, settings, docker)
    ids = [await apps(f"app{i}") for i in range(20)]
    results = await asyncio.gather(*(a.allocate(i, [(80, "tcp", None), (53, "udp", None)]) for i in ids))
    ports = [(x.host_port, x.protocol) for r in results for x in r]
    assert len(ports) == len(set(ports)) == 40


async def test_allocations_lists_everything_by_host_port(tmp_db, settings, docker, apps) -> None:
    a = make(tmp_db, settings, docker)
    first, second = await apps("a"), await apps("b")
    await a.allocate(first, [(80, "tcp", None)])
    await a.pin(second, [(53, "udp", 53)])
    listed = await a.allocations()
    assert [(x.app_id, x.host_port, x.protocol) for x in listed] == [
        (second, 53, "udp"),
        (first, 30000, "tcp"),
    ]
