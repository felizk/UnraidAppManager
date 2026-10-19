"""User-editable settings, persisted as a single JSON row."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class Settings:
    port_range_start: int = 30000
    port_range_end: int = 30999
    reserved_ports: list[int] = field(default_factory=list)
    host_ip: str | None = None
    appdata_root: str = "/mnt/user/appdata"
    npm_url: str | None = None
    npm_credential_id: int | None = None
    base_domain: str | None = None
    default_certificate_id: int | None = None
    hot_window_hours: int = 168
    hot_interval_minutes: int = 5
    cold_interval_hours: int = 24
    template_export_dir: str = "/unraid-templates"
    template_export_include_secrets: bool = True
    unraid_managed_label: str = "uam"
    pushover_credential_id: int | None = None
    notify_on_update: bool = True
    notify_on_crash: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build from a dict, ignoring unknown keys so old rows survive upgrades."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
