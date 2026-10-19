"""DockerEngine adapter built on the blocking `docker` SDK (every call runs in a thread)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import docker
import docker.errors
from docker.utils import parse_repository_tag

from uam.domain import ContainerInfo, ContainerSpec, Mount, PortBinding, RegistryAuth
from uam.errors import AuthError, ConflictError, DeployError, NotFound, RemoteError

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
_PROPAGATION_TO_MODE = {"rslave": "slave", "slave": "slave", "rshared": "shared", "shared": "shared"}


def _translate(exc: docker.errors.DockerException, what: str) -> Exception:
    """Map an SDK exception onto a UamError."""
    if isinstance(exc, docker.errors.NotFound):
        return NotFound(f"{what}: {exc}")
    if isinstance(exc, docker.errors.APIError):
        status = exc.status_code or 0
        if status == 409:
            return ConflictError(f"{what}: {exc.explanation or exc}")
        text = str(exc.explanation or exc)
        if status in (401, 403) or any(w in text.lower() for w in ("unauthorized", "denied")):
            return AuthError(f"{what}: {text}")
        return RemoteError(status, str(exc.explanation or exc))
    return RemoteError(0, f"{what}: {exc}")


def _repo_of(image_ref: str) -> str:
    """Repository part of an image ref (no tag, no digest), normalised for Docker Hub."""
    repo, _ = parse_repository_tag(image_ref)
    for prefix in ("docker.io/", "index.docker.io/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix) :]
    if repo.startswith("library/"):
        repo = repo[len("library/") :]
    return repo


def _matching_digest(repo_digests: list[str], image_ref: str) -> str | None:
    """Return the `sha256:…` part of the RepoDigest whose repository matches `image_ref`."""
    wanted = _repo_of(image_ref)
    for entry in repo_digests:
        repo, _, digest = entry.partition("@")
        if digest and _repo_of(repo) == wanted:
            return digest
    return None


def _parse_restart(policy: str) -> dict[str, Any]:
    name, _, retries = policy.partition(":")
    out: dict[str, Any] = {"Name": name}
    if retries.isdigit():
        out["MaximumRetryCount"] = int(retries)
    return out


def build_create_kwargs(spec: ContainerSpec) -> dict[str, Any]:
    """Translate a ContainerSpec into keyword arguments for `containers.create`."""
    kwargs: dict[str, Any] = {
        "image": spec.image,
        "name": spec.name,
        "detach": True,
        "environment": dict(spec.env),
        "labels": dict(spec.labels),
        "network_mode": spec.network,
        "privileged": spec.privileged,
    }
    if spec.network != "host" and spec.ports:
        kwargs["ports"] = {f"{p.container_port}/{p.protocol}": (p.host_ip, p.host_port) for p in spec.ports}
    if spec.mounts:
        # List form ("host:container:mode") so one host path can be mounted at several container paths.
        kwargs["volumes"] = [f"{m.host_path}:{m.container_path}:{m.mode}" for m in spec.mounts]
    if spec.devices:
        kwargs["devices"] = list(spec.devices)
    if spec.restart_policy:
        kwargs["restart_policy"] = _parse_restart(spec.restart_policy)
    if spec.hostname:
        kwargs["hostname"] = spec.hostname
    if spec.cap_add:
        kwargs["cap_add"] = list(spec.cap_add)
    if spec.memory_limit:
        kwargs["mem_limit"] = spec.memory_limit
    if spec.cpus:
        kwargs["nano_cpus"] = int(spec.cpus * 1_000_000_000)
    if spec.cpuset:
        kwargs["cpuset_cpus"] = spec.cpuset
    if spec.log_opts:
        kwargs["log_config"] = {"Type": "json-file", "Config": dict(spec.log_opts)}
    if spec.command:
        kwargs["command"] = list(spec.command)
    return kwargs


class SdkDockerEngine:
    """`DockerEngine` implementation over the docker SDK.

    `client` may be injected (tests); otherwise `docker.from_env()` is used lazily.
    `sleep` and `monotonic` are injectable so `wait_healthy` can be tested with a fake clock.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        proc_root: str = "/host/proc",
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._monotonic = monotonic
        self._proc_root = Path(proc_root)

    @property
    def client(self) -> Any:
        """The SDK client, created on first use."""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ----- images -----
    async def pull(self, image_ref: str, auth: RegistryAuth | None) -> str:
        """Pull `image_ref` and return the RepoDigest matching its repository."""
        return await asyncio.to_thread(self._pull, image_ref, auth)

    def _pull(self, image_ref: str, auth: RegistryAuth | None) -> str:
        kwargs: dict[str, Any] = {}
        if auth is not None:
            kwargs["auth_config"] = {"username": auth.username, "password": auth.password}
        try:
            image = self.client.images.pull(image_ref, **kwargs)
        except docker.errors.DockerException as exc:
            err = _translate(exc, f"pull {image_ref}")
            if not (kwargs and isinstance(err, AuthError)):
                raise err from exc
            # The credential was refused; a public image can still be pulled without it.
            try:
                image = self.client.images.pull(image_ref)
            except docker.errors.DockerException:
                raise err from exc
            log.warning("registry refused the stored credential for %s; pulled anonymously", image_ref)
        digest = _matching_digest(image.attrs.get("RepoDigests") or [], image_ref)
        if digest is None:
            raise DeployError("pull", f"no repo digest for {image_ref}")
        return digest

    # ----- lifecycle -----
    async def create(self, spec: ContainerSpec) -> str:
        """Create (not start) a container and attach extra networks. Returns its id."""
        return await asyncio.to_thread(self._create, spec)

    def _create(self, spec: ContainerSpec) -> str:
        try:
            container = self.client.containers.create(**build_create_kwargs(spec))
        except docker.errors.DockerException as exc:
            raise _translate(exc, f"create {spec.name}") from exc
        try:
            for net in spec.extra_networks:
                self.client.networks.get(net).connect(container)
        except docker.errors.DockerException as exc:
            try:
                container.remove(force=True)
            except docker.errors.DockerException:
                log.warning("could not clean up container %s after network failure", spec.name)
            raise _translate(exc, f"connect {spec.name} to network") from exc
        return str(container.id)

    async def start(self, container_id: str) -> None:
        """Start a container."""
        await asyncio.to_thread(self._call, container_id, "start")

    async def stop(self, container_id: str, timeout: int = 20) -> None:
        """Stop a container, killing it after `timeout` seconds."""
        await asyncio.to_thread(self._call, container_id, "stop", timeout=timeout)

    async def remove(self, container_id: str, force: bool = False) -> None:
        """Remove a container."""
        await asyncio.to_thread(self._call, container_id, "remove", force=force)

    async def rename(self, container_id: str, new_name: str) -> None:
        """Rename a container; `ConflictError` if the name is taken."""
        await asyncio.to_thread(self._call, container_id, "rename", new_name)

    def _call(self, container_id: str, op: str, *args: Any, **kwargs: Any) -> None:
        try:
            getattr(self.client.containers.get(container_id), op)(*args, **kwargs)
        except docker.errors.DockerException as exc:
            raise _translate(exc, f"{op} {container_id}") from exc

    # ----- inspection -----
    async def inspect(self, name_or_id: str) -> ContainerInfo | None:
        """Full ContainerInfo (including import fields), or None if it does not exist."""
        return await asyncio.to_thread(self._inspect, name_or_id)

    def _inspect(self, name_or_id: str) -> ContainerInfo | None:
        try:
            attrs = self.client.containers.get(name_or_id).attrs
            return self._build_info(attrs, detailed=True, image_cache={})
        except docker.errors.NotFound:
            return None
        except docker.errors.DockerException as exc:
            raise _translate(exc, f"inspect {name_or_id}") from exc

    async def list_containers(self, all: bool = True) -> list[ContainerInfo]:
        """List containers (basic fields only; use `inspect` for the import fields)."""
        return await asyncio.to_thread(self._list, all)

    def _list(self, all_: bool) -> list[ContainerInfo]:
        try:
            cache: dict[str, dict[str, Any]] = {}
            return [
                self._build_info(c.attrs, detailed=False, image_cache=cache)
                for c in self.client.containers.list(all=all_)
            ]
        except docker.errors.DockerException as exc:
            raise _translate(exc, "list containers") from exc

    def _image_attrs(self, image_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if image_id not in cache:
            try:
                cache[image_id] = self.client.images.get(image_id).attrs
            except docker.errors.NotFound:
                cache[image_id] = {}
        return cache[image_id]

    def _build_info(
        self, attrs: dict[str, Any], *, detailed: bool, image_cache: dict[str, dict[str, Any]]
    ) -> ContainerInfo:
        config = attrs.get("Config") or {}
        host_config = attrs.get("HostConfig") or {}
        state = attrs.get("State") or {}
        image_ref = config.get("Image") or ""
        image_id = attrs.get("Image") or ""
        image_attrs = self._image_attrs(image_id, image_cache) if image_id else {}
        health = (state.get("Health") or {}).get("Status")
        base = ContainerInfo(
            id=attrs["Id"],
            name=str(attrs.get("Name", "")).lstrip("/"),
            image=image_ref,
            image_digest=_matching_digest(image_attrs.get("RepoDigests") or [], image_ref),
            state=state.get("Status", ""),
            health=health,
            labels=dict(config.get("Labels") or {}),
            published_ports=_published_ports(attrs),
            restart_count=int(attrs.get("RestartCount") or 0),
            exit_code=_exit_code(state),
        )
        if not detailed:
            return base
        image_config = image_attrs.get("Config") or {}
        image_env = set(image_config.get("Env") or [])
        env = dict(e.partition("=")[::2] for e in (config.get("Env") or []) if e not in image_env)
        return ContainerInfo(
            id=base.id,
            name=base.name,
            image=base.image,
            image_digest=base.image_digest,
            state=base.state,
            health=base.health,
            labels=base.labels,
            published_ports=base.published_ports,
            image_id=image_id,
            image_labels=dict(image_config.get("Labels") or {}),
            env=env,
            mounts=_bind_mounts(attrs),
            devices=tuple(
                f"{d['PathOnHost']}:{d['PathInContainer']}:{d.get('CgroupPermissions') or 'rwm'}"
                for d in (host_config.get("Devices") or [])
            ),
            network=host_config.get("NetworkMode") or "bridge",
            restart_policy=_restart_string(host_config.get("RestartPolicy") or {}),
            restart_count=base.restart_count,
            exit_code=base.exit_code,
        )

    # ----- health -----
    async def wait_healthy(self, container_id: str, timeout_s: int = 90, stable_s: int = 15) -> bool:
        """True once the container is healthy (healthcheck) or stably running (no healthcheck)."""
        deadline = self._monotonic() + timeout_s
        stable_since: float | None = None
        baseline_restarts: int | None = None
        while True:
            snap = await asyncio.to_thread(self._health_snapshot, container_id)
            if snap is None:
                return False
            status, health, restarts, has_healthcheck = snap
            now = self._monotonic()
            if has_healthcheck:
                if health == "healthy":
                    return True
                if health == "unhealthy" or status in ("exited", "dead"):
                    return False
            else:
                if status != "running":
                    return False
                if baseline_restarts is None or restarts != baseline_restarts:
                    baseline_restarts = restarts
                    stable_since = now
                if stable_since is not None and now - stable_since >= stable_s:
                    return True
            if now >= deadline:
                return False
            await self._sleep(POLL_INTERVAL_S)

    def _health_snapshot(self, container_id: str) -> tuple[str, str | None, int, bool] | None:
        try:
            attrs = self.client.containers.get(container_id).attrs
        except docker.errors.NotFound:
            return None
        except docker.errors.DockerException as exc:
            raise _translate(exc, f"health {container_id}") from exc
        state = attrs.get("State") or {}
        test = ((attrs.get("Config") or {}).get("Healthcheck") or {}).get("Test") or []
        has_check = bool(state.get("Health")) or (bool(test) and list(test) != ["NONE"])
        return (
            state.get("Status", ""),
            (state.get("Health") or {}).get("Status"),
            int(attrs.get("RestartCount") or state.get("RestartCount") or 0),
            has_check,
        )

    # ----- misc -----
    async def logs(self, container_id: str, tail: int = 200) -> str:
        """Last `tail` lines of combined stdout/stderr."""
        return await asyncio.to_thread(self._logs, container_id, tail)

    def _logs(self, container_id: str, tail: int) -> str:
        try:
            raw: bytes = self.client.containers.get(container_id).logs(tail=tail, stdout=True, stderr=True)
        except docker.errors.DockerException as exc:
            raise _translate(exc, f"logs {container_id}") from exc
        return raw.decode("utf-8", errors="replace")

    async def host_ip(self) -> str:
        """Best guess of the host LAN IP (the `Settings.host_ip` override is applied by callers)."""
        return await asyncio.to_thread(self._host_ip)

    def _host_ip(self) -> str:
        route = self._proc_root / "net" / "route"
        fib = self._proc_root / "net" / "fib_trie"
        local_ips = _fib_local_ips(fib.read_text()) if fib.exists() else []
        if route.exists():
            found = _ip_from_default_route(route.read_text(), local_ips)
            if found:
                return found
        if local_ips:
            candidates = [ip for ip in local_ips if not _is_docker_range(ip)]
            if candidates:
                return candidates[0]
        try:
            for net in self.client.networks.list(names=["bridge"]):
                for cfg in (net.attrs.get("IPAM") or {}).get("Config") or []:
                    if cfg.get("Gateway"):
                        return str(cfg["Gateway"])
        except docker.errors.DockerException as exc:
            raise _translate(exc, "host ip") from exc
        raise NotFound("could not determine host IP")


def _published_ports(attrs: dict[str, Any]) -> tuple[PortBinding, ...]:
    out: list[PortBinding] = []
    seen: set[tuple[int, str, int]] = set()
    ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
    for key, bindings in ports.items():
        port_s, _, proto = key.partition("/")
        if proto not in ("tcp", "udp") or not port_s.isdigit():
            continue
        for b in bindings or []:
            host_port = int(b.get("HostPort") or 0)
            ident = (int(port_s), proto, host_port)
            if not host_port or ident in seen:
                continue
            seen.add(ident)
            out.append(
                PortBinding(
                    container_port=int(port_s),
                    protocol=proto,
                    host_port=host_port,
                    host_ip=b.get("HostIp") or "",
                )
            )
    return tuple(out)


def _bind_mounts(attrs: dict[str, Any]) -> tuple[Mount, ...]:
    out: list[Mount] = []
    for m in attrs.get("Mounts") or []:
        if m.get("Type") != "bind":
            continue
        mode = "rw" if m.get("RW", True) else "ro"
        extra = _PROPAGATION_TO_MODE.get(m.get("Propagation") or "")
        if extra:
            mode = f"{mode},{extra}"
        out.append(Mount(host_path=m["Source"], container_path=m["Destination"], mode=mode))
    return tuple(out)


def _exit_code(state: dict[str, Any]) -> int | None:
    """The last exit code of a container that is not running (None while it runs)."""
    code = state.get("ExitCode")
    if state.get("Status") == "running" or not isinstance(code, int):
        return None
    return code


def _restart_string(policy: dict[str, Any]) -> str:
    name = policy.get("Name") or ""
    retries = policy.get("MaximumRetryCount") or 0
    return f"{name}:{retries}" if name == "on-failure" and retries else name


def _hex_ip(value: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(int.from_bytes(bytes.fromhex(value), "little"))


def _is_docker_range(ip: str) -> bool:
    return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network("172.16.0.0/12")


def _fib_local_ips(text: str) -> list[str]:
    """Non-loopback LOCAL /32 addresses from /proc/net/fib_trie, in file order."""
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines[:-1]):
        stripped = line.strip()
        if stripped.startswith("|--") and "/32 host LOCAL" in lines[i + 1]:
            ip = stripped[3:].strip()
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    return out


def _ip_from_default_route(route_text: str, local_ips: list[str]) -> str | None:
    """Find the host address on the default route's interface from /proc/net/route content."""
    rows = [ln.split() for ln in route_text.splitlines()[1:] if ln.strip()]
    iface = next((r[0] for r in rows if len(r) >= 8 and r[1] == "00000000"), None)
    if iface is None:
        return None
    for r in rows:
        if r[0] != iface or r[1] == "00000000":
            continue
        net = ipaddress.IPv4Network(f"{_hex_ip(r[1])}/{_hex_ip(r[7])}", strict=False)
        for ip in local_ips:
            if ipaddress.IPv4Address(ip) in net:
                return ip
    return None
