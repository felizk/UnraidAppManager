"""`EngineApi` over the docker SDK's low-level client (raw inspect JSON in, raw create configs out)."""

from __future__ import annotations

from typing import Any

import docker
import docker.errors


class SdkEngineApi:
    """Blocking calls; callers in the event loop wrap them in `asyncio.to_thread`."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def api(self) -> Any:
        """The low-level `docker.APIClient`, created on first use."""
        if self._client is None:
            self._client = docker.from_env().api
        return self._client

    def inspect_container(self, ref: str) -> dict[str, Any] | None:
        """`GET /containers/{ref}/json`, or None when it doesn't exist."""
        try:
            return dict(self.api.inspect_container(ref))
        except docker.errors.NotFound:
            return None

    def inspect_image(self, ref: str) -> dict[str, Any] | None:
        """`GET /images/{ref}/json`, or None when it isn't on the server."""
        try:
            return dict(self.api.inspect_image(ref))
        except docker.errors.NotFound:
            return None

    def create(self, config: dict[str, Any], name: str) -> str:
        """`POST /containers/create` with a full config (HostConfig and NetworkingConfig inside)."""
        return str(self.api.create_container_from_config(config, name=name)["Id"])

    def connect(self, network: str, container_id: str, endpoint: dict[str, Any]) -> None:
        """Attach a created container to another network with the same endpoint settings."""
        ipam = endpoint.get("IPAMConfig") or {}
        self.api.connect_container_to_network(
            container_id,
            network,
            aliases=endpoint.get("Aliases") or None,
            links=endpoint.get("Links") or None,
            ipv4_address=ipam.get("IPv4Address") or None,
            ipv6_address=ipam.get("IPv6Address") or None,
            link_local_ips=ipam.get("LinkLocalIPs") or None,
            driver_opt=endpoint.get("DriverOpts") or None,
        )

    def start(self, container_id: str) -> None:
        """Start a container."""
        self.api.start(container_id)

    def stop(self, container_id: str, timeout: int) -> None:
        """Stop a container."""
        self.api.stop(container_id, timeout=timeout)

    def remove(self, container_id: str, force: bool = False) -> None:
        """Remove a container."""
        self.api.remove_container(container_id, force=force)

    def rename(self, container_id: str, name: str) -> None:
        """Rename a container."""
        self.api.rename(container_id, name)
