"""Parser for the Unraid template `ExtraParams` field (a subset of `docker run` flags)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from uam.errors import ValidationError

_MEMORY_RE = re.compile(r"^\d+(\.\d+)?[bkmgBKMG]?$")

# Short flag -> long name. A short flag may carry its value attached (`-m512m`).
_SHORT = {"-m": "--memory", "-h": "--hostname", "-e": "--env", "-l": "--label"}
_VALUE_FLAGS = {
    "--restart",
    "--memory",
    "--cpus",
    "--cpuset-cpus",
    "--hostname",
    "--cap-add",
    "--device",
    "--env",
    "--label",
    "--log-opt",
    "--network",
    "--add-host",
}
# Flags without a value, so the following token must not be swallowed as one.
_BOOLEAN_FLAGS = {
    "--init",
    "--read-only",
    "--privileged",
    "--rm",
    "--tty",
    "--interactive",
    "-t",
    "-i",
    "-it",
    "-d",
}


@dataclass(slots=True)
class ExtraParams:
    """Result of parsing an `ExtraParams` string."""

    restart: str | None = None
    memory: str | None = None
    cpus: float | None = None
    cpuset: str | None = None
    hostname: str | None = None
    cap_add: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    log_opts: dict[str, str] = field(default_factory=dict)
    networks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_extra_params(text: str) -> ExtraParams:
    """Parse `ExtraParams`; unsupported or malformed flags become entries in `warnings`.

    Raises `ValidationError` only when the string cannot be tokenised (unbalanced quotes).
    """
    result = ExtraParams()
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise ValidationError(f"ExtraParams cannot be parsed: {e}") from e

    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        flag, value = token, None
        if token.startswith("--"):
            if "=" in token:
                flag, value = token.split("=", 1)
        elif token.startswith("-") and len(token) > 1:
            short = token[:2]
            if short in _SHORT and len(token) > 2:
                flag, value = short, token[2:].removeprefix("=")
        else:
            result.warnings.append(f"ExtraParams: unexpected argument '{token}' ignored")
            continue

        flag = _SHORT.get(flag, flag)
        if flag not in _VALUE_FLAGS:
            unsupported = token
            if (
                value is None
                and flag not in _BOOLEAN_FLAGS
                and i < len(tokens)
                and not tokens[i].startswith("-")
            ):
                unsupported = f"{token} {tokens[i]}"
                i += 1
            result.warnings.append(f"ExtraParams: unsupported flag '{unsupported}' ignored")
            continue

        if value is None:
            if i >= len(tokens):
                result.warnings.append(f"ExtraParams: flag '{token}' is missing its value")
                continue
            value = tokens[i]
            i += 1
        _apply(result, flag, value)
    return result


def _apply(result: ExtraParams, flag: str, value: str) -> None:
    warn = result.warnings.append
    if not value:
        warn(f"ExtraParams: flag '{flag}' has an empty value")
        return
    key, sep, val = value.partition("=")
    match flag:
        case "--restart":
            result.restart = value
        case "--memory":
            if _MEMORY_RE.match(value):
                result.memory = value
            else:
                warn(f"ExtraParams: invalid memory limit '{value}' ignored")
        case "--cpus":
            try:
                cpus = float(value)
            except ValueError:
                cpus = 0.0
            if cpus > 0:
                result.cpus = cpus
            else:
                warn(f"ExtraParams: invalid --cpus value '{value}' ignored")
        case "--cpuset-cpus":
            result.cpuset = value
        case "--hostname":
            result.hostname = value
        case "--cap-add":
            if value not in result.cap_add:
                result.cap_add.append(value)
        case "--device":
            if value not in result.devices:
                result.devices.append(value)
        case "--env":
            if key and sep:
                result.env[key] = val
            else:
                warn(f"ExtraParams: --env '{value}' needs KEY=VALUE, ignored")
        case "--label":
            if key:
                result.labels[key] = val
            else:
                warn(f"ExtraParams: invalid --label '{value}' ignored")
        case "--log-opt":
            if key and sep:
                result.log_opts[key] = val
            else:
                warn(f"ExtraParams: --log-opt '{value}' needs KEY=VALUE, ignored")
        case "--network":
            if value not in result.networks:
                result.networks.append(value)
        case "--add-host":
            warn(f"ExtraParams: --add-host '{value}' is not supported and was ignored")
