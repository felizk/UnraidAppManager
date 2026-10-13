"""Lint driver: discovery, document loading (UAM001-003, UAM007), base comparison and rule runs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat

from defusedxml import ElementTree as DefusedET

from uam.domain import UnraidTemplate
from uam.errors import TemplateParseError
from uam.lint import gitbase
from uam.lint.imageref import parse_image_ref
from uam.lint.model import Context, ElementPos, Finding
from uam.lint.rules import RULES, VALID_NAME
from uam.unraid_template.codec import XmlTemplateCodec

TEMPLATE_DIRS = frozenset({"unraid", ".unraid", "templates"})
_SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".ruff_cache", "target"}
    # Test fixtures, docs and agent worktrees often hold example templates that are not app templates.
    | {".claude", "tests", "test", "fixtures", "docs", "examples"}
)


def is_template_path(path: str) -> bool:
    """Same discovery rule as `uam.clients.github._is_template_path` (kept standalone on purpose)."""
    if not path.endswith(".xml"):
        return False
    parts = path.split("/")
    return len(parts) == 1 or (len(parts) == 2 and parts[0] in TEMPLATE_DIRS)


def scan_elements(xml: str) -> list[ElementPos]:
    """Collect start elements with line numbers. Only call on XML that already parsed safely."""
    found: list[ElementPos] = []
    parser = expat.ParserCreate()

    def start(name: str, attrs: dict[str, str]) -> None:
        found.append(ElementPos(name, dict(attrs), parser.CurrentLineNumber))

    parser.StartElementHandler = start
    try:
        parser.Parse(xml.encode("utf-8"), True)
    except expat.ExpatError:
        return found
    return found


@dataclass(slots=True)
class LoadedDoc:
    """Result of loading one candidate template file."""

    path: str
    findings: list[Finding]
    template: UnraidTemplate | None = None
    ctx: Context | None = None
    is_container: bool = True


def _text(root: ET.Element, tag: str) -> str:
    el = root.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _error(path: str, rule: str, message: str, line: int | None = None) -> Finding:
    return Finding(rule, "error", path, message, line)


def load_document(path: str, xml: str, repo_root: Path, *, in_template_dir: bool) -> LoadedDoc:
    """Parse a candidate file, reporting UAM001-003. Non-Container XML at the repo root is skipped."""
    try:
        root = DefusedET.fromstring(xml.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - defusedxml has its own error types for DTDs and entities
        if not in_template_dir and "<Container" not in xml:
            return LoadedDoc(path, [], is_container=False)
        line = exc.position[0] if isinstance(exc, ET.ParseError) else None
        return LoadedDoc(path, [_error(path, "UAM001", f"malformed XML: {exc}", line)])
    if root.tag != "Container":
        if in_template_dir:
            msg = f"root element is <{root.tag}>, expected <Container>"
            return LoadedDoc(path, [_error(path, "UAM001", msg, 1)])
        return LoadedDoc(path, [], is_container=False)

    elements = scan_elements(xml)

    def line_of(tag: str) -> int | None:
        return next((e.line for e in elements if e.tag == tag), None)

    findings: list[Finding] = []
    name = _text(root, "Name")
    if not name:
        findings.append(_error(path, "UAM002", "Name is missing", line_of("Name") or 1))
    elif not VALID_NAME.match(name):
        findings.append(
            _error(path, "UAM002", f"Name {name!r} is not a valid container name", line_of("Name"))
        )
    repo = _text(root, "Repository")
    if not repo:
        findings.append(_error(path, "UAM003", "Repository is missing", line_of("Repository") or 1))
    elif parse_image_ref(repo) is None:
        msg = f"Repository {repo!r} is not a valid image reference"
        findings.append(_error(path, "UAM003", msg, line_of("Repository")))

    source = xml
    if not name or not repo:
        for tag, placeholder in (("Name", "placeholder"), ("Repository", "placeholder/placeholder")):
            if not _text(root, tag):
                el = root.find(tag)
                if el is None:
                    el = ET.SubElement(root, tag)
                el.text = placeholder
        source = ET.tostring(root, encoding="unicode")
    try:
        template = XmlTemplateCodec(strict=True).parse(source)
    except TemplateParseError as exc:
        findings.append(_error(path, "UAM001", str(exc)))
        return LoadedDoc(path, findings)
    ctx = Context(path=path, repo_root=repo_root, xml=xml, root=root, elements=elements)
    return LoadedDoc(path, findings, template, ctx)


def _read(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def find_xml_files(root: Path) -> list[str]:
    """All `*.xml` files under `root` as sorted posix relative paths."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        out.extend(
            (Path(dirpath) / fn).relative_to(root).as_posix() for fn in filenames if fn.endswith(".xml")
        )
    return sorted(out)


def _looks_like_container(xml: str) -> bool:
    try:
        return bool(DefusedET.fromstring(xml.encode("utf-8")).tag == "Container")
    except Exception:  # noqa: BLE001 - unreadable files simply aren't templates
        return False


def _load_base(root: Path, ref: str) -> dict[str, UnraidTemplate | None]:
    """Templates at `ref`; the value is None when the base file exists but doesn't parse."""
    templates: dict[str, UnraidTemplate | None] = {}
    for path in gitbase.list_files(root, ref):
        if not is_template_path(path):
            continue
        try:
            xml = gitbase.show(root, ref, path)
        except gitbase.GitError:
            continue
        doc = load_document(path, xml, root, in_template_dir="/" in path)
        if doc.is_container:
            templates[path] = doc.template
    return templates


def lint_repo(root: Path, base: str | None = None) -> tuple[list[Finding], int]:
    """Lint every template under `root`. Returns the findings and the number of templates checked."""
    base_templates = _load_base(root, base) if base else None
    findings: list[Finding] = []
    current: set[str] = set()
    checked = 0
    for rel in find_xml_files(root):
        xml = _read(root, rel)
        if xml is None:
            if is_template_path(rel):
                findings.append(_error(rel, "UAM001", "file is not readable UTF-8 text"))
            continue
        if not is_template_path(rel):
            if _looks_like_container(xml):
                msg = (
                    "template is outside the discovery locations "
                    "(repo root, unraid/, .unraid/ or templates/); UAM will not find it"
                )
                findings.append(_error(rel, "UAM007", msg, 1))
            continue
        doc = load_document(rel, xml, root, in_template_dir="/" in rel)
        if not doc.is_container:
            continue
        checked += 1
        current.add(rel)
        findings.extend(doc.findings)
        if doc.template is None or doc.ctx is None:
            continue
        if base_templates is not None:
            doc.ctx.base = base_templates.get(rel)
        for rule in RULES:
            findings.extend(rule.func(doc.template, doc.ctx))
    if base_templates is not None:
        for path in sorted(set(base_templates) - current):
            msg = f"template that exists at {base} was moved or deleted; UAM will lose track of the app"
            findings.append(_error(path, "UAM101", msg))
    findings.sort(key=lambda f: (f.file, f.line or 0, f.rule, f.message))
    return findings, checked
