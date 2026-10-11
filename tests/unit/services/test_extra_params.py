from __future__ import annotations

import pytest

from uam.errors import ValidationError
from uam.services.extra_params import ExtraParams, parse_extra_params


def _attr(p: ExtraParams, name: str) -> object:
    value = getattr(p, name)
    return list(value) if isinstance(value, list) else value


@pytest.mark.parametrize(
    ("text", "attr", "expected"),
    [
        ("--restart=always", "restart", "always"),
        ("--restart on-failure", "restart", "on-failure"),
        ("--memory=1g", "memory", "1g"),
        ("--memory 512m", "memory", "512m"),
        ("-m 256m", "memory", "256m"),
        ("-m256m", "memory", "256m"),
        ("--cpus=1.5", "cpus", 1.5),
        ("--cpus 2", "cpus", 2.0),
        ("--cpuset-cpus=0,1", "cpuset", "0,1"),
        ("--cpuset-cpus 2-3", "cpuset", "2-3"),
        ("--hostname=box", "hostname", "box"),
        ("-h box", "hostname", "box"),
        ("-hbox", "hostname", "box"),
        ("--cap-add=NET_ADMIN", "cap_add", ["NET_ADMIN"]),
        ("--cap-add NET_ADMIN --cap-add=SYS_TIME", "cap_add", ["NET_ADMIN", "SYS_TIME"]),
        ("--cap-add=NET_ADMIN --cap-add=NET_ADMIN", "cap_add", ["NET_ADMIN"]),
        ("--device=/dev/dri:/dev/dri", "devices", ["/dev/dri:/dev/dri"]),
        ("--device /dev/ttyUSB0", "devices", ["/dev/ttyUSB0"]),
        ("-e A=1", "env", {"A": "1"}),
        ("-eA=1", "env", {"A": "1"}),
        ("--env=A=1 --env B=two=2", "env", {"A": "1", "B": "two=2"}),
        ('-e "MSG=hello world"', "env", {"MSG": "hello world"}),
        ("-l k=v", "labels", {"k": "v"}),
        ("--label=a.b/c=d --label flag", "labels", {"a.b/c": "d", "flag": ""}),
        ("--log-opt max-size=10m --log-opt=max-file=3", "log_opts", {"max-size": "10m", "max-file": "3"}),
        ("--network=mynet", "networks", ["mynet"]),
        ("--network a --network b --network a", "networks", ["a", "b"]),
        ("", "restart", None),
        ("   ", "warnings", []),
    ],
)
def test_supported_flags(text: str, attr: str, expected: object) -> None:
    result = parse_extra_params(text)
    assert _attr(result, attr) == expected
    assert result.warnings == []


def test_later_value_wins() -> None:
    assert parse_extra_params("--memory=1g --memory=2g").memory == "2g"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("--sysctl net.ipv4.ip_forward=1", "--sysctl net.ipv4.ip_forward=1"),
        ("--sysctl=net.ipv4.ip_forward=1", "--sysctl=net.ipv4.ip_forward=1"),
        ("--init", "--init"),
        ("--read-only", "--read-only"),
        ("-it", "-it"),
        ("--add-host=foo:10.0.0.1", "--add-host"),
        ("--add-host foo:10.0.0.1", "--add-host"),
        ("stray", "unexpected argument 'stray'"),
        ("--memory", "missing its value"),
        ("--memory=lots", "invalid memory"),
        ("--cpus=abc", "invalid --cpus"),
        ("--cpus=-1", "invalid --cpus"),
        ("-e NOEQUALS", "needs KEY=VALUE"),
        ("--log-opt bad", "needs KEY=VALUE"),
        ("--hostname=", "empty value"),
    ],
)
def test_warnings(text: str, fragment: str) -> None:
    result = parse_extra_params(text)
    assert len(result.warnings) == 1
    assert fragment in result.warnings[0]


def test_unsupported_does_not_swallow_following_flag() -> None:
    result = parse_extra_params("--init --memory=1g")
    assert result.memory == "1g"
    assert len(result.warnings) == 1


def test_mixed_real_world_string() -> None:
    result = parse_extra_params(
        "--memory=1g --cpus 1.5 --hostname photosync --cap-add=SYS_ADMIN "
        "--log-opt max-size=10m --sysctl net.ipv4.ip_forward=1"
    )
    assert (result.memory, result.cpus, result.hostname) == ("1g", 1.5, "photosync")
    assert result.cap_add == ["SYS_ADMIN"]
    assert result.log_opts == {"max-size": "10m"}
    assert len(result.warnings) == 1


def test_unbalanced_quotes_raise() -> None:
    with pytest.raises(ValidationError):
        parse_extra_params('--label "oops')
