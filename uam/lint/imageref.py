"""Minimal Docker image reference parsing for lint rules."""

from __future__ import annotations

import re
from dataclasses import dataclass

_COMPONENT = re.compile(r"^[A-Za-z0-9]+(?:(?:[._]|__|-+)[A-Za-z0-9]+)*$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d+)?$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[+._-][A-Za-z][A-Za-z0-9]*)*:[0-9a-fA-F]{32,}$")
_DOCKER_HUB_HOSTS = {"index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}


@dataclass(frozen=True, slots=True)
class ImageRef:
    """A parsed image reference: registry host, repository path, tag and digest."""

    registry: str
    path: str
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        """Registry plus path, without tag or digest."""
        return f"{self.registry}/{self.path}"


def parse_image_ref(ref: str) -> ImageRef | None:
    """Parse an image reference, returning None when it is not valid.

    Upper case in path components is tolerated (templates often carry an `OWNER` placeholder).
    """
    if not ref or any(c.isspace() for c in ref):
        return None
    rest = ref
    digest: str | None = None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
        if not _DIGEST.match(digest):
            return None
    tag: str | None = None
    if ":" in rest.rsplit("/", 1)[-1]:
        rest, tag = rest.rsplit(":", 1)
        if not _TAG.match(tag):
            return None
    parts = rest.split("/")
    registry = "docker.io"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        if not _HOST.match(parts[0]):
            return None
        registry = parts[0].lower()
        parts = parts[1:]
    if not all(_COMPONENT.match(p) for p in parts):
        return None
    if registry in _DOCKER_HUB_HOSTS:
        registry = "docker.io"
    return ImageRef(registry, "/".join(parts), tag, digest)
