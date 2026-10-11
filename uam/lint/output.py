"""Output formatters for uam-lint."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable

from uam.lint.model import Finding

_GH_COMMAND = {"error": "error", "warning": "warning", "info": "notice"}


def summary(findings: Iterable[Finding]) -> dict[str, int]:
    """Counts per level."""
    c = Counter(f.level for f in findings)
    return {"errors": c["error"], "warnings": c["warning"], "infos": c["info"]}


def format_text(findings: list[Finding]) -> str:
    """One `file:line: level RULE message` line per finding plus a summary line."""
    lines = [
        f"{f.file}:{f.line}: {f.level} {f.rule} {f.message}"
        if f.line
        else f"{f.file}: {f.level} {f.rule} {f.message}"
        for f in findings
    ]
    s = summary(findings)
    lines.append(f"{s['errors']} error(s), {s['warnings']} warning(s), {s['infos']} info(s)")
    return "\n".join(lines) + "\n"


def format_json(findings: list[Finding]) -> str:
    """A JSON document with `findings` and `summary`."""
    doc = {
        "findings": [
            {"rule": f.rule, "level": f.level, "file": f.file, "line": f.line, "message": f.message}
            for f in findings
        ],
        "summary": summary(findings),
    }
    return json.dumps(doc, indent=2) + "\n"


def _escape(value: str, *, prop: bool = False) -> str:
    out = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        out = out.replace(":", "%3A").replace(",", "%2C")
    return out


def format_github(findings: list[Finding]) -> str:
    """GitHub Actions workflow-command annotations."""
    lines = []
    for f in findings:
        loc = f"file={_escape(f.file, prop=True)}" + (f",line={f.line}" if f.line else "")
        lines.append(f"::{_GH_COMMAND[f.level]} {loc}::{_escape(f'{f.rule} {f.message}')}\n")
    return "".join(lines)


FORMATTERS: dict[str, Callable[[list[Finding]], str]] = {
    "text": format_text,
    "json": format_json,
    "github": format_github,
}
