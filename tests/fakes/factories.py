"""Builders for domain objects with sensible defaults."""

from __future__ import annotations

from typing import Any

from uam.domain import App, AppState, ProxySettings, Source


def make_app(**fields: Any) -> App:
    """An `App` with defaults; pass any field to override it."""
    defaults: dict[str, Any] = {
        "id": 0,
        "source_id": 1,
        "template_path": "unraid/notes.xml",
        "name": "notes",
        "overrides": {},
        "image_tag_override": None,
        "auto_ports": True,
        "auto_update_image": True,
        "auto_update_template": False,
        "proxy": ProxySettings(),
        "state": AppState.NOT_INSTALLED,
        "installed_template_commit": None,
        "installed_image_digest": None,
        "container_id": None,
        "available_template_commit": None,
        "available_image_digest": None,
        "needs_attention": None,
        "last_change_at": None,
        "last_checked_at": None,
        "next_check_at": None,
        "last_error": None,
        "labels_pending": False,
    }
    defaults.update(fields)
    return App(**defaults)


def make_source(**fields: Any) -> Source:
    """A `Source` with defaults; pass any field to override it."""
    defaults: dict[str, Any] = {"id": 0, "owner": "felizk", "repo": "notes", "ref": "main"}
    defaults.update(fields)
    return Source(**defaults)
