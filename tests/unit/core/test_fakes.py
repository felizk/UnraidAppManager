"""Sanity checks so tasks can trust the fakes."""

import pytest

from tests.conftest import TEMPLATE_FIXTURES, load_template
from tests.fakes import FakeBundle, make_app, make_source
from uam.domain import ContainerInfo, CredentialKind, ProxySettings
from uam.errors import ConflictError, NotFound, PortRangeExhausted, TemplateParseError


@pytest.mark.parametrize("name", TEMPLATE_FIXTURES)
def test_fake_codec_parses_every_fixture(fakes, name):
    tpl = fakes.codec.parse(load_template(name))
    assert tpl.name and tpl.repository
    assert any(c.type == "Port" for c in tpl.configs)
    assert fakes.codec.parse(fakes.codec.render(tpl)).configs == tpl.configs


def test_fake_codec_rejects_non_container(fakes):
    with pytest.raises(TemplateParseError):
        fakes.codec.parse("<Plugin/>")


async def test_fake_resolver_and_docker_flow(fakes: FakeBundle):
    tpl = fakes.codec.parse(load_template("multi-port.xml"))
    app = make_app(id=7, name="dnsbox")
    resolved = await fakes.spec_resolver.resolve(app, tpl)
    host_ports = {(b.container_port, b.protocol): b.host_port for b in resolved.spec.ports}
    assert resolved.webui_container_port == 8080 and resolved.webui_host_port == host_ports[(8080, "tcp")]

    fakes.docker.set_image(tpl.repository, "sha256:aaa")
    assert await fakes.docker.pull(tpl.repository, None) == "sha256:aaa"
    cid = await fakes.docker.create(resolved.spec)
    await fakes.docker.start(cid)
    assert await fakes.docker.wait_healthy(cid)
    with pytest.raises(ConflictError):
        await fakes.docker.create(resolved.spec)
    fakes.docker.fail_next("health")
    assert not await fakes.docker.wait_healthy(cid)
    await fakes.docker.remove(cid, force=True)
    assert await fakes.docker.inspect("dnsbox") is None


async def test_fake_ports(fakes):
    ports = fakes.ports
    ports.external[(30000, "tcp")] = "foreign"
    first = await ports.allocate(1, [(80, "tcp", None), (53, "udp", 30005)])
    assert [a.host_port for a in first] == [30001, 30005]
    assert await ports.allocate(1, [(80, "tcp", None), (53, "udp", 30005)]) == first
    await ports.pin(2, [(8080, "tcp", 8080)])
    with pytest.raises(ConflictError):
        await ports.pin(3, [(80, "tcp", 8080)])
    await ports.pin(4, [(80, "tcp", 30000)], ignore_container_id="foreign")
    small = type(ports)(31000, 31000)
    await small.allocate(1, [(1, "tcp", None)])
    with pytest.raises(PortRangeExhausted):
        await small.allocate(2, [(1, "tcp", None)])


async def test_fake_github_etag_semantics(fakes):
    src = make_source(id=1)
    fakes.github.put_file("felizk", "notes", "unraid/notes.xml", load_template("simple-web.xml"))
    fakes.github.put_file("felizk", "notes", "docs/other.xml", load_template("simple-web.xml"))
    assert [t.path for t in await fakes.github.discover_templates(src, None)] == ["unraid/notes.xml"]
    assert await fakes.github.latest_commit_for_path(src, "unraid/notes.xml", None) is not None
    assert await fakes.github.latest_commit_for_path(src, "unraid/notes.xml", None) is None
    fakes.github.put_file("felizk", "notes", "unraid/notes.xml", load_template("kit-myapp.xml"))
    assert await fakes.github.latest_commit_for_path(src, "unraid/notes.xml", None) is not None
    fakes.github.make_private("felizk", "notes", "ghp_x")
    with pytest.raises(NotFound):
        await fakes.github.discover_templates(src, None)


async def test_fake_proxy_and_vault(fakes):
    foreign = fakes.proxy.seed_foreign_host("notes.example.com", "192.168.1.10", 3000)
    with pytest.raises(ConflictError):
        await fakes.proxy.ensure_proxy_host("notes.example.com", "h", 1, ProxySettings(), None)
    await fakes.proxy.claim_proxy_host(foreign, "notes")
    assert await fakes.proxy.ensure_proxy_host("notes.example.com", "h", 1, ProxySettings(), None) == foreign

    await fakes.vault.store("gh", CredentialKind.GITHUB_PAT, "felizk", "ghp_x")
    auth = await fakes.vault.registry_auth_for("ghcr.io/felizk/notes:latest")
    assert auth is not None and auth.password == "ghp_x"
    assert await fakes.vault.registry_auth_for("nginx:latest") is None


def test_seed_foreign_container(fakes):
    info = ContainerInfo("abc", "legacy", "ghcr.io/x/y:1", "sha256:1", "running", None, {}, ())
    fakes.docker.seed_container(info)
    assert fakes.docker.by_name("legacy") == info
