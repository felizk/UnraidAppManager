"""Read a base git ref through the git CLI (no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    """git is unavailable, the directory isn't a repository, or the ref doesn't resolve."""


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=root,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return proc.stdout.decode("utf-8", "replace")


def list_files(root: Path, ref: str) -> list[str]:
    """Paths (relative to `root`) in `ref`'s tree."""
    out = _git(root, "ls-tree", "-r", "--name-only", ref)
    return [line for line in out.splitlines() if line]


def show(root: Path, ref: str, path: str) -> str:
    """Contents of `path` (relative to `root`) at `ref`."""
    return _git(root, "show", f"{ref}:./{path}")
