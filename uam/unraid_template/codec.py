"""Parse and render Unraid Docker templates (v2 format, with v1 legacy conversion)."""

from __future__ import annotations

import re
from typing import cast
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree as DefusedET

from uam.domain import ConfigType, TemplateConfig, UnraidTemplate
from uam.errors import TemplateParseError

_CONFIG_TYPES: tuple[str, ...] = ("Port", "Path", "Variable", "Label", "Device")
_TRUE = {"true", "1"}
_FALSE = {"false", "0"}
_WEBUI_PORT = re.compile(r"\[PORT:(\d+)\]")

# (tag, dataclass field, default used when the element is missing or empty)
_SCALARS: tuple[tuple[str, str, str], ...] = (
    ("Name", "name", ""),
    ("Repository", "repository", ""),
    ("Registry", "registry", ""),
    ("Network", "network", "bridge"),
    ("Privileged", "privileged", "false"),
    ("Shell", "shell", "sh"),
    ("Support", "support", ""),
    ("Project", "project", ""),
    ("Overview", "overview", ""),
    ("Category", "category", ""),
    ("WebUI", "webui", ""),
    ("Icon", "icon", ""),
    ("ExtraParams", "extra_params", ""),
    ("PostArgs", "post_args", ""),
    ("CPUset", "cpuset", ""),
)
_KNOWN_TAGS = {t for t, _, _ in _SCALARS} | {"Config", "Networking", "Data", "Environment"}


def webui_port(template: UnraidTemplate) -> int | None:
    """Return N from the first `[PORT:N]` in the template's WebUI, or None."""
    m = _WEBUI_PORT.search(template.webui)
    return int(m.group(1)) if m else None


def _parse_bool(text: str | None, what: str) -> bool:
    value = (text or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE or value == "":
        return False
    raise TemplateParseError(f"invalid boolean for {what}: {text!r}")


def _text(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _strip_ws(elem: ET.Element) -> None:
    """Drop whitespace-only text and all tails recursively so the form is canonical."""
    if elem.text is not None and not elem.text.strip():
        elem.text = None
    elem.tail = None
    for child in elem:
        _strip_ws(child)


def _serialize(elem: ET.Element, depth: int, out: list[str], indent: str = "  ") -> None:
    pad = indent * depth
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in elem.attrib.items())
    children = list(elem)
    if children:
        out.append(f"{pad}<{elem.tag}{attrs}>")
        for child in children:
            _serialize(child, depth + 1, out, indent)
        out.append(f"{pad}</{elem.tag}>")
    elif elem.text:
        out.append(f"{pad}<{elem.tag}{attrs}>{escape(elem.text)}</{elem.tag}>")
    else:
        out.append(f"{pad}<{elem.tag}{attrs}/>")


def _canonical_raw(elem: ET.Element) -> str:
    _strip_ws(elem)
    out: list[str] = []
    _serialize(elem, 0, out, indent="")
    return "".join(out)


def _config_from_element(elem: ET.Element) -> TemplateConfig:
    a = elem.attrib
    target = a.get("Target")
    if target is None:
        raise TemplateParseError("<Config> without Target attribute")
    ctype = a.get("Type", "")
    if ctype not in _CONFIG_TYPES:
        raise TemplateParseError(f"<Config Target={target!r}> has invalid Type {ctype!r}")
    return TemplateConfig(
        name=a.get("Name", target),
        target=target,
        default=a.get("Default", ""),
        value=elem.text or "",
        type=cast(ConfigType, ctype),
        mode=a.get("Mode", "tcp" if ctype == "Port" else ""),
        description=a.get("Description", ""),
        display=a.get("Display", "always") or "always",
        required=_parse_bool(a.get("Required"), "Config@Required"),
        mask=_parse_bool(a.get("Mask"), "Config@Mask"),
    )


def _legacy_configs(root: ET.Element) -> tuple[str | None, list[TemplateConfig]]:
    """Convert v1 Networking/Data/Environment sections; returns (network mode, configs)."""
    configs: list[TemplateConfig] = []
    network: str | None = None
    for net in root.findall("Networking"):
        mode = _text(net.find("Mode"))
        if mode and network is None:
            network = mode
        for port in net.findall("Publish/Port"):
            host = _text(port.find("HostPort"))
            container = _text(port.find("ContainerPort"))
            if not container:
                continue
            proto = (_text(port.find("Protocol")) or "tcp").lower()
            configs.append(
                TemplateConfig(
                    name=f"Port {container}/{proto}",
                    target=container,
                    default=host,
                    value=host,
                    type="Port",
                    mode=proto,
                )
            )
    for vol in root.findall("Data/Volume"):
        container = _text(vol.find("ContainerDir"))
        if not container:
            continue
        host = _text(vol.find("HostDir"))
        configs.append(
            TemplateConfig(
                name=f"Path {container}",
                target=container,
                default=host,
                value=host,
                type="Path",
                mode=_text(vol.find("Mode")) or "rw",
            )
        )
    for var in root.findall("Environment/Variable"):
        name = _text(var.find("Name"))
        if not name:
            continue
        value = (var.findtext("Value") or "").strip()
        configs.append(TemplateConfig(name=name, target=name, default=value, value=value, type="Variable"))
    return network, configs


class XmlTemplateCodec:
    """`TemplateCodec` implementation backed by defusedxml."""

    def parse(self, xml: str) -> UnraidTemplate:
        """Parse Unraid template XML (v2 or legacy) into an `UnraidTemplate`."""
        try:
            root = DefusedET.fromstring(xml.encode("utf-8"))
        except (ET.ParseError, ValueError) as exc:
            raise TemplateParseError(f"malformed template XML: {exc}") from exc
        if root.tag != "Container":
            raise TemplateParseError(f"root element is <{root.tag}>, expected <Container>")

        values: dict[str, str] = {}
        for tag, attr, default in _SCALARS:
            values[attr] = _text(root.find(tag)) or default
        if not values["name"]:
            raise TemplateParseError("template has no <Name>")
        if not values["repository"]:
            raise TemplateParseError("template has no <Repository>")
        privileged = _parse_bool(values.pop("privileged"), "Privileged")

        configs = [_config_from_element(e) for e in root.findall("Config")]
        legacy_network, legacy = _legacy_configs(root)
        if root.find("Network") is None and legacy_network:
            values["network"] = legacy_network
        existing = {(c.target, c.type) for c in configs}
        configs.extend(c for c in legacy if (c.target, c.type) not in existing)

        unknown = tuple((child.tag, _canonical_raw(child)) for child in root if child.tag not in _KNOWN_TAGS)
        return UnraidTemplate(
            privileged=privileged,
            configs=tuple(configs),
            unknown_elements=unknown,
            **values,
        )

    def render(self, template: UnraidTemplate) -> str:
        """Render a template in the canonical Unraid layout."""
        t = template
        scalars = {
            "Name": t.name,
            "Repository": t.repository,
            "Registry": t.registry,
            "Network": t.network,
            "Privileged": "true" if t.privileged else "false",
            "Shell": t.shell,
            "Support": t.support,
            "Project": t.project,
            "Overview": t.overview,
            "Category": t.category,
            "WebUI": t.webui,
            "Icon": t.icon,
            "ExtraParams": t.extra_params,
            "PostArgs": t.post_args,
            "CPUset": t.cpuset,
        }
        lines = ['<?xml version="1.0"?>', '<Container version="2">']
        for tag, text in scalars.items():
            lines.append(f"  <{tag}>{escape(text)}</{tag}>" if text else f"  <{tag}/>")
        for _tag, raw in t.unknown_elements:
            try:
                elem = DefusedET.fromstring(raw.encode("utf-8"))
            except ET.ParseError as exc:
                raise TemplateParseError(f"invalid unknown element XML: {exc}") from exc
            _serialize(elem, 1, lines)
        for c in t.configs:
            attrs = (
                f"Name={quoteattr(c.name)} Target={quoteattr(c.target)} "
                f"Default={quoteattr(c.default)} Mode={quoteattr(c.mode)} "
                f"Description={quoteattr(c.description)} Type={quoteattr(c.type)} "
                f"Display={quoteattr(c.display)} "
                f'Required="{"true" if c.required else "false"}" '
                f'Mask="{"true" if c.mask else "false"}"'
            )
            lines.append(f"  <Config {attrs}>{escape(c.value)}</Config>")
        lines.append("</Container>")
        return "\n".join(lines) + "\n"
