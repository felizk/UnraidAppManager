import inspect
from typing import Protocol

import pytest

from tests.fakes import FakeBundle, PlainCipher
from uam import contracts
from uam.db import (
    SqlAppRepository,
    SqlEventLog,
    SqlHttpCache,
    SqlKeyValueStore,
    SqlSettingsStore,
    SqlSourceRepository,
)

PROTOCOLS = {
    name: obj
    for name, obj in vars(contracts).items()
    if inspect.isclass(obj) and issubclass(obj, Protocol) and obj.__module__ == contracts.__name__
}

FAKE_FOR = {
    "GitHubClient": lambda b: b.github,
    "RegistryClient": lambda b: b.registry,
    "DockerEngine": lambda b: b.docker,
    "PortAllocator": lambda b: b.ports,
    "ProxyManager": lambda b: b.proxy,
    "Vault": lambda b: b.vault,
    "FieldCipher": lambda b: PlainCipher(),
    "TemplateCodec": lambda b: b.codec,
    "SpecResolver": lambda b: b.spec_resolver,
    "AppRepository": lambda b: b.apps,
    "SourceRepository": lambda b: b.sources,
    "SettingsStore": lambda b: b.settings,
    "KeyValueStore": lambda b: b.kv,
    "HttpCache": lambda b: b.http_cache,
    "Importer": lambda b: b.importer,
    "EventLog": lambda b: b.events,
}


def test_every_protocol_has_a_fake():
    assert set(PROTOCOLS) == set(FAKE_FOR)


@pytest.mark.parametrize("name", sorted(FAKE_FOR))
def test_fake_satisfies_protocol(name):
    fake = FAKE_FOR[name](FakeBundle())
    assert isinstance(fake, PROTOCOLS[name])


@pytest.mark.parametrize("name", sorted(FAKE_FOR))
def test_fake_method_signatures_match(name):
    proto, fake = PROTOCOLS[name], FAKE_FOR[name](FakeBundle())
    for meth, proto_fn in inspect.getmembers(proto, inspect.isfunction):
        if meth.startswith("_"):
            continue
        fake_fn = getattr(fake, meth)
        assert (
            list(inspect.signature(fake_fn).parameters) == list(inspect.signature(proto_fn).parameters)[1:]
        ), meth
        assert inspect.iscoroutinefunction(fake_fn) == inspect.iscoroutinefunction(proto_fn), meth


async def test_sql_repositories_satisfy_protocols(tmp_db):
    sf = tmp_db.session_factory
    assert isinstance(SqlAppRepository(sf, PlainCipher()), contracts.AppRepository)
    assert isinstance(SqlSourceRepository(sf), contracts.SourceRepository)
    assert isinstance(SqlSettingsStore(sf), contracts.SettingsStore)
    assert isinstance(SqlKeyValueStore(sf), contracts.KeyValueStore)
    assert isinstance(SqlHttpCache(sf), contracts.HttpCache)
    assert isinstance(SqlEventLog(sf), contracts.EventLog)
