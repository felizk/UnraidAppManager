from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import TEMPLATE_FIXTURES, load_template
from tests.fakes import FakeCodec, FakePortAllocator, InMemorySettingsStore, make_app
from uam.domain import (
    LABEL_APP_ID,
    LABEL_MANAGED,
    LABEL_TEMPLATE_COMMIT,
    LABEL_UNRAID_ICON,
    LABEL_UNRAID_MANAGED,
    LABEL_UNRAID_WEBUI,
    ContainerSpec,
    TemplateConfig,
    UnraidTemplate,
)
from uam.errors import ValidationError
from uam.services.spec_resolver import DefaultSpecResolver, replace_image_tag
from uam.settings import Settings

PHOTOSYNC_OVERRIDES = {"Variable:CLOUD_USER": "me@example.com", "Variable:CLOUD_PASSWORD": "pw"}


def tpl(name: str) -> UnraidTemplate:
    return FakeCodec().parse(load_template(name))


def cfg(type_: str, target: str, value: str = "", **kw: Any) -> TemplateConfig:
    base: dict[str, Any] = {"name": target, "target": target, "default": "", "value": value, "type": type_}
    base.update(kw)
    return TemplateConfig(**base)


def custom(*configs: TemplateConfig, **kw: Any) -> UnraidTemplate:
    base: dict[str, Any] = {"name": "x", "repository": "ghcr.io/a/b:1", "configs": configs}
    base.update(kw)
    return UnraidTemplate(**base)


@pytest.fixture
def ports() -> FakePortAllocator:
    return FakePortAllocator()


@pytest.fixture
def resolver(ports: FakePortAllocator) -> DefaultSpecResolver:
    return DefaultSpecResolver(ports, InMemorySettingsStore())


@pytest.mark.parametrize("fixture", TEMPLATE_FIXTURES)
async def test_every_fixture_resolves(resolver: DefaultSpecResolver, fixture: str) -> None:
    app = make_app(id=3, overrides=dict(PHOTOSYNC_OVERRIDES), name="app")
    result = await resolver.resolve(app, tpl(fixture))
    spec = result.spec
    assert spec.name == "app"
    assert spec.labels[LABEL_MANAGED] == "true"
    assert spec.labels[LABEL_APP_ID] == "3"
    assert spec.labels[LABEL_UNRAID_MANAGED] == "uam"
    for b in spec.ports:
        assert 30000 <= b.host_port <= 30999


async def test_simple_web(resolver: DefaultSpecResolver) -> None:
    app = make_app(id=1, installed_template_commit="abc")
    r = await resolver.resolve(app, tpl("simple-web.xml"))
    assert r.spec.image == "ghcr.io/felizk/notes:latest"
    assert r.spec.env == {"TZ": "Europe/Copenhagen"}
    assert [(m.host_path, m.container_path, m.mode) for m in r.spec.mounts] == [
        ("/mnt/user/appdata/notes", "/data", "rw")
    ]
    (b,) = r.spec.ports
    assert (b.container_port, b.protocol) == (3000, "tcp")
    assert r.webui_container_port == 3000
    assert r.webui_host_port == b.host_port
    assert r.webui_url_template == f"http://[IP]:{b.host_port}/"
    assert r.spec.labels[LABEL_UNRAID_WEBUI] == r.webui_url_template
    assert r.spec.labels[LABEL_TEMPLATE_COMMIT] == "abc"
    assert r.spec.restart_policy == "unless-stopped"
    assert r.warnings == ()


async def test_multi_port_with_extra_and_post_args(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(id=2), tpl("multi-port.xml"))
    spec = r.spec
    assert [(b.container_port, b.protocol) for b in spec.ports] == [(53, "tcp"), (53, "udp"), (8080, "tcp")]
    assert len({(b.host_port, b.protocol) for b in spec.ports}) == 3
    assert spec.command == ("--log-level", "info")
    assert spec.restart_policy == "unless-stopped"
    assert spec.labels["com.example.role"] == "dns"
    assert [m.mode for m in spec.mounts] == ["rw", "ro"]
    admin = next(b for b in spec.ports if b.container_port == 8080)
    assert r.webui_url_template == f"http://[IP]:{admin.host_port}/admin"


async def test_masked_extra_warns_and_maps(resolver: DefaultSpecResolver) -> None:
    app = make_app(id=4, overrides=dict(PHOTOSYNC_OVERRIDES))
    r = await resolver.resolve(app, tpl("masked-extra.xml"))
    spec = r.spec
    assert spec.memory_limit == "1g"
    assert spec.cpus == 1.5
    assert spec.hostname == "photosync"
    assert spec.cap_add == ("SYS_ADMIN",)
    assert spec.log_opts == {"max-size": "10m"}
    assert spec.cpuset == "2,3"
    assert spec.devices == ("/dev/dri",)
    assert spec.env["CLOUD_USER"] == "me@example.com"
    assert spec.env["API_TOKEN"] == ""
    assert any("--sysctl" in w for w in r.warnings)
    mounts = [(m.host_path, m.container_path, m.mode) for m in spec.mounts]
    assert ("/mnt/user/photos", "/photos", "rw,slave") in mounts


async def test_appdata_substitution(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(overrides=dict(PHOTOSYNC_OVERRIDES)), tpl("masked-extra.xml"))
    assert ("/mnt/user/appdata/photosync", "/config") in [
        (m.host_path, m.container_path) for m in r.spec.mounts
    ]


async def test_appdata_root_from_settings(ports: FakePortAllocator) -> None:
    store = InMemorySettingsStore(Settings(appdata_root="/srv/data", unraid_managed_label="x"))
    r = await DefaultSpecResolver(ports, store).resolve(
        make_app(overrides=dict(PHOTOSYNC_OVERRIDES)), tpl("masked-extra.xml")
    )
    assert any(m.host_path == "/srv/data/photosync" for m in r.spec.mounts)
    assert r.spec.labels[LABEL_UNRAID_MANAGED] == "x"


async def test_legacy_template(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(id=5), tpl("legacy.xml"))
    assert r.spec.image == "felizk/oldtimer"
    assert {(b.container_port, b.protocol) for b in r.spec.ports} == {(8000, "tcp"), (1900, "udp")}


# ---- override precedence ----


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "value"),
        ({"Variable:X": "override"}, "override"),
        ({"Variable:X": ""}, ""),
        ({"Variable:Y": "other"}, "value"),
    ],
)
async def test_override_precedence_variable(
    resolver: DefaultSpecResolver, overrides: dict[str, str], expected: str
) -> None:
    t = custom(cfg("Variable", "X", "value", default="default"))
    r = await resolver.resolve(make_app(overrides=overrides), t)
    assert r.spec.env["X"] == expected


async def test_default_used_when_value_empty(resolver: DefaultSpecResolver) -> None:
    t = custom(cfg("Variable", "X", "", default="default"))
    r = await resolver.resolve(make_app(), t)
    assert r.spec.env["X"] == "default"


async def test_override_path_and_label_and_device(resolver: DefaultSpecResolver) -> None:
    t = custom(
        cfg("Path", "/config", "/a", mode="ro"),
        cfg("Label", "l", "1"),
        cfg("Device", "/dev/x", "/dev/x"),
    )
    app = make_app(overrides={"Path:/config": "[APPDATA]/z", "Label:l": "2", "Device:/dev/x": "/dev/y"})
    r = await resolver.resolve(app, t)
    assert [(m.host_path, m.mode) for m in r.spec.mounts] == [("/mnt/user/appdata/z", "ro")]
    assert r.spec.labels["l"] == "2"
    assert r.spec.devices == ("/dev/y",)


async def test_port_override_key_includes_mode(resolver: DefaultSpecResolver) -> None:
    t = custom(cfg("Port", "53", "53", mode="tcp"), cfg("Port", "53", "53", mode="udp"))
    app = make_app(id=1, auto_ports=False, overrides={"Port:53/udp": "5353"})
    res = await resolver.resolve(app, t)
    assert {(b.protocol, b.host_port) for b in res.spec.ports} == {("tcp", 53), ("udp", 5353)}


# ---- ports ----


async def test_auto_ports_prefers_resolved_value(resolver: DefaultSpecResolver) -> None:
    t = custom(cfg("Port", "80", "30080", mode="tcp"))
    r = await resolver.resolve(make_app(id=1), t)
    assert r.spec.ports[0].host_port == 30080


async def test_auto_ports_remaps_out_of_range(resolver: DefaultSpecResolver) -> None:
    t = custom(cfg("Port", "80", "80"))
    r = await resolver.resolve(make_app(id=1), t)
    assert r.spec.ports[0].host_port == 30000


async def test_manual_ports_used_as_is(resolver: DefaultSpecResolver, ports: FakePortAllocator) -> None:
    t = custom(cfg("Port", "80", "80"))
    r = await resolver.resolve(make_app(id=1, auto_ports=False), t)
    assert r.spec.ports[0].host_port == 80
    assert ports.allocs == {}


@pytest.mark.parametrize("bad", ["0", "65536", "-1", "abc", "80.5"])
@pytest.mark.parametrize("auto", [True, False])
async def test_invalid_port_value(resolver: DefaultSpecResolver, bad: str, auto: bool) -> None:
    t = custom(cfg("Port", "80", bad))
    with pytest.raises(ValidationError):
        await resolver.resolve(make_app(auto_ports=auto), t)


async def test_invalid_container_port(resolver: DefaultSpecResolver) -> None:
    with pytest.raises(ValidationError):
        await resolver.resolve(make_app(), custom(cfg("Port", "99999", "80")))


async def test_invalid_protocol(resolver: DefaultSpecResolver) -> None:
    with pytest.raises(ValidationError):
        await resolver.resolve(make_app(), custom(cfg("Port", "80", "80", mode="sctp")))


async def test_optional_empty_port_skipped(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(), tpl("multi-port.xml"))
    assert 9100 not in [b.container_port for b in r.spec.ports]


async def test_host_network_skips_ports(resolver: DefaultSpecResolver, ports: FakePortAllocator) -> None:
    t = custom(cfg("Port", "8080", "8080"), network="host", webui="http://[IP]:[PORT:8080]/")
    r = await resolver.resolve(make_app(id=1), t)
    assert r.spec.ports == ()
    assert r.spec.network == "host"
    assert any("host" in w for w in r.warnings)
    assert r.webui_url_template == "http://[IP]:8080/"
    assert r.webui_host_port == 8080
    assert ports.allocs == {}


# ---- required fields ----


async def test_required_variables_list_every_missing_name(resolver: DefaultSpecResolver) -> None:
    with pytest.raises(ValidationError) as exc:
        await resolver.resolve(make_app(), tpl("masked-extra.xml"))
    assert "CLOUD_USER" in str(exc.value)
    assert "CLOUD_PASSWORD" in str(exc.value)
    assert "API_TOKEN" not in str(exc.value)


async def test_required_variable_whitespace_is_missing(resolver: DefaultSpecResolver) -> None:
    with pytest.raises(ValidationError):
        await resolver.resolve(make_app(), custom(cfg("Variable", "A", "  ", required=True)))


async def test_required_path_empty(resolver: DefaultSpecResolver) -> None:
    t = custom(cfg("Path", "/data", "", required=True))
    with pytest.raises(ValidationError, match="/data"):
        await resolver.resolve(make_app(), t)


async def test_optional_path_empty_skipped(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(), custom(cfg("Path", "/data", "")))
    assert r.spec.mounts == ()


async def test_required_port_empty(resolver: DefaultSpecResolver) -> None:
    with pytest.raises(ValidationError):
        await resolver.resolve(make_app(), custom(cfg("Port", "80", "", required=True)))


async def test_required_satisfied_by_override(resolver: DefaultSpecResolver) -> None:
    t = custom(cfg("Variable", "A", "", required=True))
    r = await resolver.resolve(make_app(overrides={"Variable:A": "x"}), t)
    assert r.spec.env == {"A": "x"}


# ---- image tag ----


@pytest.mark.parametrize(
    ("image", "tag", "expected"),
    [
        ("ghcr.io/a/b:1", "2", "ghcr.io/a/b:2"),
        ("localhost:5000/x", "dev", "localhost:5000/x:dev"),
        ("localhost:5000/x:1", "dev", "localhost:5000/x:dev"),
        ("felizk/oldtimer", "v3", "felizk/oldtimer:v3"),
        ("nginx", "alpine", "nginx:alpine"),
        ("ghcr.io/a/b@sha256:abc", "2", "ghcr.io/a/b:2"),
        ("ghcr.io/a/b:1@sha256:abc", "2", "ghcr.io/a/b:2"),
    ],
)
def test_replace_image_tag(image: str, tag: str, expected: str) -> None:
    assert replace_image_tag(image, tag) == expected


async def test_image_tag_override_applied(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(image_tag_override="9"), tpl("multi-port.xml"))
    assert r.spec.image == "ghcr.io/felizk/dnsbox:9"


# ---- extras, webui, labels ----


@pytest.mark.parametrize(
    ("extra", "check"),
    [
        ("--restart=always", lambda s: s.restart_policy == "always"),
        ("-e A=1", lambda s: s.env["A"] == "1"),
        ("-e TZ=UTC", lambda s: s.env["TZ"] == "UTC"),
        ("-l a=b", lambda s: s.labels["a"] == "b"),
        ("--device /dev/fuse", lambda s: s.devices == ("/dev/fuse",)),
        ("--network mynet", lambda s: s.extra_networks == ("mynet",)),
        ("--network bridge", lambda s: s.extra_networks == ()),
    ],
)
async def test_extra_params_applied(
    resolver: DefaultSpecResolver, extra: str, check: Callable[[ContainerSpec], bool]
) -> None:
    t = dataclasses.replace(tpl("simple-web.xml"), extra_params=extra)
    r = await resolver.resolve(make_app(), t)
    assert check(r.spec)
    assert r.warnings == ()


async def test_add_host_warns(resolver: DefaultSpecResolver) -> None:
    t = dataclasses.replace(tpl("simple-web.xml"), extra_params="--add-host=a:1.2.3.4")
    r = await resolver.resolve(make_app(), t)
    assert len(r.warnings) == 1
    assert "--add-host" in r.warnings[0]


async def test_post_args_quoting(resolver: DefaultSpecResolver) -> None:
    t = dataclasses.replace(tpl("simple-web.xml"), post_args='serve --name "a b"')
    r = await resolver.resolve(make_app(), t)
    assert r.spec.command == ("serve", "--name", "a b")


async def test_post_args_invalid(resolver: DefaultSpecResolver) -> None:
    t = dataclasses.replace(tpl("simple-web.xml"), post_args='serve "oops')
    with pytest.raises(ValidationError):
        await resolver.resolve(make_app(), t)


async def test_webui_port_follows_allocation(resolver: DefaultSpecResolver, ports: FakePortAllocator) -> None:
    ports.external[(30000, "tcp")] = "other"
    t = custom(cfg("Port", "80", "80"), webui="https://[IP]:[PORT:80]/x?y=[PORT:80]", icon="http://i/png")
    r = await resolver.resolve(make_app(id=1), t)
    assert r.webui_host_port == 30001
    assert r.webui_url_template == "https://[IP]:30001/x?y=30001"
    assert r.spec.labels[LABEL_UNRAID_ICON] == "http://i/png"


async def test_webui_prefers_tcp_binding(resolver: DefaultSpecResolver) -> None:
    t = custom(
        cfg("Port", "53", "30053", mode="udp"),
        cfg("Port", "53", "30054", mode="tcp"),
        webui="http://[IP]:[PORT:53]",
    )
    r = await resolver.resolve(make_app(id=1, auto_ports=False), t)
    assert r.webui_host_port == 30054


async def test_no_webui(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(), custom())
    assert r.webui_url_template is None
    assert r.webui_host_port is None
    assert r.webui_container_port is None
    assert LABEL_UNRAID_WEBUI not in r.spec.labels
    assert LABEL_UNRAID_ICON not in r.spec.labels
    assert LABEL_TEMPLATE_COMMIT not in r.spec.labels


async def test_webui_unmatched_port_left_alone(resolver: DefaultSpecResolver) -> None:
    r = await resolver.resolve(make_app(), custom(webui="http://[IP]:[PORT:9]/"))
    assert r.webui_url_template == "http://[IP]:[PORT:9]/"
    assert r.webui_host_port is None
    assert r.webui_container_port == 9


async def test_resolve_is_stable(resolver: DefaultSpecResolver) -> None:
    app = make_app(id=7)
    a = await resolver.resolve(app, tpl("multi-port.xml"))
    b = await resolver.resolve(app, tpl("multi-port.xml"))
    assert a == b
