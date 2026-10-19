"""Data types shared by every UAM module. See docs/contracts.md; only the orchestrator edits this file."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

Protocol_ = Literal["tcp", "udp"]

# ---------- Unraid template ----------
ConfigType = Literal["Port", "Path", "Variable", "Label", "Device"]


@dataclass(frozen=True, slots=True)
class TemplateConfig:
    """One `<Config>` entry of an Unraid template."""

    name: str
    target: str
    default: str
    value: str
    type: ConfigType
    mode: str = ""
    description: str = ""
    display: str = "always"
    required: bool = False
    mask: bool = False


@dataclass(frozen=True, slots=True)
class UnraidTemplate:
    """A parsed Unraid container template."""

    name: str
    repository: str
    registry: str = ""
    network: str = "bridge"
    privileged: bool = False
    shell: str = "sh"
    support: str = ""
    project: str = ""
    overview: str = ""
    category: str = ""
    webui: str = ""
    icon: str = ""
    extra_params: str = ""
    post_args: str = ""
    cpuset: str = ""
    configs: tuple[TemplateConfig, ...] = ()
    unknown_elements: tuple[tuple[str, str], ...] = ()


# ---------- Sources / credentials ----------
class CredentialKind(StrEnum):
    GITHUB_PAT = "github_pat"
    REGISTRY = "registry"
    NPM = "npm"
    PUSHOVER = "pushover"


@dataclass(frozen=True, slots=True)
class Credential:
    """Credential metadata. The secret itself is only available through `Vault.reveal`."""

    id: int
    name: str
    kind: CredentialKind
    username: str
    registry_host: str = ""


@dataclass(frozen=True, slots=True)
class Source:
    """A GitHub repository that provides one or more app templates."""

    id: int
    owner: str
    repo: str
    ref: str = "main"
    template_path: str | None = None
    credential_id: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredTemplate:
    """A template file found in a source at a given ref."""

    path: str
    sha: str
    commit_sha: str
    committed_at: datetime
    xml: str


# ---------- Apps ----------
class AppState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    RUNNING = "running"
    STOPPED = "stopped"
    UPDATING = "updating"
    FAILED = "failed"
    FAILED_UPDATE = "failed_update"
    REMOVING = "removing"


@dataclass(frozen=True, slots=True)
class PortAllocation:
    app_id: int
    container_port: int
    protocol: Protocol_
    host_port: int


@dataclass(frozen=True, slots=True)
class ProxySettings:
    """Nginx Proxy Manager settings for an app. `subdomain=None` disables registration."""

    subdomain: str | None = None
    forward_mode: Literal["host_port", "container_network"] = "host_port"
    certificate_id: int | None = None
    ssl_forced: bool = True
    websockets: bool = True
    access_list_id: int | None = None


@dataclass(slots=True)
class App:
    """An app managed by UAM (mutable; persisted through `AppRepository`)."""

    id: int
    source_id: int
    template_path: str
    name: str
    overrides: dict[str, str]
    image_tag_override: str | None
    auto_ports: bool
    auto_update_image: bool
    auto_update_template: bool
    proxy: ProxySettings
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
    labels_pending: bool = False


# ---------- Container spec ----------
@dataclass(frozen=True, slots=True)
class PortBinding:
    container_port: int
    protocol: Protocol_
    host_port: int
    host_ip: str = ""


@dataclass(frozen=True, slots=True)
class Mount:
    host_path: str
    container_path: str
    mode: str = "rw"


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Everything needed to create a container."""

    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    ports: tuple[PortBinding, ...] = ()
    mounts: tuple[Mount, ...] = ()
    devices: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    network: str = "bridge"
    privileged: bool = False
    restart_policy: str = "unless-stopped"
    hostname: str | None = None
    cap_add: tuple[str, ...] = ()
    memory_limit: str | None = None
    cpus: float | None = None
    cpuset: str | None = None
    log_opts: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    extra_networks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedApp:
    spec: ContainerSpec
    webui_url_template: str | None
    webui_host_port: int | None
    webui_container_port: int | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """A container as reported by the Docker engine."""

    id: str
    name: str
    image: str
    image_digest: str | None
    state: str
    health: str | None
    labels: dict[str, str]
    published_ports: tuple[PortBinding, ...]
    image_id: str = ""
    image_labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    devices: tuple[str, ...] = ()
    network: str = "bridge"
    restart_policy: str = ""
    restart_count: int = 0
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class ProxyHost:
    id: int
    domain_names: tuple[str, ...]
    forward_scheme: str
    forward_host: str
    forward_port: int
    certificate_id: int | None = None
    ssl_forced: bool = False
    websockets: bool = False
    access_list_id: int | None = None


@dataclass(frozen=True, slots=True)
class RegistryAuth:
    username: str
    password: str
    registry: str


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    app_id: int
    template_changed: bool
    new_template_commit: str | None
    image_changed: bool
    new_image_digest: str | None
    needs_attention: str | None
    checked_at: datetime


# ---------- Import of existing containers (T16) ----------
@dataclass(frozen=True, slots=True)
class NewSource:
    owner: str
    repo: str
    ref: str = "main"
    credential_id: int | None = None


@dataclass(frozen=True, slots=True)
class SourceMatch:
    source_id: int | None
    owner: str
    repo: str
    template_path: str | None
    confidence: Literal["high", "medium", "low"]
    reason: str


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    container_id: str
    container_name: str
    image: str
    state: str
    unraid_managed: str | None
    flash_template_path: str | None
    flash_template: UnraidTemplate | None
    source_matches: tuple[SourceMatch, ...]
    published_ports: tuple[PortBinding, ...]
    proxy_host: ProxyHost | None
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportOptions:
    keep_host_ports: bool = True
    claim_proxy_host: bool = True
    subdomain: str | None = None
    recreate_now: bool = True
    auto_update_image: bool = True
    auto_update_template: bool = False
    archive_flash_template: bool = False


@dataclass(frozen=True, slots=True)
class ImportPlan:
    candidate: ImportCandidate
    source_id: int | None
    new_source: NewSource | None
    template_path: str
    template_commit: str
    overrides: dict[str, str]
    image_tag_override: str | None
    installed_image_digest: str
    pinned_ports: tuple[tuple[int, Protocol_, int], ...]
    proxy: ProxySettings
    adopt_proxy_host_id: int | None
    options: ImportOptions
    template_diff: str
    dropped: tuple[str, ...]
    warnings: tuple[str, ...]


# ---------- Labels UAM puts on containers ----------
LABEL_MANAGED = "uam.managed"
LABEL_APP_ID = "uam.app_id"
LABEL_SOURCE = "uam.source"
LABEL_TEMPLATE_COMMIT = "uam.template_commit"
LABEL_UNRAID_MANAGED = "net.unraid.docker.managed"
LABEL_UNRAID_ICON = "net.unraid.docker.icon"
LABEL_UNRAID_WEBUI = "net.unraid.docker.webui"
