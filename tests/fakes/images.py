"""Tiny image-reference helpers shared by the fakes (the real parsing lives in T02/T04)."""

from __future__ import annotations


def image_host(image_ref: str) -> str:
    """Registry host of an image reference (`docker.io` when none is given)."""
    first, _, rest = image_ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def strip_tag(image_ref: str) -> str:
    """Image reference without tag or digest."""
    ref = image_ref.split("@", 1)[0]
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    return ref[:colon] if colon > slash else ref


def with_tag(image_ref: str, tag: str) -> str:
    """Replace (or add) the tag."""
    return f"{strip_tag(image_ref)}:{tag}"
