"""Orchestrator tests, built entirely on the fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FrozenClock, load_template
from tests.fakes import FakeBundle, make_app, make_source
from uam.domain import (
    LABEL_APP_ID,
    LABEL_MANAGED,
    AppState,
    ContainerInfo,
    CredentialKind,
    PortBinding,
    ProxySettings,
)
from uam.errors import ConflictError, DeployError, NotFound
from uam.services.orchestrator import Orchestrator

IMAGE = "ghcr.io/felizk/notes:latest"
OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64


class Env:
    def __init__(self, fakes: FakeBundle, clock: FrozenClock) -> None:
        self.f = fakes
        self.orch = Orchestrator(
            fakes.apps,
            fakes.sources,
            fakes.github,
            fakes.codec,
            fakes.spec_resolver,
            fakes.docker,
            fakes.proxy,
            fakes.ports,
            fakes.vault,
            fakes.settings,
            fakes.events,
            clock,
        )

    async def new_app(self, **kw: Any) -> int:
        app = await self.f.apps.add(make_app(source_id=1, **kw))
        return app.id

    async def installed(self, **kw: Any) -> int:
        app_id = await self.new_app(**kw)
        await self.orch.install(app_id)
        return app_id

    async def configure_npm(self) -> None:
        s = await self.f.settings.get()
        s.npm_url, s.base_domain, s.host_ip = "http://npm:81", "example.com", "192.168.1.10"
        await self.f.settings.save(s)

    def names(self) -> list[str]:
        return sorted(c.name for c in self.f.docker.containers.values())


@pytest.fixture
async def env(fakes: FakeBundle, frozen_now: FrozenClock) -> Env:
    await fakes.sources.add(make_source())
    xml = load_template("simple-web.xml")
    head = fakes.github.put_file("felizk", "notes", "unraid/notes.xml", xml)
    # The same template is also reachable by commit sha (installed-commit fetches use ref=<sha>).
    for ref in (head.commit_sha, "abc"):
        fakes.github.put_file("felizk", "notes", "unraid/notes.xml", xml, ref=ref, commit_sha=ref)
    fakes.docker.set_image(IMAGE, OLD)
    return Env(fakes, frozen_now)


# ------------------------------------------------------------------ add / install


async def test_add_app_defaults_name_from_template(env: Env) -> None:
    app = await env.orch.add_app(1, "unraid/notes.xml", None, {}, ProxySettings())
    assert (app.name, app.state) == ("notes", AppState.NOT_INSTALLED)
    assert env.f.docker.containers == {}


async def test_add_app_uses_vault_token_for_private_source(env: Env) -> None:
    cred = await env.f.vault.store("pat", CredentialKind.GITHUB_PAT, "me", "tok")
    await env.f.sources.add(make_source(repo="secret", credential_id=cred.id))
    env.f.github.put_file("felizk", "secret", "unraid/s.xml", load_template("simple-web.xml"))
    env.f.github.make_private("felizk", "secret", "tok")
    app = await env.orch.add_app(2, "unraid/s.xml", "secret-app", {}, ProxySettings())
    assert app.name == "secret-app"
    with pytest.raises(NotFound):
        await env.orch.add_app(1, "missing.xml", None, {}, ProxySettings())


async def test_install_success(env: Env, frozen_now: FrozenClock) -> None:
    app_id = await env.new_app()
    app = await env.orch.install(app_id)
    stored = await env.f.apps.get(app_id)
    assert app.state == stored.state == AppState.RUNNING
    assert stored.installed_image_digest == OLD
    assert stored.installed_template_commit
    assert stored.last_change_at == frozen_now()
    (container,) = env.f.docker.containers.values()
    assert container.id == stored.container_id
    assert container.state == "running" and container.health == "healthy"
    assert container.labels[LABEL_MANAGED] == "true" and container.labels[LABEL_APP_ID] == str(app_id)
    assert env.f.ports.for_app(app_id) == {(3000, "tcp"): 30000}
    kinds = env.f.events.kinds(app_id)
    for k in ("install.start", "install.pull", "install.create", "install.health", "install.done"):
        assert k in kinds
    assert env.f.proxy.hosts == {}


async def test_install_rejects_installed_app(env: Env) -> None:
    app_id = await env.installed()
    with pytest.raises(ConflictError):
        await env.orch.install(app_id)


@pytest.mark.parametrize("op", ["pull", "create", "start", "health"])
async def test_install_failure_cleans_up(env: Env, op: str) -> None:
    await env.configure_npm()
    app_id = await env.new_app(proxy=ProxySettings(subdomain="notes"))
    env.f.docker.fail_next(op)
    with pytest.raises(DeployError) as err:
        await env.orch.install(app_id)
    assert err.value.stage == {"pull": "pull", "create": "create", "start": "start", "health": "health"}[op]
    stored = await env.f.apps.get(app_id)
    assert stored.state == AppState.FAILED and stored.last_error
    assert stored.container_id is None
    assert env.f.docker.containers == {}
    assert env.f.ports.for_app(app_id) == {}
    assert env.f.proxy.hosts == {}
    assert "install.failed" in env.f.events.kinds(app_id)
    app = await env.orch.install(app_id)
    assert app.state == AppState.RUNNING and len(env.f.docker.containers) == 1


async def test_install_failure_when_template_missing(env: Env) -> None:
    app_id = await env.new_app(template_path="nope.xml")
    with pytest.raises(DeployError) as err:
        await env.orch.install(app_id)
    assert err.value.stage == "template"
    assert (await env.f.apps.get(app_id)).state == AppState.FAILED


async def test_failed_reinstall_keeps_ports_when_not_first_install(env: Env) -> None:
    app_id = await env.new_app(state=AppState.FAILED, installed_template_commit="abc")
    await env.f.ports.pin(app_id, [(3000, "tcp", 31234)])
    env.f.docker.fail_next("start")
    with pytest.raises(DeployError):
        await env.orch.install(app_id)
    assert env.f.ports.for_app(app_id) == {(3000, "tcp"): 31234}


async def test_install_registers_npm_host_mode(env: Env) -> None:
    await env.configure_npm()
    app_id = await env.installed(proxy=ProxySettings(subdomain="notes"))
    (host,) = env.f.proxy.hosts.values()
    assert host.domain_names == ("notes.example.com",)
    assert (host.forward_host, host.forward_port) == ("192.168.1.10", 30000)
    assert (await env.f.apps.get(app_id)).needs_attention is None


async def test_install_registers_npm_container_network(env: Env) -> None:
    await env.configure_npm()
    await env.installed(proxy=ProxySettings(subdomain="notes", forward_mode="container_network"))
    (host,) = env.f.proxy.hosts.values()
    assert (host.forward_host, host.forward_port) == ("notes", 3000)


async def test_install_skips_npm_without_url_or_subdomain(env: Env) -> None:
    await env.installed(proxy=ProxySettings(subdomain="notes"))
    assert env.f.proxy.calls == []
    await env.configure_npm()
    await env.installed(name="other")
    assert env.f.proxy.calls == []


async def test_npm_failure_does_not_fail_install(env: Env) -> None:
    await env.configure_npm()
    env.f.proxy.seed_foreign_host("notes.example.com", "10.0.0.1", 80)
    app_id = await env.installed(proxy=ProxySettings(subdomain="notes"))
    stored = await env.f.apps.get(app_id)
    assert stored.state == AppState.RUNNING
    assert stored.needs_attention and "NPM" in stored.needs_attention
    assert "npm.failed" in env.f.events.kinds(app_id)
    assert [e["level"] for e in env.f.events.events if e["kind"] == "npm.failed"] == ["warning"]


# ------------------------------------------------------------------ locks


async def test_concurrent_operations_conflict(env: Env) -> None:
    app_id = await env.new_app()
    gate = asyncio.Event()
    real_pull = env.f.docker.pull

    async def slow_pull(ref: str, auth: Any) -> str:
        await gate.wait()
        return await real_pull(ref, auth)

    env.f.docker.pull = slow_pull  # type: ignore[method-assign,assignment]
    first = asyncio.create_task(env.orch.install(app_id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert env.orch.locks.locked(app_id)
    for call in (env.orch.start(app_id), env.orch.uninstall(app_id), env.orch.update(app_id, True, True)):
        with pytest.raises(ConflictError, match="operation in progress"):
            await call
    gate.set()
    assert (await first).state == AppState.RUNNING
    assert not env.orch.locks.locked(app_id)
    await env.orch.stop(app_id)


async def test_lock_released_after_failure(env: Env) -> None:
    app_id = await env.new_app()
    env.f.docker.fail_next("pull")
    with pytest.raises(DeployError):
        await env.orch.install(app_id)
    assert not env.orch.locks.locked(app_id)


# ------------------------------------------------------------------ update


async def test_update_success(env: Env, frozen_now: FrozenClock) -> None:
    app_id = await env.installed()
    old_id = (await env.f.apps.get(app_id)).container_id
    stored = await env.f.apps.get(app_id)
    stored.available_image_digest = NEW
    await env.f.apps.save(stored)
    env.f.docker.set_image(IMAGE, NEW)
    frozen_now.advance(__import__("datetime").timedelta(hours=1))
    app = await env.orch.update(app_id, apply_template=True, apply_image=True)
    assert app.state == AppState.RUNNING
    assert app.installed_image_digest == NEW and app.available_image_digest is None
    assert app.container_id != old_id
    assert env.names() == ["notes"]
    assert env.f.docker.containers[app.container_id or ""].state == "running"
    assert app.last_change_at == frozen_now()
    assert env.f.ports.for_app(app_id) == {(3000, "tcp"): 30000}
    kinds = env.f.events.kinds(app_id)
    for k in ("update.pull", "update.rename_old", "update.create_new", "update.health", "update.cleanup_old"):
        assert k in kinds


async def test_update_pulls_before_touching_old_container(env: Env) -> None:
    app_id = await env.installed()
    env.f.docker.calls.clear()
    await env.orch.update(app_id, True, True)
    ops = [op for op, _ in env.f.docker.calls]
    assert ops.index("pull") < ops.index("rename") < ops.index("create")


@pytest.mark.parametrize(
    ("op", "stage"),
    [
        ("pull", "pull"),
        ("rename", "rename_old"),
        ("stop", "rename_old"),
        ("create", "create_new"),
        ("start", "start_new"),
        ("health", "health"),
        ("remove", "cleanup_old"),
    ],
)
async def test_update_failure_rolls_back(env: Env, op: str, stage: str) -> None:
    app_id = await env.installed()
    before = await env.f.apps.get(app_id)
    env.f.docker.set_image(IMAGE, NEW)
    env.f.docker.fail_next(op)
    with pytest.raises(DeployError) as err:
        await env.orch.update(app_id, True, True)
    assert err.value.stage == stage
    stored = await env.f.apps.get(app_id)
    assert stored.state == AppState.FAILED_UPDATE and stored.last_error
    assert stored.installed_image_digest == OLD
    assert stored.container_id == before.container_id
    (container,) = env.f.docker.containers.values()
    assert container.id == before.container_id
    assert container.name == "notes" and container.state == "running"
    assert env.f.ports.for_app(app_id) == {(3000, "tcp"): 30000}
    assert "update.failed" in env.f.events.kinds(app_id)
    assert not env.orch.locks.locked(app_id)


async def test_update_after_failed_update_recovers(env: Env) -> None:
    app_id = await env.installed()
    env.f.docker.set_image(IMAGE, NEW)
    env.f.docker.fail_next("health")
    with pytest.raises(DeployError):
        await env.orch.update(app_id, True, True)
    app = await env.orch.update(app_id, True, True)
    assert app.state == AppState.RUNNING and app.installed_image_digest == NEW
    assert env.names() == ["notes"]


async def test_update_without_image_reuses_digest(env: Env) -> None:
    app_id = await env.installed()
    env.f.docker.set_image(IMAGE, NEW)
    env.f.docker.pulls.clear()
    app = await env.orch.update(app_id, apply_template=True, apply_image=False)
    assert app.installed_image_digest == OLD
    assert env.f.docker.pulls == []


async def test_update_npm_failure_does_not_roll_back(env: Env) -> None:
    await env.configure_npm()
    app_id = await env.installed(proxy=ProxySettings(subdomain="notes"))
    for hid in list(env.f.proxy.owners):
        env.f.proxy.owners[hid] = None
    app = await env.orch.update(app_id, True, True)
    assert app.state == AppState.RUNNING and app.needs_attention
    assert env.names() == ["notes"]


async def test_update_requires_operable_state(env: Env) -> None:
    app_id = await env.new_app()
    with pytest.raises(ConflictError):
        await env.orch.update(app_id, True, True)


# ------------------------------------------------------------------ reconfigure / imported apps


def _foreign(env: Env, cid: str = "f" * 64) -> ContainerInfo:
    return env.f.docker.seed_container(
        ContainerInfo(
            id=cid,
            name="notes",
            image=IMAGE,
            image_digest=OLD,
            state="running",
            health=None,
            labels={"net.unraid.docker.managed": "dockerman"},
            published_ports=(PortBinding(3000, "tcp", 8080),),
        )
    )


async def _imported(env: Env) -> int:
    info = _foreign(env)
    app_id = await env.new_app(
        state=AppState.RUNNING,
        labels_pending=True,
        container_id=info.id,
        installed_image_digest=OLD,
        installed_template_commit="abc",
    )
    await env.f.ports.pin(app_id, [(3000, "tcp", 8080)])
    return app_id


async def test_reconfigure_labels_pending_app_reuses_digest(env: Env) -> None:
    app_id = await _imported(env)
    env.f.docker.set_image(IMAGE, NEW)
    app = await env.orch.reconfigure(app_id, None, None, None)
    assert not app.labels_pending and app.state == AppState.RUNNING
    assert app.installed_image_digest == OLD
    assert env.f.docker.pulls == []
    (container,) = env.f.docker.containers.values()
    assert container.id == app.container_id != "f" * 64
    assert container.labels[LABEL_MANAGED] == "true"
    assert container.image == f"ghcr.io/felizk/notes@{OLD}"
    assert container.image_digest == OLD
    assert container.name == "notes"
    assert env.f.ports.for_app(app_id) == {(3000, "tcp"): 8080}
    assert (await env.f.apps.get(app_id)).labels_pending is False


async def test_reconfigure_rolls_back_imported_app(env: Env) -> None:
    app_id = await _imported(env)
    env.f.docker.fail_next("create")
    with pytest.raises(DeployError) as err:
        await env.orch.reconfigure(app_id, {"Variable:TZ": "UTC"}, None, None)
    assert err.value.stage == "create_new"
    stored = await env.f.apps.get(app_id)
    assert stored.labels_pending is True and stored.state == AppState.FAILED_UPDATE
    assert stored.overrides == {}
    assert stored.container_id == "f" * 64
    (container,) = env.f.docker.containers.values()
    assert (container.id, container.name, container.state) == ("f" * 64, "notes", "running")
    assert LABEL_MANAGED not in container.labels


async def test_reconfigure_applies_overrides_and_proxy(env: Env) -> None:
    await env.configure_npm()
    app_id = await env.installed()
    app = await env.orch.reconfigure(app_id, {"Variable:TZ": "UTC"}, ProxySettings(subdomain="notes2"), None)
    assert app.overrides == {"Variable:TZ": "UTC"}
    assert env.f.docker.specs[app.container_id or ""].env["TZ"] == "UTC"
    assert app.installed_image_digest == OLD
    assert [h.domain_names for h in env.f.proxy.hosts.values()] == [("notes2.example.com",)]
    app = await env.orch.reconfigure(app_id, None, ProxySettings(subdomain="notes3"), None)
    assert [h.domain_names for h in env.f.proxy.hosts.values()] == [("notes3.example.com",)]


async def test_reconfigure_with_new_tag_pulls(env: Env) -> None:
    app_id = await env.installed()
    env.f.docker.set_image("ghcr.io/felizk/notes:beta", NEW)
    app = await env.orch.reconfigure(app_id, None, None, "beta")
    assert app.image_tag_override == "beta" and app.installed_image_digest == NEW


async def test_update_of_labels_pending_app_clears_flag(env: Env) -> None:
    app_id = await _imported(env)
    env.f.docker.set_image(IMAGE, NEW)
    app = await env.orch.update(app_id, True, True)
    assert not app.labels_pending and app.installed_image_digest == NEW
    assert env.names() == ["notes"]


async def test_reconfigure_pending_app_with_missing_container_fails(env: Env) -> None:
    app_id = await _imported(env)
    del env.f.docker.containers["f" * 64]
    with pytest.raises(DeployError):
        await env.orch.reconfigure(app_id, None, None, None)
    assert (await env.f.apps.get(app_id)).state == AppState.FAILED_UPDATE
    assert env.f.docker.containers == {}


# ------------------------------------------------------------------ uninstall


async def test_uninstall_removes_everything(env: Env) -> None:
    await env.configure_npm()
    app_id = await env.installed(proxy=ProxySettings(subdomain="notes"))
    assert env.f.proxy.hosts
    await env.orch.uninstall(app_id)
    assert env.f.docker.containers == {}
    assert env.f.proxy.hosts == {}
    assert env.f.ports.for_app(app_id) == {}
    assert app_id not in env.f.apps.apps


async def test_uninstall_ignores_missing_npm_host(env: Env) -> None:
    await env.configure_npm()
    app_id = await env.installed(proxy=ProxySettings(subdomain="notes"))
    env.f.proxy.hosts.clear()
    await env.orch.uninstall(app_id)
    assert app_id not in env.f.apps.apps


async def test_uninstall_failure_keeps_app(env: Env) -> None:
    app_id = await env.installed()
    env.f.docker.fail_next("remove")
    with pytest.raises(DeployError):
        await env.orch.uninstall(app_id)
    stored = await env.f.apps.get(app_id)
    assert stored.state == AppState.RUNNING and len(env.f.docker.containers) == 1


async def test_uninstall_deletes_appdata_only_under_root(env: Env, tmp_path: Path) -> None:
    root = tmp_path / "appdata"
    inside, outside = root / "notes", tmp_path / "elsewhere"
    for p in (inside, outside):
        p.mkdir(parents=True)
        (p / "f").write_text("x")
    s = await env.f.settings.get()
    s.appdata_root = str(root)
    await env.f.settings.save(s)
    app_id = await env.installed(overrides={"Path:/data": str(inside)})
    await env.orch.uninstall(app_id, delete_appdata=True)
    assert not inside.exists() and root.exists()

    other = await env.installed(name="other", overrides={"Path:/data": str(outside)})
    await env.orch.uninstall(other, delete_appdata=True)
    assert (outside / "f").exists()

    via_link = tmp_path / "link"
    via_link.symlink_to(outside)
    third = await env.installed(name="third", overrides={"Path:/data": str(root / ".." / "link")})
    await env.orch.uninstall(third, delete_appdata=True)
    assert (outside / "f").exists()

    at_root = await env.installed(name="rootapp", overrides={"Path:/data": str(root)})
    await env.orch.uninstall(at_root, delete_appdata=True)
    assert root.exists()

    at_slash = await env.installed(name="slashapp", overrides={"Path:/data": "/"})
    await env.orch.uninstall(at_slash, delete_appdata=True)
    assert await asyncio.to_thread(Path("/").exists)


async def test_uninstall_keeps_appdata_by_default(env: Env, tmp_path: Path) -> None:
    root = tmp_path / "appdata"
    (root / "notes").mkdir(parents=True)
    s = await env.f.settings.get()
    s.appdata_root = str(root)
    await env.f.settings.save(s)
    app_id = await env.installed(overrides={"Path:/data": str(root / "notes")})
    await env.orch.uninstall(app_id)
    assert (root / "notes").exists()


# ------------------------------------------------------------------ start / stop / sync


async def test_start_stop_restart(env: Env) -> None:
    app_id = await env.installed()
    cid = (await env.f.apps.get(app_id)).container_id or ""
    assert (await env.orch.stop(app_id)).state == AppState.STOPPED
    assert env.f.docker.containers[cid].state == "exited"
    assert (await env.orch.start(app_id)).state == AppState.RUNNING
    assert env.f.docker.containers[cid].state == "running"
    env.f.docker.calls.clear()
    await env.orch.restart(app_id)
    assert [op for op, _ in env.f.docker.calls] == ["stop", "start"]
    env.f.docker.fail_next("stop")
    with pytest.raises(DeployError):
        await env.orch.stop(app_id)


async def test_start_requires_installed_app(env: Env) -> None:
    app_id = await env.new_app()
    with pytest.raises(ConflictError):
        await env.orch.start(app_id)


async def test_sync_state_container_deleted_outside_uam(env: Env) -> None:
    app_id = await env.installed()
    env.f.docker.containers.clear()
    await env.orch.sync_state()
    stored = await env.f.apps.get(app_id)
    assert stored.state == AppState.FAILED and stored.container_id is None
    assert stored.last_error == "container missing"
    assert "sync.missing" in env.f.events.kinds(app_id)
    app = await env.orch.install(app_id)
    assert app.state == AppState.RUNNING


async def test_sync_state_follows_container_state(env: Env) -> None:
    app_id = await env.installed()
    cid = (await env.f.apps.get(app_id)).container_id or ""
    await env.f.docker.stop(cid)
    await env.orch.sync_state()
    assert (await env.f.apps.get(app_id)).state == AppState.STOPPED
    await env.f.docker.start(cid)
    await env.orch.sync_state()
    assert (await env.f.apps.get(app_id)).state == AppState.RUNNING


async def test_sync_state_matches_imported_app_by_name(env: Env) -> None:
    _foreign(env, "e" * 64)
    app_id = await env.new_app(state=AppState.RUNNING, labels_pending=True, container_id="d" * 64)
    await env.orch.sync_state()
    stored = await env.f.apps.get(app_id)
    assert stored.container_id == "e" * 64 and stored.state == AppState.RUNNING


async def test_sync_state_skips_busy_and_transitional_apps(env: Env) -> None:
    busy = await env.installed()
    installing = await env.new_app(name="x", state=AppState.INSTALLING)
    env.f.docker.containers.clear()
    async with env.orch.locks.hold(busy):
        await env.orch.sync_state()
    assert (await env.f.apps.get(busy)).state == AppState.RUNNING
    assert (await env.f.apps.get(installing)).state == AppState.INSTALLING


async def test_image_only_update_keeps_installed_template(env: Env) -> None:
    app_id = await env.installed()
    installed = (await env.f.apps.get(app_id)).installed_template_commit
    changed = load_template("simple-web.xml").replace("Europe/Copenhagen", "UTC")
    env.f.github.put_file("felizk", "notes", "unraid/notes.xml", changed)
    env.f.docker.set_image(IMAGE, "sha256:" + "f" * 64)
    await env.orch.update(app_id, apply_template=False, apply_image=True)
    app = await env.f.apps.get(app_id)
    assert app.installed_template_commit == installed
    assert env.f.docker.specs[app.container_id or ""].env["TZ"] == "Europe/Copenhagen"
    await env.orch.update(app_id, apply_template=True, apply_image=False)
    app = await env.f.apps.get(app_id)
    assert env.f.docker.specs[app.container_id or ""].env["TZ"] == "UTC"
    assert env.f.docker.specs[app.container_id or ""].labels["uam.source"] == "felizk/notes"


async def test_default_certificate_is_applied(env: Env) -> None:
    await env.configure_npm()
    s = await env.f.settings.get()
    s.default_certificate_id = 7
    await env.f.settings.save(s)
    app_id = await env.installed(proxy=ProxySettings(subdomain="notes"))
    [host] = env.f.proxy.hosts.values()
    assert env.f.proxy.settings[host.id].certificate_id == 7
    stored = await env.f.apps.get(app_id)
    assert stored.proxy.certificate_id is None  # the app keeps following the default


async def test_ssl_not_forced_without_certificate(env: Env) -> None:
    await env.configure_npm()
    await env.installed(proxy=ProxySettings(subdomain="notes", ssl_forced=True))
    [host] = env.f.proxy.hosts.values()
    assert env.f.proxy.settings[host.id].ssl_forced is False


async def test_unhealthy_install_keeps_container_output(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def logs(container_id: str, tail: int = 200) -> str:
        return "starting...\nerror: DATABASE_URL is not set token=abc123"

    monkeypatch.setattr(env.f.docker, "logs", logs)
    app_id = await env.new_app()
    env.f.docker.fail_next("health")
    with pytest.raises(DeployError):
        await env.orch.install(app_id)
    [event] = [e for e in env.f.events.events if e["kind"] == "install.logs"]
    assert event["level"] == "error"
    assert "DATABASE_URL is not set" in event["message"] and "abc123" not in event["message"]
    stored = await env.f.apps.get(app_id)
    assert "did not become healthy" in (stored.last_error or "")
