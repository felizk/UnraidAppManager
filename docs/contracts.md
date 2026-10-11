# Contracts

This document is the source of truth for the interfaces between modules. T00 turns it into
`uam/domain.py` (data types) and `uam/contracts.py` (Protocols), plus in-memory fakes in
`tests/fakes/`. After T00, only the orchestrator changes these.

Conventions:
* All I/O is `async`.
* Data types are frozen `@dataclass(slots=True)`, except where a type is noted as mutable.
* Errors subclass `uam.errors.UamError` (see the end of this document).

## domain.py

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

Protocol_ = Literal["tcp", "udp"]

# ---------- Unraid template ----------
ConfigType = Literal["Port", "Path", "Variable", "Label", "Device"]

@dataclass(frozen=True, slots=True)
class TemplateConfig:
    name: str                 # Config@Name (display name)
    target: str               # Config@Target: container port / container path / env var / label key / device
    default: str              # Config@Default
    value: str                # element text (current value)
    type: ConfigType
    mode: str = ""            # "tcp"/"udp" for Port, "rw"/"ro"/"rw,slave"… for Path
    description: str = ""
    display: str = "always"   # always | always-hide | advanced | advanced-hide
    required: bool = False
    mask: bool = False        # secret

@dataclass(frozen=True, slots=True)
class UnraidTemplate:
    name: str
    repository: str           # image ref, e.g. ghcr.io/me/app:latest
    registry: str = ""
    network: str = "bridge"
    privileged: bool = False
    shell: str = "sh"
    support: str = ""
    project: str = ""
    overview: str = ""
    category: str = ""
    webui: str = ""           # e.g. http://[IP]:[PORT:8080]/
    icon: str = ""
    extra_params: str = ""
    post_args: str = ""
    cpuset: str = ""
    configs: tuple[TemplateConfig, ...] = ()
    unknown_elements: tuple[tuple[str, str], ...] = ()  # (tag, raw xml) preserved for round-trip

# ---------- Sources / credentials ----------
class CredentialKind(StrEnum):
    GITHUB_PAT = "github_pat"
    REGISTRY = "registry"     # username + password/token for a registry host
    NPM = "npm"               # identity (email) + secret

@dataclass(frozen=True, slots=True)
class Credential:
    id: int
    name: str
    kind: CredentialKind
    username: str             # GitHub user / registry user / NPM email
    registry_host: str = ""   # for REGISTRY: "ghcr.io", "docker.io", …
    # secret is never held here; obtain it via Vault.reveal(id)

@dataclass(frozen=True, slots=True)
class Source:
    id: int
    owner: str
    repo: str
    ref: str = "main"
    template_path: str | None = None   # explicit path; None = auto-discover
    credential_id: int | None = None   # GitHub PAT for private repos

@dataclass(frozen=True, slots=True)
class DiscoveredTemplate:
    path: str
    sha: str            # blob SHA of the file
    commit_sha: str     # last commit touching the path at ref
    committed_at: datetime
    xml: str

# ---------- Apps ----------
class AppState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    RUNNING = "running"
    STOPPED = "stopped"
    UPDATING = "updating"
    FAILED = "failed"               # install failed
    FAILED_UPDATE = "failed_update" # rolled back
    REMOVING = "removing"

@dataclass(frozen=True, slots=True)
class PortAllocation:
    app_id: int
    container_port: int
    protocol: Protocol_
    host_port: int

@dataclass(frozen=True, slots=True)
class ProxySettings:
    subdomain: str | None = None        # None = no NPM registration
    forward_mode: Literal["host_port", "container_network"] = "host_port"
    certificate_id: int | None = None
    ssl_forced: bool = True
    websockets: bool = True
    access_list_id: int | None = None

@dataclass(slots=True)  # mutable
class App:
    id: int
    source_id: int
    template_path: str
    name: str                           # container name, unique
    overrides: dict[str, str]           # key = TemplateConfig.target (+ type prefix, see T08), value
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
    needs_attention: str | None         # human-readable reason
    last_change_at: datetime | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    last_error: str | None

# ---------- Container spec (input to docker adapter) ----------
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
    name: str
    image: str                          # fully qualified incl. tag
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
    memory_limit: str | None = None     # "512m"
    cpus: float | None = None
    cpuset: str | None = None
    log_opts: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()       # from PostArgs
    extra_networks: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ResolvedApp:
    spec: ContainerSpec
    webui_url_template: str | None      # with [IP]/[PORT:x] resolved except [IP]
    webui_host_port: int | None
    webui_container_port: int | None
    warnings: tuple[str, ...]           # e.g. unsupported ExtraParams flags

@dataclass(frozen=True, slots=True)
class ContainerInfo:
    id: str
    name: str
    image: str
    image_digest: str | None            # RepoDigest matching the image's repository
    state: str                          # running/exited/…
    health: str | None                  # healthy/unhealthy/starting/None
    labels: dict[str, str]
    published_ports: tuple[PortBinding, ...]

@dataclass(frozen=True, slots=True)
class ProxyHost:
    id: int
    domain_names: tuple[str, ...]
    forward_scheme: str
    forward_host: str
    forward_port: int

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
```

## contracts.py

```python
from typing import Protocol, Sequence
from uam.domain import *

class GitHubClient(Protocol):
    async def discover_templates(self, source: Source, token: str | None) -> list[DiscoveredTemplate]: ...
    async def get_template(self, source: Source, path: str, token: str | None) -> DiscoveredTemplate: ...
    async def latest_commit_for_path(self, source: Source, path: str, token: str | None) -> tuple[str, datetime] | None:
        """Conditional (ETag) request. Returns None when unchanged since the last call (HTTP 304)."""
    async def rate_limit_remaining_fraction(self) -> float: ...
    async def validate_token(self, token: str) -> list[str]:  # returns scopes

class RegistryClient(Protocol):
    async def get_digest(self, image_ref: str, auth: RegistryAuth | None) -> str:
        """HEAD manifest → Docker-Content-Digest (manifest-list digest for multi-arch)."""

class DockerEngine(Protocol):
    async def pull(self, image_ref: str, auth: RegistryAuth | None) -> str: ...  # returns repo digest
    async def create(self, spec: ContainerSpec) -> str: ...                      # container id
    async def start(self, container_id: str) -> None: ...
    async def stop(self, container_id: str, timeout: int = 20) -> None: ...
    async def remove(self, container_id: str, force: bool = False) -> None: ...
    async def rename(self, container_id: str, new_name: str) -> None: ...
    async def inspect(self, name_or_id: str) -> ContainerInfo | None: ...
    async def list_containers(self, all: bool = True) -> list[ContainerInfo]: ...
    async def wait_healthy(self, container_id: str, timeout_s: int = 90, stable_s: int = 15) -> bool: ...
    async def logs(self, container_id: str, tail: int = 200) -> str: ...
    async def host_ip(self) -> str: ...  # best guess of the Unraid LAN IP (setting overrides)

class PortAllocator(Protocol):
    async def allocate(self, app_id: int, requests: Sequence[tuple[int, Protocol_, int | None]]) -> list[PortAllocation]:
        """requests = (container_port, protocol, preferred_host_port|None). Stable: existing allocations are returned unchanged."""
    async def release(self, app_id: int) -> None: ...
    async def in_use(self) -> set[tuple[int, Protocol_]]: ...

class ProxyManager(Protocol):
    async def ensure_proxy_host(self, domain: str, forward_host: str, forward_port: int,
                                settings: ProxySettings, existing_id: int | None) -> int: ...
    async def delete_proxy_host(self, host_id: int) -> None: ...  # idempotent (404 ok)
    async def find_by_domain(self, domain: str) -> ProxyHost | None: ...
    async def list_certificates(self) -> list[tuple[int, str]]: ...
    async def list_access_lists(self) -> list[tuple[int, str]]: ...

class Vault(Protocol):
    async def store(self, name: str, kind: CredentialKind, username: str, secret: str, registry_host: str = "") -> Credential: ...
    async def update_secret(self, credential_id: int, secret: str) -> None: ...
    async def reveal(self, credential_id: int) -> str: ...
    async def get(self, credential_id: int) -> Credential: ...
    async def list(self) -> list[Credential]: ...
    async def delete(self, credential_id: int) -> None: ...
    async def registry_auth_for(self, image_ref: str) -> RegistryAuth | None:
        """Match the image host to a REGISTRY credential. For ghcr.io, fall back to a GITHUB_PAT."""

class TemplateCodec(Protocol):
    def parse(self, xml: str) -> UnraidTemplate: ...
    def render(self, template: UnraidTemplate) -> str: ...

class SpecResolver(Protocol):
    async def resolve(self, app: App, template: UnraidTemplate) -> ResolvedApp: ...

class AppRepository(Protocol):  # db layer, implemented in T00
    async def get(self, app_id: int) -> App: ...
    async def get_by_name(self, name: str) -> App | None: ...
    async def list(self) -> list[App]: ...
    async def due_for_check(self, now: datetime) -> list[App]: ...
    async def add(self, app: App) -> App: ...      # id ignored on input
    async def save(self, app: App) -> None: ...
    async def delete(self, app_id: int) -> None: ...

class SourceRepository(Protocol):
    async def get(self, source_id: int) -> Source: ...
    async def list(self) -> list[Source]: ...
    async def add(self, source: Source) -> Source: ...
    async def delete(self, source_id: int) -> None: ...

class SettingsStore(Protocol):
    async def get(self) -> "Settings": ...       # see uam/settings.py (T00)
    async def save(self, settings: "Settings") -> None: ...

class EventLog(Protocol):
    async def record(self, app_id: int | None, kind: str, message: str, level: str = "info") -> None: ...
    async def recent(self, app_id: int | None = None, limit: int = 100) -> list[dict]: ...
```

## Settings (uam/settings.py)

A mutable dataclass that is persisted as a single JSON row:

```python
@dataclass
class Settings:
    port_range_start: int = 30000
    port_range_end: int = 30999
    reserved_ports: list[int] = field(default_factory=list)
    host_ip: str | None = None                    # override auto-detection
    appdata_root: str = "/mnt/user/appdata"
    npm_url: str | None = None                    # e.g. http://192.168.1.10:81
    npm_credential_id: int | None = None
    base_domain: str | None = None                # e.g. example.com
    default_certificate_id: int | None = None
    hot_window_hours: int = 168
    hot_interval_minutes: int = 5
    cold_interval_hours: int = 24
    template_export_dir: str = "/unraid-templates"
    template_export_include_secrets: bool = True
    unraid_managed_label: str = "uam"
```

## Errors (uam/errors.py)

`UamError` is the base class. Subclasses: `NotFound`, `ConflictError`, `AuthError`,
`RateLimited(reset_at)`, `RemoteError(status, body)`, `TemplateParseError`,
`PortRangeExhausted`, `DeployError(stage, cause)`, `ValidationError`.

## Labels UAM puts on containers

```
uam.managed=true
uam.app_id=<id>
uam.source=<owner>/<repo>
uam.template_commit=<sha>
net.unraid.docker.managed=<settings.unraid_managed_label>
net.unraid.docker.icon=<template icon>
net.unraid.docker.webui=<resolved webui url>
```
