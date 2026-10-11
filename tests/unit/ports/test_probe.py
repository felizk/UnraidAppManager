from pathlib import Path

from uam.ports.probe import HostListenerProbe, parse_proc_net

TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1 0
   1: 0100007F:0050 0100007F:C000 01 00000000:00000000 00:00000000 00000000     0        0 2 1 0
"""
UDP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref
   0: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 3 2 0 0
"""


def test_parse_tcp_only_listen() -> None:
    assert parse_proc_net(TCP, "tcp") == {8080}


def test_parse_udp_bound() -> None:
    assert parse_proc_net(UDP, "udp") == {53}


def test_parse_garbage_ignored() -> None:
    assert parse_proc_net("header\nbad line\n  0: zz 1 0A x\n", "tcp") == set()


async def test_probe_reads_dir(tmp_path: Path) -> None:
    (tmp_path / "tcp").write_text(TCP)
    (tmp_path / "udp").write_text(UDP)
    probe = HostListenerProbe(str(tmp_path))
    assert await probe.listening() == {(8080, "tcp"), (53, "udp")}


async def test_probe_missing_path(tmp_path: Path) -> None:
    assert await HostListenerProbe(str(tmp_path / "nope")).listening() == set()
