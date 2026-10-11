"""Host listener probe: reads /proc/net/* to find ports bound on the host."""

from __future__ import annotations

import asyncio
from pathlib import Path

from uam.domain import Protocol_

DEFAULT_PROC_NET = "/host/proc/net"
TCP_LISTEN = "0A"
_FILES: tuple[tuple[str, Protocol_], ...] = (
    ("tcp", "tcp"),
    ("tcp6", "tcp"),
    ("udp", "udp"),
    ("udp6", "udp"),
)


def parse_proc_net(content: str, protocol: Protocol_) -> set[int]:
    """Ports from the text of one /proc/net file.

    TCP files only count sockets in LISTEN state; UDP files count every bound socket.
    """
    ports: set[int] = set()
    for line in content.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        local, state = fields[1], fields[3]
        if protocol == "tcp" and state != TCP_LISTEN:
            continue
        try:
            port = int(local.rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if port:
            ports.add(port)
    return ports


class HostListenerProbe:
    """Reports (port, protocol) pairs that something on the host is listening on."""

    def __init__(self, proc_net_dir: str = DEFAULT_PROC_NET) -> None:
        self._dir = Path(proc_net_dir)

    def _read(self) -> set[tuple[int, Protocol_]]:
        found: set[tuple[int, Protocol_]] = set()
        if not self._dir.is_dir():
            return found
        for name, protocol in _FILES:
            try:
                text = (self._dir / name).read_text()
            except OSError:
                continue
            found.update((p, protocol) for p in parse_proc_net(text, protocol))
        return found

    async def listening(self) -> set[tuple[int, Protocol_]]:
        """Ports in use on the host; empty when the proc path is missing."""
        return await asyncio.to_thread(self._read)
