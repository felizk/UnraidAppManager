"""Write Unraid user templates to the flash drive, on demand, with a preview step first.

Approach: the template is fetched with `GitHubClient.get_template` using a copy of the app's
`Source` whose `ref` is the app's `installed_template_commit`, so the exact installed revision is
exported. Port values come from the resolved container spec (actual host ports); other configs use
the app's override (`"<Type>:<Target>"`, ports `"Port:<Target>/<mode>"`) or the template value.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from uam.contracts import (
    AppRepository,
    GitHubClient,
    SettingsStore,
    SourceRepository,
    SpecResolver,
    TemplateCodec,
    Vault,
)
from uam.domain import App, TemplateConfig, UnraidTemplate
from uam.errors import ConflictError, ValidationError

DEFAULT_BACKUP_DIR = "/data/template-backups"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ExportPlan:
    """What exporting one app would do. `old_xml` is the file content when the plan was made."""

    app_id: int
    path: str
    new_xml: str
    old_xml: str | None
    diff: str
    changed: bool


def safe_file_name(name: str) -> str:
    """Return `my-<name>.xml` with path separators and other unsafe characters replaced."""
    cleaned = _UNSAFE.sub("_", name).strip("._") or "app"
    return f"my-{cleaned}.xml"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _writable_dir(d: Path) -> bool:
    return d.is_dir() and os.access(d, os.W_OK)


def _backup(backup: Path, path: Path, content: str) -> None:
    backup.mkdir(parents=True, exist_ok=True)
    (backup / path.name).write_text(content, encoding="utf-8")


class TemplateExporter:
    """Plans and applies template exports to the flash drive."""

    def __init__(
        self,
        apps: AppRepository,
        sources: SourceRepository,
        github: GitHubClient,
        codec: TemplateCodec,
        spec_resolver: SpecResolver,
        vault: Vault,
        settings: SettingsStore,
        backup_dir: str | Path = DEFAULT_BACKUP_DIR,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._apps = apps
        self._sources = sources
        self._github = github
        self._codec = codec
        self._resolver = spec_resolver
        self._vault = vault
        self._settings = settings
        self._backup_dir = Path(backup_dir)
        self._clock = clock

    async def plan(self, app_ids: list[int] | None, include_secrets: bool | None = None) -> list[ExportPlan]:
        """Preview the export. `None` means every installed app (one with an installed template commit).

        `include_secrets=None` uses `Settings.template_export_include_secrets`.
        """
        export_dir = await self._export_dir()
        if app_ids is None:
            apps = [a for a in await self._apps.list() if a.installed_template_commit]
        else:
            apps = [await self._apps.get(i) for i in app_ids]
            for a in apps:
                if not a.installed_template_commit:
                    raise ValidationError(f"{a.name} has no installed template commit to export")
        include = (
            include_secrets
            if include_secrets is not None
            else (await self._settings.get()).template_export_include_secrets
        )
        plans = []
        for app in apps:
            xml = await self._build(app, include)
            path = export_dir / safe_file_name(app.name)
            old = await asyncio.to_thread(_read, path)
            plans.append(self._make_plan(app.id, path, xml, old))
        return plans

    async def apply(self, plans: list[ExportPlan], include_secrets: bool | None = None) -> list[str]:
        """Write the changed plans and return the written paths.

        With `include_secrets` set, the content is rebuilt with that choice (a plan's `new_xml`
        uses the stored setting). Raises `ConflictError` if a file changed since its plan was made.
        """
        export_dir = await self._export_dir()
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        written: list[str] = []
        for p in plans:
            if not p.changed:
                continue
            path = Path(p.path)
            current = await asyncio.to_thread(_read, path)
            if current != p.old_xml:
                raise ConflictError(f"{path.name} changed since the plan was made; preview again")
            xml = p.new_xml
            if include_secrets is not None:
                xml = await self._build(await self._apps.get(p.app_id), include_secrets)
            try:
                if current is not None:
                    await asyncio.to_thread(_backup, self._backup_dir / stamp, path, current)
                await asyncio.to_thread(_write_atomic, path, xml)
            except OSError as e:
                raise ValidationError(self._dir_hint(export_dir, str(e))) from e
            written.append(str(path))
        return written

    @staticmethod
    def _dir_hint(export_dir: Path, why: str) -> str:
        return (
            f"Cannot write to template directory {export_dir} ({why}). "
            "Check that the Unraid flash path (/boot/config/plugins/dockerMan/templates-user) "
            f"is mapped read-write into the container at {export_dir}."
        )

    async def _export_dir(self) -> Path:
        d = Path((await self._settings.get()).template_export_dir)
        if not await asyncio.to_thread(_writable_dir, d):
            raise ValidationError(self._dir_hint(d, "missing or read-only"))
        return d

    @staticmethod
    def _make_plan(app_id: int, path: Path, xml: str, old: str | None) -> ExportPlan:
        diff = "".join(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                xml.splitlines(keepends=True),
                fromfile=str(path) if old is not None else "/dev/null",
                tofile=str(path),
            )
        )
        return ExportPlan(app_id, str(path), xml, old, diff, changed=old != xml)

    async def _build(self, app: App, include_secrets: bool) -> str:
        source = await self._sources.get(app.source_id)
        token = await self._vault.reveal(source.credential_id) if source.credential_id is not None else None
        at_commit = replace(source, ref=app.installed_template_commit or source.ref)
        found = await self._github.get_template(at_commit, app.template_path, token)
        template = self._codec.parse(found.xml)
        resolved = await self._resolver.resolve(app, template)
        host_ports = {(b.container_port, b.protocol): b.host_port for b in resolved.spec.ports}

        def value(c: TemplateConfig) -> str:
            if c.mask and not include_secrets:
                return ""
            if c.type == "Port":
                key = (int(c.target), "udp" if c.mode == "udp" else "tcp") if c.target.isdigit() else None
                if key is not None and key in host_ports:
                    return str(host_ports[key])
                return app.overrides.get(f"Port:{c.target}/{c.mode or 'tcp'}", c.value)
            return app.overrides.get(f"{c.type}:{c.target}", c.value)

        exported: UnraidTemplate = replace(
            template,
            name=app.name,
            configs=tuple(replace(c, value=value(c)) for c in template.configs),
        )
        return self._codec.render(exported)
