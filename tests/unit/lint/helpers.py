"""Builders for lint tests."""

from __future__ import annotations

from pathlib import Path

from uam.lint.engine import lint_repo
from uam.lint.model import Finding

PORT = '<Config Name="Web" Target="8080" Default="8080" Mode="tcp" Type="Port" Required="true">8080</Config>'
PATH_CFG = (
    '<Config Name="Data" Target="/config" Default="/mnt/user/appdata/x" Mode="rw" Type="Path">'
    "/mnt/user/appdata/x</Config>"
)


def template(
    *,
    name: str | None = "myapp",
    repository: str | None = "ghcr.io/owner/myapp:latest",
    network: str = "bridge",
    webui: str | None = "http://[IP]:[PORT:8080]/",
    icon: str | None = "https://example.com/i.png",
    project: str | None = "https://github.com/owner/myapp",
    extra_params: str = "",
    configs: tuple[str, ...] = (PORT, PATH_CFG),
    legacy: str = "",
) -> str:
    """A valid template by default; override pieces to trigger a rule."""
    parts = ['<?xml version="1.0"?>', '<Container version="2">']
    for tag, value in (
        ("Name", name),
        ("Repository", repository),
        ("Project", project),
        ("WebUI", webui),
        ("Icon", icon),
    ):
        if value is not None:
            parts.append(f"  <{tag}>{value}</{tag}>")
    parts.append(f"  <Network>{network}</Network>")
    parts.append(f"  <ExtraParams>{extra_params}</ExtraParams>")
    parts.extend(f"  {c}" for c in configs)
    if legacy:
        parts.append(legacy)
    parts.append("</Container>")
    return "\n".join(parts) + "\n"


def cfg(
    ctype: str,
    target: str,
    *,
    default: str = "x",
    mode: str = "",
    required: str = "false",
    mask: str = "false",
    name: str = "n",
) -> str:
    """One `<Config>` element."""
    return (
        f'<Config Name="{name}" Target="{target}" Default="{default}" Mode="{mode}" '
        f'Type="{ctype}" Required="{required}" Mask="{mask}">{default}</Config>'
    )


def write(root: Path, rel: str, text: str) -> None:
    """Write a file under `root`, creating directories."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def lint_one(tmp_path: Path, xml: str, rel: str = "unraid/app.xml") -> list[Finding]:
    """Lint a repo holding a single template."""
    write(tmp_path, rel, xml)
    return lint_repo(tmp_path)[0]


def ids(findings: list[Finding]) -> list[str]:
    """Rule ids of the findings."""
    return [f.rule for f in findings]
