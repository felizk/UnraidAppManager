"""`uam-lint` command line entry point."""

from __future__ import annotations

import argparse
import posixpath
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from uam.lint import gitbase
from uam.lint.engine import lint_repo
from uam.lint.output import FORMATTERS


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uam-lint", description="Check an app repository against the UAM app-repo guide rules."
    )
    p.add_argument("path", nargs="?", default=".", metavar="PATH", help="repository root (default: .)")
    p.add_argument("--base", metavar="GIT_REF", help="compare against this ref to find breaking changes")
    p.add_argument("--format", choices=sorted(FORMATTERS), default="text", dest="fmt")
    p.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run the linter. Exit 0 when clean, 1 on errors (or warnings with --strict), 2 on usage errors."""
    args = _parser().parse_args(argv)
    root = Path(args.path)
    if not root.is_dir():
        print(f"uam-lint: {args.path}: not a directory", file=sys.stderr)
        return 2
    try:
        findings, checked = lint_repo(root, args.base)
    except gitbase.GitError as exc:
        print(f"uam-lint: cannot read base {args.base!r}: {exc}", file=sys.stderr)
        return 2
    if posixpath.normpath(args.path) != ".":
        findings = [replace(f, file=posixpath.normpath(posixpath.join(args.path, f.file))) for f in findings]
    sys.stdout.write(FORMATTERS[args.fmt](findings))
    if checked == 0 and not findings:
        print("uam-lint: no templates found", file=sys.stderr)
    failing = {"error", "warning"} if args.strict else {"error"}
    return 1 if any(f.level in failing for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
