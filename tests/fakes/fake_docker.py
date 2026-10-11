"""In-memory Docker engine with failure injection."""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import replace

from uam.domain import ContainerInfo, ContainerSpec, RegistryAuth
from uam.errors import ConflictError, NotFound

OPS = ("pull", "create", "start", "stop", "remove", "rename", "health")


class FakeDocker:
    """Containers live in `self.containers` (id → ContainerInfo); specs in `self.specs`.

    * `set_image(ref, digest)` makes an image pullable.
    * `fail_next(op, exc, times)` makes the next `times` calls of `op` raise (`health` returns False).
    * `unhealthy_images` holds image refs whose containers never become healthy.
    * `seed_container(info)` adds a container UAM did not create (e.g. for import tests).
    """

    def __init__(self, host_ip: str = "192.168.1.10") -> None:
        self.containers: dict[str, ContainerInfo] = {}
        self.specs: dict[str, ContainerSpec] = {}
        self.images: dict[str, str] = {}
        self.unhealthy_images: set[str] = set()
        self.pulls: list[tuple[str, RegistryAuth | None]] = []
        self.calls: list[tuple[str, str]] = []
        self.log_text: dict[str, str] = {}
        self._failures: dict[str, list[BaseException | None]] = defaultdict(list)
        self._ids = itertools.count(1)
        self._host_ip = host_ip

    # ----- test controls -----
    def set_image(self, image_ref: str, digest: str) -> None:
        self.images[image_ref] = digest

    def fail_next(self, op: str, exc: BaseException | None = None, times: int = 1) -> None:
        assert op in OPS, op
        self._failures[op].extend([exc or RuntimeError(f"injected {op} failure")] * times)

    def seed_container(self, info: ContainerInfo) -> ContainerInfo:
        self.containers[info.id] = info
        return info

    def by_name(self, name: str) -> ContainerInfo | None:
        return next((c for c in self.containers.values() if c.name == name), None)

    def _maybe_fail(self, op: str) -> BaseException | None:
        return self._failures[op].pop(0) if self._failures[op] else None

    def _raise_if_failing(self, op: str) -> None:
        exc = self._maybe_fail(op)
        if exc is not None:
            raise exc

    def _get(self, container_id: str) -> ContainerInfo:
        info = self.containers.get(container_id)
        if info is None:
            raise NotFound(f"container {container_id}")
        return info

    def _digest_for(self, image_ref: str) -> str | None:
        if "@" in image_ref:
            return image_ref.split("@", 1)[1]
        return self.images.get(image_ref)

    # ----- DockerEngine -----
    async def pull(self, image_ref: str, auth: RegistryAuth | None) -> str:
        self.calls.append(("pull", image_ref))
        self._raise_if_failing("pull")
        digest = self._digest_for(image_ref)
        if digest is None:
            raise NotFound(f"image {image_ref}")
        self.pulls.append((image_ref, auth))
        return digest

    async def create(self, spec: ContainerSpec) -> str:
        self.calls.append(("create", spec.name))
        self._raise_if_failing("create")
        if self.by_name(spec.name):
            raise ConflictError(f"container name {spec.name} in use")
        cid = f"{next(self._ids):064x}"
        self.containers[cid] = ContainerInfo(
            id=cid,
            name=spec.name,
            image=spec.image,
            image_digest=self._digest_for(spec.image),
            state="created",
            health=None,
            labels=dict(spec.labels),
            published_ports=() if spec.network == "host" else spec.ports,
            env=dict(spec.env),
            mounts=spec.mounts,
            devices=spec.devices,
            network=spec.network,
            restart_policy=spec.restart_policy,
        )
        self.specs[cid] = spec
        return cid

    async def start(self, container_id: str) -> None:
        self.calls.append(("start", container_id))
        self._raise_if_failing("start")
        self.containers[container_id] = replace(self._get(container_id), state="running")

    async def stop(self, container_id: str, timeout: int = 20) -> None:
        self.calls.append(("stop", container_id))
        self._raise_if_failing("stop")
        self.containers[container_id] = replace(self._get(container_id), state="exited")

    async def remove(self, container_id: str, force: bool = False) -> None:
        self.calls.append(("remove", container_id))
        self._raise_if_failing("remove")
        info = self._get(container_id)
        if info.state == "running" and not force:
            raise ConflictError("container is running")
        del self.containers[container_id]
        self.specs.pop(container_id, None)

    async def rename(self, container_id: str, new_name: str) -> None:
        self.calls.append(("rename", container_id))
        self._raise_if_failing("rename")
        other = self.by_name(new_name)
        if other and other.id != container_id:
            raise ConflictError(f"container name {new_name} in use")
        self.containers[container_id] = replace(self._get(container_id), name=new_name)

    async def inspect(self, name_or_id: str) -> ContainerInfo | None:
        if name_or_id in self.containers:
            return self.containers[name_or_id]
        return self.by_name(name_or_id) or next(
            (c for c in self.containers.values() if len(name_or_id) >= 12 and c.id.startswith(name_or_id)),
            None,
        )

    async def list_containers(self, all: bool = True) -> list[ContainerInfo]:
        return [c for c in self.containers.values() if all or c.state == "running"]

    async def wait_healthy(self, container_id: str, timeout_s: int = 90, stable_s: int = 15) -> bool:
        self.calls.append(("health", container_id))
        if self._maybe_fail("health") is not None:
            return False
        info = self._get(container_id)
        healthy = info.state == "running" and info.image not in self.unhealthy_images
        self.containers[container_id] = replace(info, health="healthy" if healthy else "unhealthy")
        return healthy

    async def logs(self, container_id: str, tail: int = 200) -> str:
        self._get(container_id)
        return "\n".join(self.log_text.get(container_id, "").splitlines()[-tail:])

    async def host_ip(self) -> str:
        return self._host_ip
