"""Fakes for the port allocator, vault, template codec, spec resolver and importer."""

from __future__ import annotations

import itertools
import re
import xml.etree.ElementTree as ET  # noqa: S405 - only used to build XML, parsing goes through defusedxml
from collections.abc import Callable, Sequence
from dataclasses import fields, replace

from defusedxml import ElementTree as SafeET

from tests.fakes.factories import make_app
from tests.fakes.images import image_host, with_tag
from uam.domain import (
    LABEL_APP_ID,
    LABEL_MANAGED,
    LABEL_TEMPLATE_COMMIT,
    LABEL_UNRAID_ICON,
    LABEL_UNRAID_MANAGED,
    LABEL_UNRAID_WEBUI,
    App,
    AppState,
    ContainerSpec,
    Credential,
    CredentialKind,
    ImportCandidate,
    ImportOptions,
    ImportPlan,
    Mount,
    NewSource,
    PortAllocation,
    PortBinding,
    Protocol_,
    RegistryAuth,
    ResolvedApp,
    TemplateConfig,
    UnraidTemplate,
)
from uam.errors import ConflictError, NotFound, PortRangeExhausted, TemplateParseError

# ----------------------------------------------------------------- ports


class FakePortAllocator:
    """Same observable rules as T06, without a database or host probing.

    `external[(port, protocol)] = container_id` simulates ports published by containers UAM doesn't own.
    """

    def __init__(self, start: int = 30000, end: int = 30999) -> None:
        self.start, self.end = start, end
        self.allocs: dict[tuple[int, int, Protocol_], int] = {}
        self.external: dict[tuple[int, Protocol_], str] = {}

    def _taken(
        self, ignore_app: int | None = None, ignore_container: str | None = None
    ) -> set[tuple[int, Protocol_]]:
        taken: set[tuple[int, Protocol_]] = {
            (h, p) for (a, _, p), h in self.allocs.items() if a != ignore_app
        }
        taken |= {k for k, cid in self.external.items() if cid != ignore_container}
        return taken

    async def allocate(
        self, app_id: int, requests: Sequence[tuple[int, Protocol_, int | None]]
    ) -> list[PortAllocation]:
        wanted = {(app_id, c, p) for c, p, _ in requests}
        for key in [k for k in self.allocs if k[0] == app_id and k not in wanted]:
            del self.allocs[key]
        result = []
        for cport, proto, preferred in requests:
            key = (app_id, cport, proto)
            if key not in self.allocs:
                taken = self._taken()
                if preferred and self.start <= preferred <= self.end and (preferred, proto) not in taken:
                    self.allocs[key] = preferred
                else:
                    free = next((p for p in range(self.start, self.end + 1) if (p, proto) not in taken), None)
                    if free is None:
                        raise PortRangeExhausted(f"{self.start}-{self.end}")
                    self.allocs[key] = free
            result.append(PortAllocation(app_id, cport, proto, self.allocs[key]))
        return result

    async def pin(
        self,
        app_id: int,
        bindings: Sequence[tuple[int, Protocol_, int]],
        ignore_container_id: str | None = None,
    ) -> list[PortAllocation]:
        taken = self._taken(ignore_app=app_id, ignore_container=ignore_container_id)
        for _, proto, host in bindings:
            if (host, proto) in taken:
                raise ConflictError(f"port {host}/{proto} in use")
        for cport, proto, host in bindings:
            self.allocs[(app_id, cport, proto)] = host
        return [PortAllocation(app_id, c, p, h) for c, p, h in bindings]

    async def release(self, app_id: int) -> None:
        for key in [k for k in self.allocs if k[0] == app_id]:
            del self.allocs[key]

    async def allocations(self) -> list[PortAllocation]:
        return sorted(
            (PortAllocation(a, c, p, h) for (a, c, p), h in self.allocs.items()),
            key=lambda x: (x.host_port, x.protocol),
        )

    async def in_use(self) -> set[tuple[int, Protocol_]]:
        return {(h, p) for (_, _, p), h in self.allocs.items()} | set(self.external)

    def for_app(self, app_id: int) -> dict[tuple[int, str], int]:
        """Test helper: {(container_port, protocol): host_port}."""
        return {(c, p): h for (a, c, p), h in self.allocs.items() if a == app_id}


# ----------------------------------------------------------------- vault


class FakeVault:
    def __init__(self) -> None:
        self.creds: dict[int, tuple[Credential, str]] = {}
        self._ids = itertools.count(1)

    async def store(
        self, name: str, kind: CredentialKind, username: str, secret: str, registry_host: str = ""
    ) -> Credential:
        if any(c.name == name for c, _ in self.creds.values()):
            raise ConflictError(f"credential {name} exists")
        cred = Credential(next(self._ids), name, kind, username, registry_host)
        self.creds[cred.id] = (cred, secret)
        return cred

    async def update_secret(self, credential_id: int, secret: str) -> None:
        cred, _ = self._get(credential_id)
        self.creds[credential_id] = (cred, secret)

    async def update_username(self, credential_id: int, username: str) -> None:
        cred, secret = self._get(credential_id)
        self.creds[credential_id] = (replace(cred, username=username), secret)

    def _get(self, credential_id: int) -> tuple[Credential, str]:
        if credential_id not in self.creds:
            raise NotFound(f"credential {credential_id}")
        return self.creds[credential_id]

    async def reveal(self, credential_id: int) -> str:
        return self._get(credential_id)[1]

    async def get(self, credential_id: int) -> Credential:
        return self._get(credential_id)[0]

    async def delete(self, credential_id: int) -> None:
        self.creds.pop(credential_id, None)

    async def registry_auth_for(self, image_ref: str) -> RegistryAuth | None:
        host = image_host(image_ref)
        for cred, secret in self.creds.values():
            if cred.kind == CredentialKind.REGISTRY and cred.registry_host == host:
                return RegistryAuth(cred.username, secret, host)
        if host == "ghcr.io":
            for cred, secret in self.creds.values():
                if cred.kind == CredentialKind.GITHUB_PAT:
                    return RegistryAuth(cred.username, secret, host)
        return None

    async def list(self) -> list[Credential]:
        return [c for c, _ in self.creds.values()]


# ----------------------------------------------------------------- codec

_HEADER = {f.name: f.name for f in fields(UnraidTemplate) if f.name not in ("configs", "unknown_elements")}
_TAGS = {
    "name": "Name",
    "repository": "Repository",
    "registry": "Registry",
    "network": "Network",
    "privileged": "Privileged",
    "shell": "Shell",
    "support": "Support",
    "project": "Project",
    "overview": "Overview",
    "category": "Category",
    "webui": "WebUI",
    "icon": "Icon",
    "extra_params": "ExtraParams",
    "post_args": "PostArgs",
    "cpuset": "CPUset",
}
assert set(_TAGS) == set(_HEADER)


def _bool(text: str | None) -> bool:
    return (text or "").strip().lower() in ("true", "1", "yes")


class FakeCodec:
    """Good-enough parser/renderer for the fixture templates. T01 implements the real one."""

    def parse(self, xml: str) -> UnraidTemplate:
        try:
            root = SafeET.fromstring(xml)
        except SafeET.ParseError as e:
            raise TemplateParseError(str(e)) from e
        if root.tag != "Container":
            raise TemplateParseError(f"root is <{root.tag}>, expected <Container>")
        values: dict[str, object] = {}
        for attr, tag in _TAGS.items():
            text = (root.findtext(tag) or "").strip()
            values[attr] = _bool(text) if attr == "privileged" else text
        configs = [
            TemplateConfig(
                name=c.get("Name", ""),
                target=c.get("Target", ""),
                default=c.get("Default", ""),
                value=(c.text or "").strip(),
                type=c.get("Type", "Variable"),  # type: ignore[arg-type]
                mode=c.get("Mode", ""),
                description=c.get("Description", ""),
                display=c.get("Display", "always"),
                required=_bool(c.get("Required")),
                mask=_bool(c.get("Mask")),
            )
            for c in root.findall("Config")
        ]
        net = root.find("Networking")
        if net is not None:
            values["network"] = (net.findtext("Mode") or "bridge").strip()
            for p in net.iter("Port"):
                cport, host = p.findtext("ContainerPort", ""), p.findtext("HostPort", "")
                mode = p.findtext("Protocol", "tcp")
                configs.append(TemplateConfig(f"Port {cport}", cport, host, host, "Port", mode))
        for v in root.iter("Volume"):
            ctr, host = v.findtext("ContainerDir", ""), v.findtext("HostDir", "")
            configs.append(TemplateConfig(ctr, ctr, host, host, "Path", v.findtext("Mode", "rw")))
        env = root.find("Environment")
        if env is not None:
            for var in env.iter("Variable"):
                name, val = var.findtext("Name", ""), var.findtext("Value", "")
                configs.append(TemplateConfig(name, name, val, val, "Variable"))
        if not values["network"]:
            values["network"] = "bridge"
        if not values["shell"]:
            values["shell"] = "sh"
        return UnraidTemplate(configs=tuple(configs), **values)  # type: ignore[arg-type]

    def render(self, template: UnraidTemplate) -> str:
        root = ET.Element("Container", version="2")
        for attr, tag in _TAGS.items():
            value = getattr(template, attr)
            ET.SubElement(root, tag).text = str(value).lower() if isinstance(value, bool) else value
        for c in template.configs:
            el = ET.SubElement(
                root,
                "Config",
                Name=c.name,
                Target=c.target,
                Default=c.default,
                Mode=c.mode,
                Description=c.description,
                Type=c.type,
                Display=c.display,
                Required=str(c.required).lower(),
                Mask=str(c.mask).lower(),
            )
            el.text = c.value
        ET.indent(root)
        return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ----------------------------------------------------------------- spec resolver

_PORT_RE = re.compile(r"\[PORT:(\d+)\]")


def _proto(c: TemplateConfig) -> Protocol_:
    return "udp" if c.mode == "udp" else "tcp"


class FakeSpecResolver:
    """Straightforward template → spec mapping. Uses `ports` for allocation when `app.auto_ports` is set."""

    def __init__(self, ports: FakePortAllocator | None = None, managed_label: str = "uam") -> None:
        self.ports = ports
        self.managed_label = managed_label

    async def resolve(self, app: App, template: UnraidTemplate) -> ResolvedApp:
        def value(c: TemplateConfig) -> str:
            key = f"{c.type}:{c.target}" + (f"/{c.mode or 'tcp'}" if c.type == "Port" else "")
            return app.overrides.get(key, c.value or c.default)

        port_cfgs = [c for c in template.configs if c.type == "Port" and value(c)]
        if app.auto_ports and self.ports is not None:
            allocs = await self.ports.allocate(
                app.id, [(int(c.target), _proto(c), int(value(c))) for c in port_cfgs]
            )
            bindings = tuple(PortBinding(a.container_port, a.protocol, a.host_port) for a in allocs)
        else:
            bindings = tuple(PortBinding(int(c.target), _proto(c), int(value(c))) for c in port_cfgs)
        m = _PORT_RE.search(template.webui)
        webui_cport = int(m.group(1)) if m else None
        webui_host = next((b.host_port for b in bindings if b.container_port == webui_cport), None)
        webui = _PORT_RE.sub(str(webui_host), template.webui) if m and webui_host else template.webui or None
        image = (
            with_tag(template.repository, app.image_tag_override)
            if app.image_tag_override
            else template.repository
        )
        labels = {
            LABEL_MANAGED: "true",
            LABEL_APP_ID: str(app.id),
            LABEL_TEMPLATE_COMMIT: app.installed_template_commit or "",
            LABEL_UNRAID_MANAGED: self.managed_label,
            LABEL_UNRAID_ICON: template.icon,
            LABEL_UNRAID_WEBUI: webui or "",
        }
        labels.update({c.target: value(c) for c in template.configs if c.type == "Label"})
        spec = ContainerSpec(
            name=app.name,
            image=image,
            env={c.target: value(c) for c in template.configs if c.type == "Variable"},
            ports=bindings,
            mounts=tuple(
                Mount(value(c), c.target, c.mode or "rw")
                for c in template.configs
                if c.type == "Path" and value(c)
            ),
            devices=tuple(value(c) for c in template.configs if c.type == "Device" and value(c)),
            labels=labels,
            network=template.network,
            privileged=template.privileged,
        )
        return ResolvedApp(spec, webui, webui_host, webui_cport, ())


# ----------------------------------------------------------------- importer


class FakeImporter:
    """Returns canned candidates and plans; `apply` builds an App via `on_apply` (or a default)."""

    def __init__(self) -> None:
        self.candidates: list[ImportCandidate] = []
        self.plans: dict[str, ImportPlan] = {}
        self.applied: list[ImportPlan] = []
        self.on_apply: Callable[[ImportPlan], App] | None = None

    async def scan(self) -> list[ImportCandidate]:
        return list(self.candidates)

    async def plan(
        self,
        container_name: str,
        source_id: int | None,
        new_source: NewSource | None,
        template_path: str | None,
        options: ImportOptions,
    ) -> ImportPlan:
        if container_name not in self.plans:
            raise NotFound(container_name)
        return self.plans[container_name]

    async def apply(self, plan: ImportPlan) -> App:
        self.applied.append(plan)
        if self.on_apply:
            return self.on_apply(plan)
        return make_app(
            id=len(self.applied),
            name=plan.candidate.container_name,
            source_id=plan.source_id or 1,
            template_path=plan.template_path,
            overrides=dict(plan.overrides),
            state=AppState.RUNNING,
            container_id=plan.candidate.container_id,
            installed_image_digest=plan.installed_image_digest,
            installed_template_commit=plan.template_commit,
            labels_pending=not plan.options.recreate_now,
        )
