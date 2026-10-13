from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FIXTURES, FrozenClock
from tests.fakes import FakeBundle, make_app, make_source
from uam.contracts import AppRepository
from uam.domain import (
    App,
    AppState,
    ContainerInfo,
    ImportOptions,
    Mount,
    NewSource,
    PortBinding,
    ProxySettings,
    Source,
)
from uam.errors import ConflictError, NotFound, ValidationError
from uam.services.import_matching import OCI_SOURCE_LABEL
from uam.services.importer import DefaultImporter

CID = "a" * 64
IMAGE = "ghcr.io/felizk/notes:1.2"
DIGEST = "ghcr.io/felizk/notes@sha256:" + "b" * 64
REPO_XML = (FIXTURES / "import" / "repo-notes.xml").read_text()
FLASH_XML = (FIXTURES / "import" / "flash-my-notes.xml").read_text()


class StubReconfigurer:
    def __init__(self, apps: AppRepository) -> None:
        self.apps = apps
        self.calls: list[int] = []
        self.error: Exception | None = None

    async def reconfigure(
        self,
        app_id: int,
        overrides: dict[str, str] | None = None,
        proxy: ProxySettings | None = None,
        image_tag_override: str | None = None,
    ) -> App:
        self.calls.append(app_id)
        if self.error:
            raise self.error
        app = await self.apps.get(app_id)
        app.labels_pending = False
        await self.apps.save(app)
        return app


def foreign(**kw: Any) -> ContainerInfo:
    base: dict[str, Any] = {
        "id": CID,
        "name": "notes",
        "image": IMAGE,
        "image_digest": DIGEST,
        "state": "running",
        "health": None,
        "labels": {"net.unraid.docker.managed": "dockerman"},
        "published_ports": (PortBinding(3000, "tcp", 8080), PortBinding(9999, "tcp", 9999)),
        "image_labels": {OCI_SOURCE_LABEL: "https://github.com/felizk/notes"},
        "env": {
            "PATH": "/usr/bin",
            "TZ": "Europe/Copenhagen",
            "DEBUG": "1",
            "API_TOKEN": "hunter2",
            "FOO": "bar",
            "SECRET_KEY": "s3cr3t",
        },
        "mounts": (
            Mount("/mnt/user/appdata/notes-data", "/data"),
            Mount("/mnt/cache/extra", "/extra", "ro"),
        ),
    }
    base.update(kw)
    return ContainerInfo(**base)


class Env:
    def __init__(self, fakes: FakeBundle, tmp_path: Path, frozen_now: FrozenClock) -> None:
        self.fakes = fakes
        self.flash_dir = tmp_path / "flash"
        self.flash_dir.mkdir()
        self.archive_dir = tmp_path / "archive"
        fakes.settings.settings.template_export_dir = str(self.flash_dir)
        fakes.settings.settings.base_domain = "example.com"
        self.reconfigurer = StubReconfigurer(fakes.apps)
        self.importer = DefaultImporter(
            docker=fakes.docker,
            codec=fakes.codec,
            github=fakes.github,
            sources=fakes.sources,
            apps=fakes.apps,
            ports=fakes.ports,
            proxy=fakes.proxy,
            vault=fakes.vault,
            settings=fakes.settings,
            events=fakes.events,
            reconfigurer=self.reconfigurer,
            archive_dir=self.archive_dir,
            clock=frozen_now,
            hostname="",
        )

    def flash(self, name: str = "my-notes.xml", text: str = FLASH_XML) -> Path:
        path = self.flash_dir / name
        path.write_text(text)
        return path

    async def add_source(self, **kw: Any) -> Source:
        self.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", REPO_XML, commit_sha="c0ffee")
        return await self.fakes.sources.add(make_source(**kw))


@pytest.fixture
def env(fakes: FakeBundle, tmp_path: Path, frozen_now: FrozenClock) -> Env:
    fakes.docker.seed_container(foreign())
    return Env(fakes, tmp_path, frozen_now)


# ------------------------------------------------------------------ scan


async def test_scan_skips_managed_and_own(env: Env) -> None:
    d = env.fakes.docker
    d.seed_container(foreign(id="b" * 64, name="managed", labels={"uam.managed": "true"}))
    d.seed_container(foreign(id="c" * 64, name="uam"))
    env.importer._hostname = "c" * 12
    assert [c.container_name for c in await env.importer.scan()] == ["notes"]


async def test_scan_flash_template_by_name_then_by_content(env: Env) -> None:
    env.flash("my-other.xml")
    [c] = await env.importer.scan()
    assert c.flash_template is not None and c.flash_template_path == str(env.flash_dir / "my-other.xml")
    assert c.unraid_managed == "dockerman"
    env.flash()
    [c] = await env.importer.scan()
    assert c.flash_template_path == str(env.flash_dir / "my-notes.xml")


async def test_scan_flash_cache_follows_mtime(env: Env) -> None:
    path = env.flash()
    await env.importer.scan()
    path.write_text(FLASH_XML.replace("<Overview/>", "<Overview>changed</Overview>"))
    os.utime(path, ns=(1, 2_000_000_000))
    [c] = await env.importer.scan()
    assert c.flash_template is not None and c.flash_template.overview == "changed"


async def test_scan_unparseable_flash_template_warns(env: Env) -> None:
    env.flash(text="<Container><Name>notes</Name>")
    [c] = await env.importer.scan()
    assert c.flash_template is None and c.flash_template_path is not None
    assert any("could not be parsed" in w for w in c.warnings)


async def test_scan_matches_existing_source_and_fills_template_path(env: Env) -> None:
    src = await env.add_source()
    [c] = await env.importer.scan()
    [m] = c.source_matches
    assert (m.confidence, m.source_id, m.template_path) == ("high", src.id, "unraid/notes.xml")


async def test_scan_low_match_by_template_repository(env: Env) -> None:
    await env.add_source()
    env.fakes.docker.seed_container(foreign(image_labels={}, image="docker.io/felizk/notes:1"))
    env.fakes.github.put_file(
        "felizk", "notes", "unraid/notes.xml", REPO_XML.replace("ghcr.io/felizk/notes", "felizk/notes")
    )
    [c] = await env.importer.scan()
    [m] = c.source_matches
    assert (m.confidence, m.template_path) == ("low", "unraid/notes.xml")


async def test_scan_new_source_suggested_from_label(env: Env) -> None:
    [c] = await env.importer.scan()
    [m] = c.source_matches
    assert (m.confidence, m.source_id, m.template_path) == ("high", None, None)


async def test_scan_proxy_host_by_domain_and_by_port(env: Env) -> None:
    proxy = env.fakes.proxy
    by_port = proxy.seed_foreign_host("whatever.example.com", "192.168.1.10", 8080)
    [c] = await env.importer.scan()
    assert c.proxy_host is not None and c.proxy_host.id == by_port
    by_domain = proxy.seed_foreign_host("notes.example.com", "10.1.1.1", 1)
    [c] = await env.importer.scan()
    assert c.proxy_host is not None and c.proxy_host.id == by_domain


async def test_scan_blocker_when_app_exists(env: Env) -> None:
    await env.fakes.apps.add(make_app(name="notes"))
    [c] = await env.importer.scan()
    assert any("already exists" in b for b in c.blockers)


# ------------------------------------------------------------------ plan


async def plan(env: Env, options: ImportOptions | None = None, **kw: Any) -> Any:
    src = await env.add_source()
    return await env.importer.plan(
        "notes", kw.pop("source_id", src.id), None, "unraid/notes.xml", options or ImportOptions()
    )


async def test_plan_overrides(env: Env) -> None:
    p = await plan(env)
    assert p.overrides == {
        "Path:/data": "/mnt/user/appdata/notes-data",
        "Port:3000/tcp": "8080",
        "Variable:API_TOKEN": "hunter2",
    }
    assert "Variable:TZ" not in p.overrides
    assert p.template_commit == "c0ffee"


async def test_plan_flash_template_is_fallback_only(env: Env) -> None:
    env.flash()
    env.fakes.docker.seed_container(foreign(env={"PATH": "/x"}, mounts=()))
    p = await plan(env, ImportOptions(keep_host_ports=False))
    assert p.overrides["Variable:API_TOKEN"] == "hunter2"
    assert "Path:/data" not in p.overrides


async def test_plan_dropped_lists_unknown_settings(env: Env) -> None:
    p = await plan(env)
    assert set(p.dropped) == {
        "env DEBUG=1 (not in template; will be removed on recreate)",
        "env FOO=bar (not in template; will be removed on recreate)",
        "env SECRET_KEY=*** (not in template; will be removed on recreate)",
        "path /mnt/cache/extra:/extra:ro (not in template; will be removed on recreate)",
        "port 9999:9999/tcp (not in template; will be removed on recreate)",
    }


async def test_plan_dropped_masks_flash_masked_env(env: Env) -> None:
    env.flash(
        text=FLASH_XML.replace('Target="DEBUG"', 'Target="FOO"').replace('Mask="false">1', 'Mask="true">1')
    )
    p = await plan(env)
    assert "env FOO=*** (not in template; will be removed on recreate)" in p.dropped


async def test_plan_pins_running_tag(env: Env) -> None:
    p = await plan(env)
    assert p.image_tag_override == "1.2"
    assert p.installed_image_digest == DIGEST
    env.fakes.docker.seed_container(foreign(image="ghcr.io/felizk/notes:latest"))
    assert (
        await env.importer.plan("notes", p.source_id, None, "unraid/notes.xml", ImportOptions())
    ).image_tag_override is None


async def test_plan_pins_ports_outside_range(env: Env) -> None:
    p = await plan(env)
    assert p.pinned_ports == ((3000, "tcp", 8080),)
    assert not any("reallocated" in w for w in p.warnings)


async def test_plan_without_keep_ports_leaves_unpinned_and_warns(env: Env) -> None:
    p = await plan(env, ImportOptions(keep_host_ports=False))
    assert p.pinned_ports == ()
    assert "Port:3000/tcp" not in p.overrides
    assert any("WebUI and proxy port will change" in w for w in p.warnings)


async def test_plan_proxy_adoption_and_subdomain(env: Env) -> None:
    host_id = env.fakes.proxy.seed_foreign_host("wiki.example.com", "192.168.1.10", 8080)
    p = await plan(env)
    assert p.proxy.subdomain == "wiki" and p.adopt_proxy_host_id == host_id
    p2 = await env.importer.plan(
        "notes", p.source_id, None, "unraid/notes.xml", ImportOptions(claim_proxy_host=False, subdomain="n")
    )
    assert p2.proxy.subdomain == "n" and p2.adopt_proxy_host_id is None


async def test_plan_warns_when_proxy_port_is_not_webui(env: Env) -> None:
    env.fakes.proxy.seed_foreign_host("notes.example.com", "192.168.1.10", 9999)
    p = await plan(env)
    assert any("not the WebUI port" in w for w in p.warnings)


async def test_plan_template_diff(env: Env) -> None:
    p = await plan(env)
    assert p.template_diff == ""
    env.flash()
    p = await env.importer.plan("notes", p.source_id, None, "unraid/notes.xml", ImportOptions())
    assert "--- flash template" in p.template_diff
    assert "hunter2" not in p.template_diff
    assert "-  <Repository>ghcr.io/felizk/notes:1.2</Repository>" in p.template_diff


async def test_plan_local_image_is_a_blocker(env: Env) -> None:
    env.fakes.docker.seed_container(foreign(image_digest=None))
    p = await plan(env)
    assert any("RepoDigest" in b for b in p.candidate.blockers)
    with pytest.raises(ConflictError):
        await env.importer.apply(p)


async def test_plan_new_source_and_discovery(env: Env) -> None:
    env.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", REPO_XML)
    p = await env.importer.plan("notes", None, NewSource("felizk", "notes"), None, ImportOptions())
    assert p.source_id is None and p.new_source is not None and p.template_path == "unraid/notes.xml"
    assert env.fakes.sources.sources == {}


async def test_plan_errors(env: Env) -> None:
    with pytest.raises(NotFound):
        await env.importer.plan("missing", None, NewSource("a", "b"), "x.xml", ImportOptions())
    with pytest.raises(ValidationError):
        await env.importer.plan("notes", None, None, "x.xml", ImportOptions())


async def test_plan_device_and_label_overrides(env: Env) -> None:
    xml = REPO_XML.replace(
        "</Container>",
        '  <Config Name="GPU" Target="/dev/dri" Default="/dev/dri" Mode="" Type="Device">/dev/dri</Config>\n'
        '  <Config Name="L" Target="tier" Default="a" Mode="" Type="Label">a</Config>\n</Container>',
    )
    env.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", xml)
    src = await env.fakes.sources.add(make_source())
    env.fakes.docker.seed_container(foreign(devices=("/dev/dri2:/dev/dri",), labels={"tier": "b"}))
    p = await env.importer.plan("notes", src.id, None, "unraid/notes.xml", ImportOptions())
    assert p.overrides["Device:/dev/dri"] == "/dev/dri2:/dev/dri"
    assert p.overrides["Label:tier"] == "b"
    assert not any(line.startswith("device") for line in p.dropped)


# ------------------------------------------------------------------ apply


async def test_apply_imports_app(env: Env, frozen_now: FrozenClock) -> None:
    host_id = env.fakes.proxy.seed_foreign_host("wiki.example.com", "192.168.1.10", 8080)
    p = await plan(env)
    app = await env.importer.apply(p)
    stored = await env.fakes.apps.get(app.id)
    assert stored.name == "notes" and stored.state == AppState.RUNNING
    assert stored.container_id == CID and stored.installed_image_digest == DIGEST
    assert stored.installed_template_commit == "c0ffee" and stored.image_tag_override == "1.2"
    assert stored.last_change_at == frozen_now.now == stored.next_check_at
    assert stored.proxy.subdomain == "wiki" and stored.overrides == p.overrides
    assert env.fakes.ports.for_app(app.id) == {(3000, "tcp"): 8080}
    assert env.fakes.proxy.owners[host_id] == "notes"
    assert env.fakes.events.kinds(app.id) == ["import"]
    assert "FOO=bar" in env.fakes.events.events[0]["message"]
    assert env.reconfigurer.calls == [app.id]
    assert stored.labels_pending is False and stored.needs_attention is None


async def test_apply_saves_new_source_and_stopped_state(env: Env) -> None:
    env.fakes.docker.seed_container(foreign(state="exited"))
    env.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", REPO_XML)
    p = await env.importer.plan(
        "notes", None, NewSource("felizk", "notes"), None, ImportOptions(recreate_now=False)
    )
    app = await env.importer.apply(p)
    assert app.state == AppState.STOPPED and app.labels_pending is True
    assert (await env.fakes.sources.get(app.source_id)).repo == "notes"
    assert env.reconfigurer.calls == []


async def test_apply_stale_when_container_replaced(env: Env) -> None:
    p = await plan(env)
    del env.fakes.docker.containers[CID]
    env.fakes.docker.seed_container(foreign(id="d" * 64))
    with pytest.raises(ConflictError):
        await env.importer.apply(p)
    assert await env.fakes.apps.list() == []


async def test_apply_stale_when_blocker_appears(env: Env) -> None:
    p = await plan(env)
    await env.fakes.apps.add(make_app(name="notes"))
    with pytest.raises(ConflictError):
        await env.importer.apply(p)


async def test_apply_stale_when_container_already_managed(env: Env) -> None:
    p = await plan(env)
    env.fakes.docker.seed_container(foreign(labels={"uam.managed": "true"}))
    with pytest.raises(ConflictError):
        await env.importer.apply(p)


async def test_apply_rolls_back_when_npm_claim_fails(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.fakes.proxy.seed_foreign_host("wiki.example.com", "192.168.1.10", 8080)
    env.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", REPO_XML)
    p = await env.importer.plan("notes", None, NewSource("felizk", "notes"), None, ImportOptions())

    async def boom(host_id: int, app_name: str) -> None:
        raise RuntimeError("npm down")

    monkeypatch.setattr(env.fakes.proxy, "claim_proxy_host", boom)
    with pytest.raises(RuntimeError, match="npm down"):
        await env.importer.apply(p)
    assert await env.fakes.apps.list() == []
    assert env.fakes.sources.sources == {}
    assert env.fakes.ports.allocs == {}
    assert env.fakes.events.events == []
    assert env.reconfigurer.calls == []


async def test_apply_rolls_back_when_pin_fails(env: Env) -> None:
    env.fakes.ports.external[(8080, "tcp")] = "someone-else"
    p = await plan(env)
    with pytest.raises(ConflictError):
        await env.importer.apply(p)
    assert await env.fakes.apps.list() == []


async def test_apply_recreate_failure_keeps_import_and_flags_app(env: Env) -> None:
    flash = env.flash()
    env.reconfigurer.error = RuntimeError("unhealthy")
    p = await plan(env, ImportOptions(archive_flash_template=True))
    app = await env.importer.apply(p)
    stored = await env.fakes.apps.get(app.id)
    assert stored.labels_pending is True
    assert stored.needs_attention is not None and "unhealthy" in stored.needs_attention
    assert env.fakes.ports.for_app(app.id) == {(3000, "tcp"): 8080}
    assert flash.exists() and not env.archive_dir.exists()
    assert env.fakes.events.events[-1]["level"] == "error"


async def test_apply_archives_flash_template_after_recreate(env: Env) -> None:
    flash = env.flash()
    p = await plan(env, ImportOptions(archive_flash_template=True))
    await env.importer.apply(p)
    assert not flash.exists()
    assert (env.archive_dir / "my-notes.xml").read_text() == FLASH_XML


async def test_apply_does_not_archive_by_default(env: Env) -> None:
    flash = env.flash()
    await env.importer.apply(await plan(env))
    assert flash.exists()


def test_own_container_id_from_mountinfo(tmp_path) -> None:
    from uam.services.importer import own_container_id

    cid = "a" * 64
    info = tmp_path / "mountinfo"
    info.write_text(
        f"1 2 0:1 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw - ext4 /dev/sda rw\n"
    )
    assert own_container_id(str(info)) == cid
    info.write_text("1 2 0:1 / / rw - overlay overlay rw\n")
    assert own_container_id(str(info)) is None
    assert own_container_id(str(tmp_path / "missing")) is None


async def test_plan_keeps_existing_proxy_tls_settings(env: Env) -> None:
    from dataclasses import replace as dc_replace

    host_id = env.fakes.proxy.seed_foreign_host("wiki.example.com", "192.168.1.10", 8080)
    proxy = env.fakes.proxy
    proxy.hosts[host_id] = dc_replace(
        proxy.hosts[host_id], certificate_id=5, ssl_forced=True, websockets=True, access_list_id=2
    )
    p = await plan(env)
    assert (p.proxy.certificate_id, p.proxy.ssl_forced, p.proxy.websockets, p.proxy.access_list_id) == (
        5,
        True,
        True,
        2,
    )


async def test_plan_uses_default_certificate_for_plain_http_host(env: Env) -> None:
    env.fakes.proxy.seed_foreign_host("wiki.example.com", "192.168.1.10", 8080)
    s = await env.fakes.settings.get()
    s.default_certificate_id = 9
    await env.fakes.settings.save(s)
    p = await plan(env)
    assert p.proxy.certificate_id == 9 and p.proxy.ssl_forced
