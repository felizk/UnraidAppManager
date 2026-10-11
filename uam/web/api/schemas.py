"""Pydantic v2 request and response models for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from uam.domain import (
    App,
    AppState,
    Credential,
    CredentialKind,
    ImportOptions,
    NewSource,
    PortAllocation,
    ProxySettings,
    Source,
    TemplateConfig,
    UnraidTemplate,
)
from uam.settings import Settings
from uam.web.deps import ExportPlan


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class AuthStatusOut(BaseModel):
    setup_required: bool
    authenticated: bool


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class OkOut(BaseModel):
    ok: bool = True


class AcceptedOut(BaseModel):
    app_id: int


# ---------------------------------------------------------------- settings
class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port_range_start: int = Field(ge=1, le=65535)
    port_range_end: int = Field(ge=1, le=65535)
    reserved_ports: list[int] = Field(default_factory=list)
    host_ip: str | None = None
    appdata_root: str = Field(min_length=1)
    npm_url: str | None = None
    npm_credential_id: int | None = None
    base_domain: str | None = None
    default_certificate_id: int | None = None
    hot_window_hours: int = Field(ge=1)
    hot_interval_minutes: int = Field(ge=1)
    cold_interval_hours: int = Field(ge=1)
    template_export_dir: str = Field(min_length=1)
    template_export_include_secrets: bool = True
    unraid_managed_label: str = Field(min_length=1)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.port_range_end < self.port_range_start:
            raise ValueError("port_range_end must not be below port_range_start")
        return self

    @classmethod
    def from_settings(cls, s: Settings) -> SettingsModel:
        """Build from the persisted dataclass."""
        return cls(**s.to_dict())

    def to_settings(self) -> Settings:
        """Convert to the persisted dataclass."""
        return Settings.from_dict(self.model_dump())


# ---------------------------------------------------------------- credentials
class CredentialIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: CredentialKind
    username: str = ""
    secret: str = Field(min_length=1, repr=False)
    registry_host: str = ""


class CredentialOut(BaseModel):
    """A credential without its secret."""

    id: int
    name: str
    kind: CredentialKind
    username: str
    registry_host: str

    @classmethod
    def from_domain(cls, c: Credential) -> CredentialOut:
        """Build from the domain object (which never holds the secret)."""
        return cls(id=c.id, name=c.name, kind=c.kind, username=c.username, registry_host=c.registry_host)


class CredentialTestOut(BaseModel):
    ok: bool
    tested: bool = True
    detail: str = ""
    scopes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- sources / templates
class SourceIn(BaseModel):
    owner: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    ref: str = "main"
    template_path: str | None = None
    credential_id: int | None = None


class SourceOut(BaseModel):
    id: int
    owner: str
    repo: str
    ref: str
    template_path: str | None
    credential_id: int | None

    @classmethod
    def from_domain(cls, s: Source) -> SourceOut:
        """Build from the domain object."""
        return cls(
            id=s.id,
            owner=s.owner,
            repo=s.repo,
            ref=s.ref,
            template_path=s.template_path,
            credential_id=s.credential_id,
        )


class DiscoveredTemplateOut(BaseModel):
    path: str
    sha: str
    commit_sha: str
    committed_at: datetime
    name: str | None = None
    repository: str | None = None
    icon: str | None = None
    parse_error: str | None = None


class TemplateConfigOut(BaseModel):
    name: str
    target: str
    default: str
    value: str
    type: str
    mode: str
    description: str
    display: str
    required: bool
    mask: bool

    @classmethod
    def from_domain(cls, c: TemplateConfig) -> TemplateConfigOut:
        """Build from the parsed config."""
        return cls(
            name=c.name,
            target=c.target,
            default=c.default,
            value=c.value,
            type=c.type,
            mode=c.mode,
            description=c.description,
            display=c.display,
            required=c.required,
            mask=c.mask,
        )


class TemplatePreviewOut(BaseModel):
    path: str
    commit_sha: str
    name: str
    repository: str
    network: str
    privileged: bool
    webui: str
    overview: str
    category: str
    icon: str
    support: str
    project: str
    extra_params: str
    configs: list[TemplateConfigOut]

    @classmethod
    def from_template(cls, path: str, commit_sha: str, t: UnraidTemplate) -> TemplatePreviewOut:
        """Build the add-app form data from a parsed template."""
        return cls(
            path=path,
            commit_sha=commit_sha,
            name=t.name,
            repository=t.repository,
            network=t.network,
            privileged=t.privileged,
            webui=t.webui,
            overview=t.overview,
            category=t.category,
            icon=t.icon,
            support=t.support,
            project=t.project,
            extra_params=t.extra_params,
            configs=[TemplateConfigOut.from_domain(c) for c in t.configs],
        )


# ---------------------------------------------------------------- apps
class ProxyIn(BaseModel):
    subdomain: str | None = None
    forward_mode: Literal["host_port", "container_network"] = "host_port"
    certificate_id: int | None = None
    ssl_forced: bool = True
    websockets: bool = True
    access_list_id: int | None = None

    def to_domain(self) -> ProxySettings:
        """Convert to the domain object."""
        return ProxySettings(**self.model_dump())

    @classmethod
    def from_domain(cls, p: ProxySettings) -> ProxyIn:
        """Build from the domain object."""
        return cls(
            subdomain=p.subdomain,
            forward_mode=p.forward_mode,
            certificate_id=p.certificate_id,
            ssl_forced=p.ssl_forced,
            websockets=p.websockets,
            access_list_id=p.access_list_id,
        )


class AppCreate(BaseModel):
    source_id: int
    template_path: str = Field(min_length=1)
    name: str | None = None
    overrides: dict[str, str] = Field(default_factory=dict)
    proxy: ProxyIn = Field(default_factory=ProxyIn)
    auto_ports: bool = True
    install: bool = True


class AppPatch(BaseModel):
    overrides: dict[str, str] | None = None
    proxy: ProxyIn | None = None
    image_tag_override: str | None = None
    auto_ports: bool | None = None
    auto_update_image: bool | None = None
    auto_update_template: bool | None = None


class AppUpdateIn(BaseModel):
    apply_template: bool = True
    apply_image: bool = True


class AppOut(BaseModel):
    id: int
    source_id: int
    template_path: str
    name: str
    overrides: dict[str, str]
    image_tag_override: str | None
    auto_ports: bool
    auto_update_image: bool
    auto_update_template: bool
    proxy: ProxyIn
    state: AppState
    installed_template_commit: str | None
    installed_image_digest: str | None
    container_id: str | None
    available_template_commit: str | None
    available_image_digest: str | None
    needs_attention: str | None
    last_change_at: datetime | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    last_error: str | None
    labels_pending: bool

    @classmethod
    def from_domain(cls, a: App) -> AppOut:
        """Build from the domain object."""
        return cls(
            id=a.id,
            source_id=a.source_id,
            template_path=a.template_path,
            name=a.name,
            overrides=dict(a.overrides),
            image_tag_override=a.image_tag_override,
            auto_ports=a.auto_ports,
            auto_update_image=a.auto_update_image,
            auto_update_template=a.auto_update_template,
            proxy=ProxyIn.from_domain(a.proxy),
            state=a.state,
            installed_template_commit=a.installed_template_commit,
            installed_image_digest=a.installed_image_digest,
            container_id=a.container_id,
            available_template_commit=a.available_template_commit,
            available_image_digest=a.available_image_digest,
            needs_attention=a.needs_attention,
            last_change_at=a.last_change_at,
            last_checked_at=a.last_checked_at,
            next_check_at=a.next_check_at,
            last_error=a.last_error,
            labels_pending=a.labels_pending,
        )


class UpdateCheckOut(BaseModel):
    app_id: int
    template_changed: bool
    new_template_commit: str | None
    image_changed: bool
    new_image_digest: str | None
    needs_attention: str | None
    checked_at: datetime


class LogsOut(BaseModel):
    app_id: int
    tail: int
    logs: str


# ---------------------------------------------------------------- ports / npm
class PortAllocationOut(BaseModel):
    app_id: int
    container_port: int
    protocol: str
    host_port: int

    @classmethod
    def from_domain(cls, p: PortAllocation) -> PortAllocationOut:
        """Build from the domain object."""
        return cls(
            app_id=p.app_id, container_port=p.container_port, protocol=p.protocol, host_port=p.host_port
        )


class PortInUseOut(BaseModel):
    host_port: int
    protocol: str


class PortsOut(BaseModel):
    range_start: int
    range_end: int
    reserved: list[int]
    allocations: list[PortAllocationOut]
    in_use: list[PortInUseOut]


class NamedIdOut(BaseModel):
    id: int
    name: str


# ---------------------------------------------------------------- export
class ExportPlanRequest(BaseModel):
    app_ids: list[int] | None = None


class ExportPlanModel(BaseModel):
    app_id: int
    path: str
    new_xml: str
    old_xml: str | None = None
    diff: str = ""
    changed: bool

    @classmethod
    def from_domain(cls, p: ExportPlan) -> ExportPlanModel:
        """Build from the exporter's plan."""
        return cls(
            app_id=p.app_id, path=p.path, new_xml=p.new_xml, old_xml=p.old_xml, diff=p.diff, changed=p.changed
        )

    def to_domain(self) -> ExportPlan:
        """Convert back to the exporter's plan type."""
        return ExportPlan(self.app_id, self.path, self.new_xml, self.old_xml, self.diff, self.changed)


class ExportApplyRequest(BaseModel):
    plans: list[ExportPlanModel]
    include_secrets: bool | None = None


class ExportApplyOut(BaseModel):
    written: list[str]


# ---------------------------------------------------------------- imports
class ImportPlanRequest(BaseModel):
    container_name: str = Field(min_length=1)
    source_id: int | None = None
    new_source: NewSource | None = None
    template_path: str | None = None
    options: ImportOptions = Field(default_factory=ImportOptions)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.source_id is None) == (self.new_source is None):
            raise ValueError("give exactly one of source_id or new_source")
        return self


EventOut = dict[str, Any]
