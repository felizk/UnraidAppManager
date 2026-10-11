from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fakes import XorCipher, make_app, make_source
from uam.db import (
    SqlAppRepository,
    SqlEventLog,
    SqlHttpCache,
    SqlKeyValueStore,
    SqlSettingsStore,
    SqlSourceRepository,
)
from uam.db.models import AppRow, PortAllocationRow
from uam.domain import AppState, ProxySettings
from uam.errors import ConflictError, NotFound
from uam.settings import Settings

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def repos(tmp_db):
    sources = SqlSourceRepository(tmp_db.session_factory)
    apps = SqlAppRepository(tmp_db.session_factory, XorCipher())
    source = await sources.add(make_source())
    return sources, apps, source


async def test_app_round_trip(repos):
    _, apps, source = repos
    app = make_app(
        source_id=source.id,
        overrides={"Variable:CLOUD_PASSWORD": "hunter2"},
        proxy=ProxySettings(subdomain="notes", certificate_id=3),
        state=AppState.RUNNING,
        last_change_at=NOW,
        labels_pending=True,
    )
    stored = await apps.add(app)
    assert stored.id > 0
    assert await apps.get(stored.id) == replace(app, id=stored.id)
    assert await apps.get_by_name("notes") == stored
    assert await apps.get_by_name("nope") is None


async def test_overrides_are_encrypted_at_rest(repos, tmp_db):
    _, apps, source = repos
    await apps.add(make_app(source_id=source.id, overrides={"Variable:TOKEN": "hunter2"}))
    async with tmp_db.session_factory() as s:
        row = (await s.execute(select(AppRow))).scalar_one()
    assert b"hunter2" not in row.overrides_ciphertext


async def test_save_and_delete(repos, tmp_db):
    _, apps, source = repos
    app = await apps.add(make_app(source_id=source.id))
    app.state = AppState.FAILED
    app.last_error = "boom"
    await apps.save(app)
    assert (await apps.get(app.id)).last_error == "boom"
    async with tmp_db.session_factory() as s:
        s.add(PortAllocationRow(app_id=app.id, container_port=80, protocol="tcp", host_port=30000))
        await s.commit()
    await apps.delete(app.id)
    with pytest.raises(NotFound):
        await apps.get(app.id)
    async with tmp_db.session_factory() as s:
        assert (await s.execute(select(PortAllocationRow))).first() is None  # cascaded


async def test_duplicate_name_conflicts(repos):
    _, apps, source = repos
    await apps.add(make_app(source_id=source.id))
    with pytest.raises(ConflictError):
        await apps.add(make_app(source_id=source.id))


async def test_save_missing_app(repos):
    _, apps, source = repos
    with pytest.raises(NotFound):
        await apps.save(make_app(id=999, source_id=source.id))


async def test_due_for_check(repos):
    _, apps, source = repos

    async def add(name, state, next_check):
        return await apps.add(make_app(source_id=source.id, name=name, state=state, next_check_at=next_check))

    never = await add("never", AppState.RUNNING, None)
    past = await add("past", AppState.STOPPED, NOW - timedelta(minutes=1))
    exact = await add("exact", AppState.FAILED_UPDATE, NOW)
    await add("future", AppState.RUNNING, NOW + timedelta(minutes=1))
    await add("not-installed", AppState.NOT_INSTALLED, None)
    await add("updating", AppState.UPDATING, None)
    due = await apps.due_for_check(NOW)
    assert [a.name for a in due] == [never.name, past.name, exact.name]


async def test_sources(repos):
    sources, apps, source = repos
    assert await sources.get(source.id) == source
    other = await sources.add(make_source(repo="dnsbox", template_path="unraid/dns.xml"))
    assert [s.repo for s in await sources.list()] == ["dnsbox", "notes"]
    await apps.add(make_app(source_id=source.id))
    with pytest.raises(ConflictError):
        await sources.delete(source.id)
    await sources.delete(other.id)
    with pytest.raises(NotFound):
        await sources.get(other.id)


async def test_settings_store(tmp_db):
    store = SqlSettingsStore(tmp_db.session_factory)
    assert await store.get() == Settings()
    await store.save(Settings(base_domain="example.com"))
    await store.save(Settings(base_domain="example.org", reserved_ports=[30001]))
    assert await store.get() == Settings(base_domain="example.org", reserved_ports=[30001])


async def test_kv_and_http_cache(tmp_db):
    kv = SqlKeyValueStore(tmp_db.session_factory)
    assert await kv.get("admin_password_hash") is None
    await kv.set("admin_password_hash", "a")
    await kv.set("admin_password_hash", "b")
    assert await kv.get("admin_password_hash") == "b"
    await kv.delete("admin_password_hash")
    assert await kv.get("admin_password_hash") is None

    cache = SqlHttpCache(tmp_db.session_factory)
    assert await cache.get("k") is None
    await cache.put("k", 'W/"1"', "{}")
    await cache.put("k", 'W/"2"', "[]")
    assert await cache.get("k") == ('W/"2"', "[]")


async def test_event_log(tmp_db):
    log = SqlEventLog(tmp_db.session_factory)
    await log.record(1, "install", "started")
    await log.record(2, "install", "started")
    await log.record(1, "install", "done", level="warning")
    recent = await log.recent(app_id=1)
    assert [e["message"] for e in recent] == ["done", "started"]
    assert recent[0]["level"] == "warning" and recent[0]["ts"].tzinfo is not None
    assert len(await log.recent(limit=2)) == 2
