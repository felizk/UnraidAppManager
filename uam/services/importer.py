"""Import containers that already run on the server (usually from Unraid's Docker tab) into UAM."""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from uam.contracts import (
    AppRepository,
    DockerEngine,
    EventLog,
    GitHubClient,
    PortAllocator,
    ProxyManager,
    SettingsStore,
    SourceRepository,
    TemplateCodec,
    Vault,
)
from uam.domain import (
    LABEL_MANAGED,
    LABEL_UNRAID_MANAGED,
    App,
    AppState,
    ContainerInfo,
    ImportCandidate,
    ImportOptions,
    ImportPlan,
    NewSource,
    PortBinding,
    Protocol_,
    ProxyHost,
    ProxySettings,
    Source,
    SourceMatch,
    TemplateConfig,
    UnraidTemplate,
)
from uam.errors import ConflictError, NotFound, TemplateParseError, UamError, ValidationError
from uam.services.import_matching import (
    choose_template,
    dedupe_matches,
    derive_subdomain,
    match_proxy_host,
    match_sources,
    split_image,
    template_matches_image,
)
from uam.settings import Settings

log = logging.getLogger(__name__)

_WEBUI_PORT_RE = re.compile(r"\[PORT:(\d+)\]")
_FLASH_NAME_RE = re.compile(r"<Name>\s*(.*?)\s*</Name>", re.DOTALL)
_SECRET_NAME_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL", re.IGNORECASE)
# The engine reports the whole container environment, so variables the image or Unraid itself sets are
# recognised by name. ContainerInfo has no separate image environment.
_IMAGE_ENV = frozenset(
    {"PATH", "HOME", "HOSTNAME", "TERM", "LANG", "LANGUAGE", "LC_ALL", "PWD", "SHLVL", "container"}
)
_UNRAID_ENV = frozenset({"HOST_OS", "HOST_HOSTNAME", "HOST_CONTAINERNAME"})
_UNRAID_ENV_PREFIXES = ("TCP_PORT_", "UDP_PORT_")
_REMOVED = "(not in template; will be removed on recreate)"


class AppReconfigurer(Protocol):
    """Recreates an app's container from its stored settings (T09's orchestrator in production)."""

    async def reconfigure(
        self,
        app_id: int,
        overrides: dict[str, str] | None = None,
        proxy: ProxySettings | None = None,
        image_tag_override: str | None = None,
    ) -> App: ...


@dataclass
class _Context:
    """Per-call state shared by candidates: sources, apps, proxy hosts and a discovery cache."""

    settings: Settings
    sources: list[Source]
    app_names: set[str]
    host_ip: str
    proxy_hosts: list[ProxyHost] = field(default_factory=list)
    proxy_error: str | None = None
    discovered: dict[tuple[str, str, str], list[tuple[str, UnraidTemplate]]] = field(default_factory=dict)
    discovery_errors: dict[tuple[str, str, str], str] = field(default_factory=dict)


def _proto(config: TemplateConfig) -> Protocol_:
    return "udp" if config.mode == "udp" else "tcp"


def _port_key(config: TemplateConfig) -> str:
    return f"Port:{config.target}/{config.mode or 'tcp'}"


def _config_default(config: TemplateConfig) -> str:
    return config.value if config.value != "" else config.default


def _device_host(device: str) -> str:
    return device.split(":", 1)[0]


def _device_matches(config: TemplateConfig, device: str) -> bool:
    """A device config matches by container-side target, or by equality with its value/default."""
    parts = device.split(":")
    container_side = parts[1] if len(parts) > 1 else parts[0]
    if config.target:
        return config.target in (container_side, device)
    return _device_host(device) in (config.value, config.default) or device in (config.value, config.default)


def _is_ignored_env(key: str) -> bool:
    return key in _IMAGE_ENV or key in _UNRAID_ENV or key.startswith(_UNRAID_ENV_PREFIXES)


def _unique_bindings(bindings: Sequence[PortBinding]) -> list[PortBinding]:
    seen: set[tuple[int, str, int]] = set()
    result: list[PortBinding] = []
    for b in bindings:
        key = (b.container_port, b.protocol, b.host_port)
        if key not in seen:
            seen.add(key)
            result.append(b)
    return result


def derive_overrides(
    template: UnraidTemplate,
    info: ContainerInfo,
    flash: UnraidTemplate | None,
    keep_host_ports: bool,
) -> dict[str, str]:
    """Overrides that make the repo template reproduce the running container.

    The container is the truth and the flash template a fallback. Only values that differ from
    the repo template's value/default are written.
    """
    flash_cfg = {(c.type, c.target): c for c in flash.configs} if flash else {}
    published = {(b.container_port, b.protocol): b.host_port for b in info.published_ports}
    overrides: dict[str, str] = {}
    for c in template.configs:
        fallback = flash_cfg.get((c.type, c.target))
        fallback_value = _config_default(fallback) if fallback else None
        actual: str | None = None
        key = f"{c.type}:{c.target}"
        if c.type == "Variable":
            actual = info.env.get(c.target, fallback_value)
        elif c.type == "Path":
            mount = next((m for m in info.mounts if m.container_path == c.target), None)
            actual = mount.host_path if mount else fallback_value
        elif c.type == "Label":
            actual = info.labels.get(c.target, fallback_value)
        elif c.type == "Device":
            device = next((d for d in info.devices if _device_matches(c, d)), None)
            actual = device if device is not None else fallback_value
        elif c.type == "Port" and keep_host_ports and c.target.isdigit():
            host = published.get((int(c.target), _proto(c)))
            actual = str(host) if host is not None else None
            key = _port_key(c)
        if actual is not None and actual != _config_default(c):
            overrides[key] = actual
    return overrides


def derive_dropped(
    template: UnraidTemplate, info: ContainerInfo, flash: UnraidTemplate | None
) -> tuple[str, ...]:
    """Human-readable lines for container settings the repo template has no config for."""
    masked = {c.target for c in flash.configs if c.mask} if flash else set()
    targets = {(c.type, c.target) for c in template.configs}
    dropped: list[str] = []
    for key, value in info.env.items():
        if ("Variable", key) in targets or _is_ignored_env(key):
            continue
        shown = "***" if key in masked or _SECRET_NAME_RE.search(key) else value
        dropped.append(f"env {key}={shown} {_REMOVED}")
    for m in info.mounts:
        if ("Path", m.container_path) not in targets:
            dropped.append(f"path {m.host_path}:{m.container_path}:{m.mode} {_REMOVED}")
    device_cfgs = [c for c in template.configs if c.type == "Device"]
    for d in info.devices:
        if not any(_device_matches(c, d) for c in device_cfgs):
            dropped.append(f"device {d} {_REMOVED}")
    port_cfgs = {(c.target, _proto(c)) for c in template.configs if c.type == "Port"}
    for b in _unique_bindings(info.published_ports):
        if (str(b.container_port), b.protocol) not in port_cfgs:
            dropped.append(f"port {b.host_port}:{b.container_port}/{b.protocol} {_REMOVED}")
    return tuple(dropped)


class DefaultImporter:
    """`Importer` implementation: scan, plan and apply the adoption of foreign containers."""

    def __init__(
        self,
        docker: DockerEngine,
        codec: TemplateCodec,
        github: GitHubClient,
        sources: SourceRepository,
        apps: AppRepository,
        ports: PortAllocator,
        proxy: ProxyManager | None,
        vault: Vault,
        settings: SettingsStore,
        events: EventLog,
        reconfigurer: AppReconfigurer,
        archive_dir: Path | str,
        clock: Callable[[], datetime] | None = None,
        hostname: str | None = None,
    ) -> None:
        self._docker = docker
        self._codec = codec
        self._github = github
        self._sources = sources
        self._apps = apps
        self._ports = ports
        self._proxy = proxy
        self._vault = vault
        self._settings = settings
        self._events = events
        self._reconfigurer = reconfigurer
        self._archive_dir = Path(archive_dir)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._hostname = hostname if hostname is not None else os.environ.get("HOSTNAME", "")
        self._flash_cache: dict[str, tuple[int, UnraidTemplate | None, str, str]] = {}

    # ------------------------------------------------------------------ scan

    async def scan(self) -> list[ImportCandidate]:
        """Candidates for every container that UAM does not manage and that is not UAM itself."""
        ctx = await self._context()
        candidates: list[ImportCandidate] = []
        for info in await self._docker.list_containers(all=True):
            if info.labels.get(LABEL_MANAGED) == "true" or self._is_own(info):
                continue
            candidates.append(await self._candidate(info, ctx))
        return candidates

    def _is_own(self, info: ContainerInfo) -> bool:
        return len(self._hostname) >= 12 and info.id.startswith(self._hostname)

    async def _context(self) -> _Context:
        settings = await self._settings.get()
        ctx = _Context(
            settings=settings,
            sources=await self._sources.list(),
            app_names={a.name for a in await self._apps.list()},
            host_ip=settings.host_ip or await self._docker.host_ip(),
        )
        if self._proxy is not None:
            try:
                ctx.proxy_hosts = await self._proxy.list_proxy_hosts()
            except Exception as e:
                ctx.proxy_error = f"could not list NPM proxy hosts: {e}"
        return ctx

    async def _candidate_for(self, name: str) -> ImportCandidate | None:
        """The candidate for one container name, own container included (with a blocker)."""
        ctx = await self._context()
        for info in await self._docker.list_containers(all=True):
            if info.name == name and info.labels.get(LABEL_MANAGED) != "true":
                return await self._candidate(info, ctx)
        return None

    async def _candidate(self, info: ContainerInfo, ctx: _Context) -> ImportCandidate:
        blockers: list[str] = []
        warnings: list[str] = []
        if info.name in ctx.app_names:
            blockers.append(f"A UAM app named {info.name} already exists")
        if self._is_own(info):
            blockers.append("This is UAM's own container")
        if ctx.proxy_error:
            warnings.append(ctx.proxy_error)

        flash_path, flash, flash_warnings = await asyncio.to_thread(
            self._find_flash_template, ctx.settings.template_export_dir, info.name
        )
        warnings += flash_warnings

        matches = match_sources(info.image, info.image_labels, flash, ctx.sources)
        matches = await self._fill_template_paths(matches, info, ctx, warnings)

        proxy_host = None
        if self._proxy is not None and not ctx.proxy_error:
            proxy_host = match_proxy_host(
                ctx.proxy_hosts, info.name, ctx.settings.base_domain, info.published_ports, ctx.host_ip
            )
        return ImportCandidate(
            container_id=info.id,
            container_name=info.name,
            image=info.image,
            state=info.state,
            unraid_managed=info.labels.get(LABEL_UNRAID_MANAGED),
            flash_template_path=flash_path,
            flash_template=flash,
            source_matches=tuple(matches),
            published_ports=info.published_ports,
            proxy_host=proxy_host,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
        )

    async def _fill_template_paths(
        self, matches: list[SourceMatch], info: ContainerInfo, ctx: _Context, warnings: list[str]
    ) -> list[SourceMatch]:
        by_id = {s.id: s for s in ctx.sources}
        result: list[SourceMatch] = []
        for m in matches:
            source = by_id.get(m.source_id) if m.source_id is not None else None
            if source is not None:
                templates = await self._discover(source, ctx)
                path = choose_template(templates, info.image, info.name)
                m = replace(m, template_path=path)
                error = ctx.discovery_errors.get(self._source_key(source))
                if error:
                    warnings.append(f"template discovery failed for {source.owner}/{source.repo}: {error}")
            result.append(m)
        matched = {m.source_id for m in result if m.source_id is not None}
        for source in ctx.sources:
            if source.id in matched:
                continue
            templates = await self._discover(source, ctx)
            hit = next((p for p, t in templates if template_matches_image(t, info.image)), None)
            if hit is not None:
                result.append(
                    SourceMatch(
                        source.id,
                        source.owner,
                        source.repo,
                        hit,
                        "low",
                        f"template {hit} in {source.owner}/{source.repo} uses this image",
                    )
                )
        return dedupe_matches(result)

    @staticmethod
    def _source_key(source: Source) -> tuple[str, str, str]:
        return source.owner.lower(), source.repo.lower(), source.ref

    async def _token(self, credential_id: int | None) -> str | None:
        return await self._vault.reveal(credential_id) if credential_id is not None else None

    async def _discover(self, source: Source, ctx: _Context) -> list[tuple[str, UnraidTemplate]]:
        key = self._source_key(source)
        if key in ctx.discovered:
            return ctx.discovered[key]
        found: list[tuple[str, UnraidTemplate]] = []
        try:
            token = await self._token(source.credential_id)
            for d in await self._github.discover_templates(source, token):
                try:
                    found.append((d.path, self._codec.parse(d.xml)))
                except TemplateParseError:
                    continue
        except UamError as e:
            ctx.discovery_errors[key] = str(e)
        ctx.discovered[key] = found
        return found

    # ----- flash templates

    def _load_flash(self, path: Path) -> tuple[UnraidTemplate | None, str, str]:
        """Parse a flash template, cached by mtime. Returns (template, raw `<Name>`, parse error)."""
        mtime = path.stat().st_mtime_ns
        cached = self._flash_cache.get(str(path))
        if cached is None or cached[0] != mtime:
            text = path.read_text(encoding="utf-8", errors="replace")
            m = _FLASH_NAME_RE.search(text)
            raw_name = m.group(1) if m else ""
            try:
                cached = (mtime, self._codec.parse(text), raw_name, "")
            except TemplateParseError as e:
                cached = (mtime, None, raw_name, str(e))
            self._flash_cache[str(path)] = cached
        return cached[1], cached[2], cached[3]

    def _find_flash_template(
        self, directory: str, name: str
    ) -> tuple[str | None, UnraidTemplate | None, list[str]]:
        base = Path(directory)
        direct = base / f"my-{name}.xml"
        try:
            if direct.is_file():
                tpl, _, error = self._load_flash(direct)
                if tpl is None:
                    return str(direct), None, [f"flash template {direct.name} could not be parsed: {error}"]
                return str(direct), tpl, []
            if not base.is_dir():
                return None, None, []
            for path in sorted(base.glob("my-*.xml")):
                tpl, raw_name, error = self._load_flash(path)
                if tpl is not None and tpl.name == name:
                    return str(path), tpl, []
                if tpl is None and raw_name == name:
                    return str(path), None, [f"flash template {path.name} could not be parsed: {error}"]
        except OSError as e:
            return None, None, [f"could not read flash templates in {directory}: {e}"]
        return None, None, []

    # ------------------------------------------------------------------ plan

    async def plan(
        self,
        container_name: str,
        source_id: int | None,
        new_source: NewSource | None,
        template_path: str | None,
        options: ImportOptions,
    ) -> ImportPlan:
        """Work out what importing `container_name` would change, without changing anything."""
        candidate = await self._candidate_for(container_name)
        if candidate is None:
            raise NotFound(f"container {container_name}")
        info = await self._docker.inspect(candidate.container_id)
        if info is None:
            raise NotFound(f"container {container_name}")
        ctx = await self._context()

        source = await self._resolve_source(source_id, new_source)
        if template_path is None:
            templates = await self._discover(source, ctx)
            template_path = choose_template(templates, info.image, info.name)
            if template_path is None:
                raise ValidationError(f"no template in {source.owner}/{source.repo} matches {info.image}")
        token = await self._token(source.credential_id)
        discovered = await self._github.get_template(source, template_path, token)
        template = self._codec.parse(discovered.xml)
        flash = candidate.flash_template

        blockers = list(candidate.blockers)
        warnings = list(candidate.warnings)
        if info.image_digest is None:
            blockers.append("The running image has no RepoDigest (built locally), so it cannot be tracked")
        overrides = derive_overrides(template, info, flash, options.keep_host_ports)
        dropped = derive_dropped(template, info, flash)

        port_cfgs = {(c.target, _proto(c)) for c in template.configs if c.type == "Port"}
        pinned: tuple[tuple[int, Protocol_, int], ...] = ()
        if options.keep_host_ports:
            pinned = tuple(
                (b.container_port, b.protocol, b.host_port)
                for b in _unique_bindings(info.published_ports)
                if (str(b.container_port), b.protocol) in port_cfgs
            )
        else:
            warnings.append(
                "Host ports will be reallocated: the WebUI and proxy port will change after the recreate"
            )

        proxy_settings, adopt_id = self._plan_proxy(candidate, template, info, options, ctx, warnings)

        tag_override = self._tag_override(template, info, warnings)
        return ImportPlan(
            candidate=replace(candidate, blockers=tuple(blockers)),
            source_id=source.id if source_id is not None else None,
            new_source=new_source if source_id is None else None,
            template_path=template_path,
            template_commit=discovered.commit_sha,
            overrides=overrides,
            image_tag_override=tag_override,
            installed_image_digest=info.image_digest or "",
            pinned_ports=pinned,
            proxy=proxy_settings,
            adopt_proxy_host_id=adopt_id,
            options=options,
            template_diff=self._template_diff(flash, template),
            dropped=dropped,
            warnings=tuple(warnings),
        )

    async def _resolve_source(self, source_id: int | None, new_source: NewSource | None) -> Source:
        if source_id is not None:
            return await self._sources.get(source_id)
        if new_source is not None:
            return Source(
                id=0,
                owner=new_source.owner,
                repo=new_source.repo,
                ref=new_source.ref,
                credential_id=new_source.credential_id,
            )
        raise ValidationError("either source_id or new_source is required")

    def _plan_proxy(
        self,
        candidate: ImportCandidate,
        template: UnraidTemplate,
        info: ContainerInfo,
        options: ImportOptions,
        ctx: _Context,
        warnings: list[str],
    ) -> tuple[ProxySettings, int | None]:
        host = candidate.proxy_host
        subdomain = options.subdomain
        if subdomain is None and host is not None:
            subdomain = derive_subdomain(host, ctx.settings.base_domain)
        adopt_id = host.id if host is not None and options.claim_proxy_host else None
        m = _WEBUI_PORT_RE.search(template.webui)
        if host is not None and m:
            container_port = int(m.group(1))
            host_port = next(
                (b.host_port for b in info.published_ports if b.container_port == container_port), None
            )
            if host.forward_port not in (host_port, container_port):
                warnings.append(
                    f"NPM host {host.domain_names[0] if host.domain_names else host.id} forwards to port "
                    f"{host.forward_port}, which is not the WebUI port ({host_port or container_port})"
                )
        return ProxySettings(subdomain=subdomain), adopt_id

    @staticmethod
    def _tag_override(template: UnraidTemplate, info: ContainerInfo, warnings: list[str]) -> str | None:
        running_repo, running_tag = split_image(info.image)
        template_repo, template_tag = split_image(template.repository)
        if not template_matches_image(template, info.image):
            warnings.append(f"Running image {running_repo} differs from the template image {template_repo}")
        return running_tag if running_tag != template_tag else None

    def _template_diff(self, flash: UnraidTemplate | None, repo: UnraidTemplate) -> str:
        if flash is None:
            return ""

        def render(tpl: UnraidTemplate) -> list[str]:
            blanked = tuple(replace(c, value="", default="" if c.mask else c.default) for c in tpl.configs)
            return self._codec.render(replace(tpl, configs=blanked)).splitlines()

        return "\n".join(
            difflib.unified_diff(
                render(flash), render(repo), "flash template", "repo template", lineterm="", n=2
            )
        )

    # ----------------------------------------------------------------- apply

    async def apply(self, plan: ImportPlan) -> App:
        """Bring the container under UAM management; undo everything if a step up to the NPM claim fails."""
        name = plan.candidate.container_name
        current = await self._candidate_for(name)
        if current is None or current.container_id != plan.candidate.container_id:
            raise ConflictError(f"container {name} changed since the plan was made; plan again")
        blockers = current.blockers + plan.candidate.blockers
        if blockers:
            raise ConflictError(f"cannot import {name}: {'; '.join(dict.fromkeys(blockers))}")

        now = self._clock()
        undo: list[Callable[[], Awaitable[None]]] = []
        try:
            source = await self._save_source(plan, undo)
            app = await self._apps.add(
                App(
                    id=0,
                    source_id=source.id,
                    template_path=plan.template_path,
                    name=name,
                    overrides=dict(plan.overrides),
                    image_tag_override=plan.image_tag_override,
                    auto_ports=True,
                    auto_update_image=plan.options.auto_update_image,
                    auto_update_template=plan.options.auto_update_template,
                    proxy=plan.proxy,
                    state=AppState.RUNNING if plan.candidate.state == "running" else AppState.STOPPED,
                    installed_template_commit=plan.template_commit,
                    installed_image_digest=plan.installed_image_digest,
                    container_id=plan.candidate.container_id,
                    available_template_commit=None,
                    available_image_digest=None,
                    needs_attention=None,
                    last_change_at=now,
                    last_checked_at=None,
                    next_check_at=now,
                    last_error=None,
                    labels_pending=True,
                )
            )
            undo.append(lambda: self._apps.delete(app.id))
            if plan.pinned_ports:
                undo.append(lambda: self._ports.release(app.id))
                await self._ports.pin(
                    app.id, plan.pinned_ports, ignore_container_id=plan.candidate.container_id
                )
            if plan.adopt_proxy_host_id is not None and self._proxy is not None:
                await self._proxy.claim_proxy_host(plan.adopt_proxy_host_id, app.name)
        except BaseException:
            for step in reversed(undo):
                try:
                    await step()
                except Exception:
                    log.exception("import rollback step failed")
            raise

        label = f"{source.owner}/{source.repo}"
        dropped = f"; dropped {len(plan.dropped)}: " + "; ".join(plan.dropped) if plan.dropped else ""
        await self._events.record(
            app.id, "import", f"Imported {name} from {label} ({plan.template_path}){dropped}"
        )

        recreated = False
        if plan.options.recreate_now:
            try:
                app = await self._reconfigurer.reconfigure(app.id)
                recreated = True
            except Exception as e:
                message = f"Recreate after import failed: {e}"
                app = await self._apps.get(app.id)
                app.needs_attention = message
                await self._apps.save(app)
                await self._events.record(app.id, "import", message, level="error")
        if recreated and plan.options.archive_flash_template and plan.candidate.flash_template_path:
            await self._archive_flash_template(app.id, plan.candidate.flash_template_path)
        return app

    async def _save_source(self, plan: ImportPlan, undo: list[Callable[[], Awaitable[None]]]) -> Source:
        if plan.new_source is None:
            if plan.source_id is None:
                raise ValidationError("plan has neither source_id nor new_source")
            return await self._sources.get(plan.source_id)
        ns = plan.new_source
        source = await self._sources.add(
            Source(
                id=0,
                owner=ns.owner,
                repo=ns.repo,
                ref=ns.ref,
                template_path=None,
                credential_id=ns.credential_id,
            )
        )
        undo.append(lambda: self._sources.delete(source.id))
        return source

    async def _archive_flash_template(self, app_id: int, flash_path: str) -> None:
        def move() -> Path:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            src = Path(flash_path)
            dest = self._archive_dir / src.name
            if dest.exists():
                dest = self._archive_dir / f"{src.stem}.{self._clock():%Y%m%dT%H%M%S}{src.suffix}"
            shutil.move(src, dest)
            return dest

        try:
            dest = await asyncio.to_thread(move)
        except OSError as e:
            await self._events.record(
                app_id, "import", f"Could not archive flash template {flash_path}: {e}", level="warning"
            )
            return
        await self._events.record(app_id, "import", f"Archived flash template to {dest}")
