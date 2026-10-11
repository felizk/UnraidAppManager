from __future__ import annotations

import dataclasses
import random

import pytest

from tests.conftest import TEMPLATE_FIXTURES, load_template
from uam.domain import TemplateConfig, UnraidTemplate
from uam.errors import TemplateParseError
from uam.unraid_template import XmlTemplateCodec, webui_port

codec = XmlTemplateCodec()
HEAD = "<Container><Name>a</Name><Repository>r</Repository>"


@pytest.mark.parametrize("name", TEMPLATE_FIXTURES)
def test_round_trip(name: str) -> None:
    first = codec.parse(load_template(name))
    assert codec.parse(codec.render(first)) == first


@pytest.mark.parametrize("name", TEMPLATE_FIXTURES)
def test_render_is_canonical(name: str) -> None:
    out = codec.render(codec.parse(load_template(name)))
    assert out.startswith('<?xml version="1.0"?>\n<Container version="2">\n  <Name>')
    assert codec.render(codec.parse(out)) == out


def test_simple_web() -> None:
    t = codec.parse(load_template("simple-web.xml"))
    assert (t.name, t.repository, t.shell, t.network) == (
        "notes",
        "ghcr.io/felizk/notes:latest",
        "sh",
        "bridge",
    )
    assert t.webui == "http://[IP]:[PORT:3000]/"
    assert [c.type for c in t.configs] == ["Port", "Path", "Variable"]
    assert t.configs[0] == TemplateConfig(
        name="Web UI",
        target="3000",
        default="3000",
        value="3000",
        type="Port",
        mode="tcp",
        description="HTTP port",
        display="always",
        required=True,
        mask=False,
    )
    assert [tag for tag, _ in t.unknown_elements] == ["TemplateURL"]
    assert webui_port(t) == 3000


def test_unknown_elements_rendered_before_configs() -> None:
    out = codec.render(codec.parse(load_template("simple-web.xml")))
    assert out.index("<CPUset/>") < out.index("<TemplateURL>") < out.index("<Config ")


def test_unknown_nested_preserved() -> None:
    xml = HEAD + '<Extra a="1"><X>1 &amp; 2</X><Y/></Extra><Zed>z</Zed></Container>'
    t = codec.parse(xml)
    assert [tag for tag, _ in t.unknown_elements] == ["Extra", "Zed"]
    out = codec.render(t)
    assert '  <Extra a="1">\n    <X>1 &amp; 2</X>\n    <Y/>\n  </Extra>\n  <Zed>z</Zed>' in out


def test_masked_extra() -> None:
    t = codec.parse(load_template("masked-extra.xml"))
    masks = {c.target: c.mask for c in t.configs}
    assert masks["CLOUD_PASSWORD"] and masks["API_TOKEN"] and not masks["CLOUD_USER"]
    assert t.configs[1].mode == "rw,slave"
    assert t.cpuset == "2,3"
    assert "--memory=1g" in t.extra_params
    assert [c for c in t.configs if c.type == "Device"][0].target == "/dev/dri"
    assert [c for c in t.configs if c.target == "API_TOKEN"][0].required is False


def test_multi_port() -> None:
    t = codec.parse(load_template("multi-port.xml"))
    ports = [(c.target, c.mode) for c in t.configs if c.type == "Port"]
    assert ports[:2] == [("53", "tcp"), ("53", "udp")]
    assert t.overview == "DNS server with an admin web interface & metrics."
    assert t.shell == "bash"
    assert t.configs[-1].display == "advanced-hide"
    assert webui_port(t) == 8080


def test_kit_template() -> None:
    t = codec.parse(load_template("kit-myapp.xml"))
    assert t.name == "myapp"
    assert t.cpuset == ""


def test_legacy_conversion() -> None:
    t = codec.parse(load_template("legacy.xml"))
    assert t.network == "bridge"
    assert [(c.type, c.target, c.mode, c.value) for c in t.configs] == [
        ("Port", "8000", "tcp", "8000"),
        ("Port", "1900", "udp", "1900"),
        ("Path", "/config", "rw", "/mnt/user/appdata/oldtimer"),
        ("Variable", "PUID", "", "99"),
        ("Variable", "PGID", "", "100"),
    ]
    assert [tag for tag, _ in t.unknown_elements] == ["BindTime"]
    out = codec.render(t)
    assert "<Networking>" not in out and 'Type="Port"' in out


def test_legacy_skips_existing_config() -> None:
    xml = (
        HEAD
        + '<Config Name="P" Target="80" Default="80" Mode="tcp" Type="Port">8080</Config>'
        + "<Networking><Mode>host</Mode><Publish><Port><HostPort>81</HostPort>"
        + "<ContainerPort>80</ContainerPort><Protocol>tcp</Protocol></Port>"
        + "<Port><HostPort>9</HostPort><ContainerPort>9</ContainerPort></Port></Publish></Networking>"
        + "<Environment><Variable><Name>80</Name><Value>x</Value></Variable></Environment>"
        + "</Container>"
    )
    t = codec.parse(xml)
    assert t.network == "host"
    assert [(c.type, c.target, c.value) for c in t.configs] == [
        ("Port", "80", "8080"),
        ("Port", "9", "9"),
        ("Variable", "80", "x"),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("true", True), ("True", True), ("1", True), ("false", False), ("False", False), ("0", False)],
)
def test_booleans(text: str, expected: bool) -> None:
    xml = (
        HEAD
        + f"<Privileged>{text}</Privileged>"
        + f'<Config Name="n" Target="T" Type="Variable" Required="{text}" Mask="{text}"/></Container>'
    )
    t = codec.parse(xml)
    assert t.privileged is expected
    assert t.configs[0].required is expected and t.configs[0].mask is expected


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "<Container><Name>a</Container>",
        "<Template><Name>a</Name></Template>",
        "<Container><Repository>r</Repository></Container>",
        "<Container><Name>a</Name></Container>",
        HEAD + "<Privileged>maybe</Privileged></Container>",
        HEAD + '<Config Name="x" Type="Port"/></Container>',
        HEAD + '<Config Target="x" Type="Bogus"/></Container>',
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Container><Name>&a;</Name></Container>',
    ],
)
def test_parse_errors(xml: str) -> None:
    with pytest.raises(TemplateParseError):
        codec.parse(xml)


def test_escaping_not_cdata() -> None:
    cfg = TemplateConfig("n<", "T", "d&", "v<&>\"'", "Variable", description='say "hi" <b>')
    t = UnraidTemplate(name="a", repository="r", overview="a < b & c > d", configs=(cfg,))
    out = codec.render(t)
    assert "CDATA" not in out
    assert "<Overview>a &lt; b &amp; c &gt; d</Overview>" in out
    assert codec.parse(out) == t


def test_escaping_random_strings() -> None:
    rng = random.Random(1234)  # noqa: S311
    alphabet = list("ab <>&\"'/=;[]:\t\né☃") + ["&amp;", "<![CDATA[", "]]>", "&#38;"]

    def s() -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))

    for _ in range(300):
        cfg = TemplateConfig(s() or "n", s() or "T", s(), s(), "Variable", description=s())
        t = UnraidTemplate(name="a", repository="r", overview=s().strip(), configs=(cfg,))
        assert codec.parse(codec.render(t)) == dataclasses.replace(t)


def test_webui_port_none() -> None:
    assert webui_port(UnraidTemplate(name="a", repository="r")) is None
    assert webui_port(UnraidTemplate(name="a", repository="r", webui="http://[IP]:80/")) is None
