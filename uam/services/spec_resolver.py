"""Turns an `(App, UnraidTemplate)` pair into a `ResolvedApp` (container spec plus WebUI info)."""

from __future__ import annotations

import re
import shlex

from uam.contracts import PortAllocator, SettingsStore
from uam.domain import (
    LABEL_APP_ID,
    LABEL_MANAGED,
    LABEL_TEMPLATE_COMMIT,
    LABEL_UNRAID_ICON,
    LABEL_UNRAID_MANAGED,
    LABEL_UNRAID_WEBUI,
    App,
    ContainerSpec,
    Mount,
    PortBinding,
    Protocol_,
    ResolvedApp,
    TemplateConfig,
    UnraidTemplate,
)
from uam.errors import ValidationError
from uam.services.extra_params import parse_extra_params

_WEBUI_PORT_RE = re.compile(r"\[PORT:(\d+)\]")

__all__ = ["DefaultSpecResolver", "override_key", "replace_image_tag"]


def replace_image_tag(image: str, tag: str) -> str:
    """Replace the tag (and any digest) of `image`, keeping the registry host and `host:port`."""
    ref = image.split("@", 1)[0]
    if ref.rfind(":") > ref.rfind("/"):
        ref = ref[: ref.rfind(":")]
    return f"{ref}:{tag}"


def override_key(config: TemplateConfig) -> str:
    """The `App.overrides` key for a template entry (`Port:<target>/<mode>` for ports)."""
    if config.type == "Port":
        return f"Port:{config.target}/{config.mode or 'tcp'}"
    return f"{config.type}:{config.target}"


class DefaultSpecResolver:
    """`SpecResolver` implementation: overrides, port allocation, ExtraParams and labels."""

    def __init__(self, ports: PortAllocator, settings: SettingsStore) -> None:
        self._ports = ports
        self._settings = settings

    async def resolve(self, app: App, template: UnraidTemplate) -> ResolvedApp:
        """Build the `ResolvedApp` for `app`; raises `ValidationError` for invalid or missing values."""
        settings = await self._settings.get()
        warnings: list[str] = []
        missing: list[str] = []

        def value(c: TemplateConfig) -> str:
            raw = app.overrides.get(override_key(c))
            if raw is None:
                raw = c.value or c.default
            return raw.strip() if c.type == "Port" else raw

        host_net = template.network == "host"

        env: dict[str, str] = {}
        labels: dict[str, str] = {}
        devices: list[str] = []
        mounts: list[Mount] = []
        for c in template.configs:
            v = value(c)
            if c.type == "Variable":
                if c.required and not v.strip():
                    missing.append(c.target)
                env[c.target] = v
            elif c.type == "Label":
                if v:
                    labels[c.target] = v
            elif c.type == "Device":
                if v:
                    devices.append(v)
                elif c.required:
                    missing.append(c.target)
            elif c.type == "Path":
                host_path = v.replace("[APPDATA]", settings.appdata_root)
                if not host_path.strip():
                    if c.required:
                        missing.append(c.target)
                    continue
                mounts.append(Mount(host_path, c.target, c.mode or "rw"))

        bindings: list[PortBinding] = []
        if host_net:
            if any(c.type == "Port" for c in template.configs):
                warnings.append("Network is 'host': port mappings are ignored")
        else:
            requests: dict[tuple[int, Protocol_], int] = {}
            for c in template.configs:
                if c.type != "Port":
                    continue
                v = value(c)
                if not v:
                    if c.required:
                        missing.append(f"{c.target}/{c.mode or 'tcp'}")
                    continue
                target = _port_number(c.target, f"container port of '{c.name}'")
                host = _port_number(v, f"port '{c.name}'")
                requests.setdefault((target, _protocol(c)), host)
            if app.auto_ports:
                allocs = await self._ports.allocate(app.id, [(t, p, h) for (t, p), h in requests.items()])
                bindings = [PortBinding(a.container_port, a.protocol, a.host_port) for a in allocs]
            else:
                bindings = [PortBinding(t, p, h) for (t, p), h in requests.items()]

        if missing:
            raise ValidationError("Missing required values: " + ", ".join(missing))

        extra = parse_extra_params(template.extra_params)
        warnings.extend(extra.warnings)
        env.update(extra.env)
        labels.update(extra.labels)
        for dev in extra.devices:
            if dev not in devices:
                devices.append(dev)
        try:
            command = tuple(shlex.split(template.post_args))
        except ValueError as e:
            raise ValidationError(f"PostArgs cannot be parsed: {e}") from e
        extra_networks = tuple(n for n in extra.networks if n != template.network)

        webui_template, webui_host, webui_container = _resolve_webui(template.webui, bindings, host_net)

        labels.update(
            {
                LABEL_MANAGED: "true",
                LABEL_APP_ID: str(app.id),
                LABEL_UNRAID_MANAGED: settings.unraid_managed_label,
            }
        )
        if app.installed_template_commit:
            labels[LABEL_TEMPLATE_COMMIT] = app.installed_template_commit
        if template.icon:
            labels[LABEL_UNRAID_ICON] = template.icon
        if webui_template:
            labels[LABEL_UNRAID_WEBUI] = webui_template

        image = template.repository
        if app.image_tag_override:
            image = replace_image_tag(image, app.image_tag_override)

        spec = ContainerSpec(
            name=app.name,
            image=image,
            env=env,
            ports=tuple(bindings),
            mounts=tuple(mounts),
            devices=tuple(devices),
            labels=labels,
            network=template.network or "bridge",
            privileged=template.privileged,
            restart_policy=extra.restart or "unless-stopped",
            hostname=extra.hostname,
            cap_add=tuple(extra.cap_add),
            memory_limit=extra.memory,
            cpus=extra.cpus,
            cpuset=template.cpuset or extra.cpuset or None,
            log_opts=extra.log_opts,
            command=command,
            extra_networks=extra_networks,
        )
        return ResolvedApp(spec, webui_template, webui_host, webui_container, tuple(warnings))


def _protocol(c: TemplateConfig) -> Protocol_:
    mode = (c.mode or "tcp").lower()
    if mode == "tcp":
        return "tcp"
    if mode == "udp":
        return "udp"
    raise ValidationError(f"Port '{c.name}' has invalid protocol '{c.mode}'")


def _port_number(raw: str, what: str) -> int:
    try:
        number = int(raw.strip())
    except ValueError:
        raise ValidationError(f"Invalid {what}: '{raw}' is not a number") from None
    if not 1 <= number <= 65535:
        raise ValidationError(f"Invalid {what}: {number} is outside 1-65535")
    return number


def _resolve_webui(
    webui: str, bindings: list[PortBinding], host_net: bool
) -> tuple[str | None, int | None, int | None]:
    """Replace `[PORT:N]` with the host port bound to container port N; `[IP]` is left alone."""
    if not webui:
        return None, None, None
    first = _WEBUI_PORT_RE.search(webui)
    container = int(first.group(1)) if first else None
    host_for: dict[int, int] = {}
    for b in sorted(bindings, key=lambda b: b.protocol != "tcp"):
        host_for.setdefault(b.container_port, b.host_port)

    def sub(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if host_net:
            return str(n)
        return str(host_for[n]) if n in host_for else m.group(0)

    resolved = _WEBUI_PORT_RE.sub(sub, webui)
    host: int | None = None
    if container is not None:
        host = container if host_net else host_for.get(container)
    return resolved, host, container
