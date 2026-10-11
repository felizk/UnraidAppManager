"""Shared types for uam-lint: findings and the per-template lint context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

from uam.domain import UnraidTemplate

Level = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation, located by repo-relative path and (when known) line."""

    rule: str
    level: Level
    file: str
    message: str
    line: int | None = None


@dataclass(frozen=True, slots=True)
class ElementPos:
    """A start tag seen by the line-tracking scan."""

    tag: str
    attrs: dict[str, str]
    line: int


@dataclass(slots=True)
class Context:
    """Everything a rule needs besides the parsed template."""

    path: str
    repo_root: Path
    xml: str
    root: ET.Element
    elements: list[ElementPos] = field(default_factory=list)
    base: UnraidTemplate | None = None

    def line_of_tag(self, tag: str) -> int | None:
        """Line of the first `<tag>` start element."""
        for el in self.elements:
            if el.tag == tag:
                return el.line
        return None

    def line_of_config(self, ctype: str, target: str, occurrence: int = 0) -> int | None:
        """Line of the n-th `<Config>` with the given Type and Target."""
        seen = 0
        for el in self.elements:
            if el.tag == "Config" and el.attrs.get("Type") == ctype and el.attrs.get("Target") == target:
                if seen == occurrence:
                    return el.line
                seen += 1
        return None

    def finding(self, rule: str, level: Level, message: str, line: int | None = None) -> Finding:
        """Build a finding for this template."""
        return Finding(rule, level, self.path, message, line)
