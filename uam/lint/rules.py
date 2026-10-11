"""Lint rules. The table in docs/app-repo-guide.md section 8 is the spec for ids and levels.

UAM001-003 (document loading) and UAM007/UAM101 (repo-wide) are emitted by `engine.py`; the
per-template rules below are registered in `RULES`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from uam.domain import TemplateConfig, UnraidTemplate
from uam.lint.extraparams import unsupported_flags
from uam.lint.imageref import parse_image_ref
from uam.lint.model import Context, Finding, Level

RuleFunc = Callable[[UnraidTemplate, Context], list[Finding]]

VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_WEBUI_PORT = re.compile(r"\[PORT:(\d+)\]")
_SECRET_WORDS = ("PASS", "TOKEN", "SECRET", "KEY")
_PINNED_TAG = re.compile(r"^[vV]?\d+\.\d+\.\d+")
_LEGACY_TAGS = ("Networking", "Data", "Environment")


@dataclass(frozen=True, slots=True)
class Rule:
    """A registered per-template rule."""

    id: str
    level: Level
    func: RuleFunc


def _configs_with_lines(t: UnraidTemplate, ctx: Context) -> list[tuple[TemplateConfig, int | None]]:
    seen: dict[tuple[str, str], int] = {}
    out: list[tuple[TemplateConfig, int | None]] = []
    for cfg in t.configs:
        key = (cfg.type, cfg.target)
        occ = seen.get(key, 0)
        seen[key] = occ + 1
        out.append((cfg, ctx.line_of_config(cfg.type, cfg.target, occ)))
    return out


def uam004_duplicate(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """Duplicate `(Type, Target)`; a Port may repeat with a different Mode (tcp plus udp)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for cfg, line in _configs_with_lines(t, ctx):
        mode = cfg.mode.lower() if cfg.type == "Port" else ""
        key = (cfg.type, cfg.target, mode)
        if key in seen:
            out.append(ctx.finding("UAM004", "error", f"duplicate config {cfg.type}:{cfg.target}", line))
        seen.add(key)
    return out


def uam005_port(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """Port Target must be 1-65535 and Mode tcp or udp."""
    out: list[Finding] = []
    for cfg, line in _configs_with_lines(t, ctx):
        if cfg.type != "Port":
            continue
        if not (cfg.target.isascii() and cfg.target.isdigit() and 1 <= int(cfg.target) <= 65535):
            out.append(ctx.finding("UAM005", "error", f"port Target {cfg.target!r} is not 1-65535", line))
        if cfg.mode.lower() not in {"tcp", "udp"}:
            out.append(
                ctx.finding("UAM005", "error", f"port {cfg.target} Mode {cfg.mode!r} is not tcp or udp", line)
            )
    return out


def uam006_webui_port(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """Every WebUI `[PORT:N]` must name a declared Port Target."""
    declared = {c.target for c in t.configs if c.type == "Port"}
    out: list[Finding] = []
    for m in _WEBUI_PORT.finditer(t.webui):
        if m.group(1) not in declared:
            out.append(
                ctx.finding(
                    "UAM006",
                    "error",
                    f"WebUI refers to [PORT:{m.group(1)}] but no Port config has Target {m.group(1)}",
                    ctx.line_of_tag("WebUI"),
                )
            )
    return out


def uam102_removed_target(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """A config present in the base version is gone."""
    if ctx.base is None:
        return []
    now = {(c.type, c.target) for c in t.configs}
    return [
        ctx.finding(
            "UAM102",
            "warning",
            f"config {cfg.type}:{cfg.target} was removed or renamed; user values will be lost",
        )
        for cfg in ctx.base.configs
        if (cfg.type, cfg.target) not in now
    ]


def uam103_required_added(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """A required config without a default was added."""
    if ctx.base is None:
        return []
    before = {(c.type, c.target) for c in ctx.base.configs}
    out: list[Finding] = []
    for cfg, line in _configs_with_lines(t, ctx):
        if (cfg.type, cfg.target) not in before and cfg.required and not cfg.default.strip():
            out.append(
                ctx.finding(
                    "UAM103",
                    "warning",
                    f"required config {cfg.type}:{cfg.target} was added without a default; "
                    "this blocks auto-update",
                    line,
                )
            )
    return out


def _image_name(repository: str) -> str:
    ref = parse_image_ref(repository)
    return ref.name if ref else repository


def uam104_identity_changed(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """`Name` or `Repository` (ignoring the tag) differs from the base version."""
    if ctx.base is None:
        return []
    out: list[Finding] = []
    if t.name != ctx.base.name:
        out.append(
            ctx.finding(
                "UAM104",
                "warning",
                f"Name changed from {ctx.base.name!r} to {t.name!r}",
                ctx.line_of_tag("Name"),
            )
        )
    if _image_name(t.repository) != _image_name(ctx.base.repository):
        out.append(
            ctx.finding(
                "UAM104",
                "warning",
                f"Repository changed from {ctx.base.repository!r} to {t.repository!r}",
                ctx.line_of_tag("Repository"),
            )
        )
    return out


def uam201_legacy(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """Legacy `<Networking>`/`<Data>`/`<Environment>` blocks are present."""
    for tag in _LEGACY_TAGS:
        if ctx.root.find(tag) is not None:
            return [
                ctx.finding(
                    "UAM201", "warning", "legacy template format; use <Config> entries", ctx.line_of_tag(tag)
                )
            ]
    return []


def uam202_extra_params(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """ExtraParams contains flags UAM ignores."""
    bad = unsupported_flags(t.extra_params)
    if not bad:
        return []
    return [
        ctx.finding(
            "UAM202",
            "warning",
            f"ExtraParams has unsupported flags (ignored by UAM): {', '.join(bad)}",
            ctx.line_of_tag("ExtraParams"),
        )
    ]


def uam203_unmasked_secret(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """A Variable whose name looks like a secret is not masked."""
    out: list[Finding] = []
    for cfg, line in _configs_with_lines(t, ctx):
        if cfg.type != "Variable" or cfg.mask:
            continue
        hay = f"{cfg.target} {cfg.name}".upper()
        if any(w in hay for w in _SECRET_WORDS):
            out.append(
                ctx.finding(
                    "UAM203", "warning", f'Variable {cfg.target} looks like a secret; set Mask="true"', line
                )
            )
    return out


def uam204_required_no_default(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """A required Variable or Path has an empty default."""
    return [
        ctx.finding("UAM204", "warning", f"required {cfg.type} {cfg.target} has no default", line)
        for cfg, line in _configs_with_lines(t, ctx)
        if cfg.type in {"Variable", "Path"} and cfg.required and not cfg.default.strip()
    ]


def uam205_host_network(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """`Network` is host, so ports are neither mapped nor proxied."""
    if t.network.strip().lower() != "host":
        return []
    return [
        ctx.finding(
            "UAM205",
            "warning",
            "Network is host; ports won't be mapped or proxied",
            ctx.line_of_tag("Network"),
        )
    ]


def uam206_healthcheck(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """The Dockerfile next to the template (or at the repo root) has no HEALTHCHECK."""
    here = (ctx.repo_root / ctx.path).parent
    for directory in (here, ctx.repo_root):
        dockerfile = directory / "Dockerfile"
        if dockerfile.is_file():
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^\s*HEALTHCHECK\s+(?!NONE\b)", text, re.MULTILINE | re.IGNORECASE):
                return []
            rel = dockerfile.relative_to(ctx.repo_root).as_posix()
            return [ctx.finding("UAM206", "warning", f"{rel} has no HEALTHCHECK")]
    return []


def uam301_pinned_tag(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """The image tag is a full version or a digest, so there are no digest-based updates."""
    ref = parse_image_ref(t.repository)
    if ref is None:
        return []
    if ref.digest or (ref.tag and _PINNED_TAG.match(ref.tag)):
        return [
            ctx.finding(
                "UAM301",
                "info",
                f"image tag is pinned ({t.repository}); UAM only updates when the template changes",
                ctx.line_of_tag("Repository"),
            )
        ]
    return []


def uam302_registry(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """The image registry is neither ghcr.io nor docker.io."""
    ref = parse_image_ref(t.repository)
    if ref is None or ref.registry in {"ghcr.io", "docker.io"}:
        return []
    return [
        ctx.finding(
            "UAM302",
            "info",
            f"image registry {ref.registry} is not ghcr.io or docker.io",
            ctx.line_of_tag("Repository"),
        )
    ]


def uam303_metadata(t: UnraidTemplate, ctx: Context) -> list[Finding]:
    """`Icon`, `WebUI` or `Project` is missing."""
    return [
        ctx.finding("UAM303", "info", f"{tag} is missing", ctx.line_of_tag(tag))
        for tag, value in (("Icon", t.icon), ("WebUI", t.webui), ("Project", t.project))
        if not value.strip()
    ]


RULES: tuple[Rule, ...] = (
    Rule("UAM004", "error", uam004_duplicate),
    Rule("UAM005", "error", uam005_port),
    Rule("UAM006", "error", uam006_webui_port),
    Rule("UAM102", "warning", uam102_removed_target),
    Rule("UAM103", "warning", uam103_required_added),
    Rule("UAM104", "warning", uam104_identity_changed),
    Rule("UAM201", "warning", uam201_legacy),
    Rule("UAM202", "warning", uam202_extra_params),
    Rule("UAM203", "warning", uam203_unmasked_secret),
    Rule("UAM204", "warning", uam204_required_no_default),
    Rule("UAM205", "warning", uam205_host_network),
    Rule("UAM206", "warning", uam206_healthcheck),
    Rule("UAM301", "info", uam301_pinned_tag),
    Rule("UAM302", "info", uam302_registry),
    Rule("UAM303", "info", uam303_metadata),
)
