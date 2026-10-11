"""ExtraParams flag check. Mirrors the supported list in docs/app-repo-guide.md section 3."""

from __future__ import annotations

import shlex

SUPPORTED_FLAGS: frozenset[str] = frozenset(
    {
        "--restart",
        "--memory",
        "-m",
        "--cpus",
        "--cpuset-cpus",
        "--hostname",
        "-h",
        "--cap-add",
        "--device",
        "-e",
        "--env",
        "-l",
        "--label",
        "--log-opt",
        "--network",
    }
)


def unsupported_flags(extra_params: str) -> list[str]:
    """Return the flags in `extra_params` that UAM does not apply, in order of appearance."""
    try:
        tokens = shlex.split(extra_params)
    except ValueError:
        tokens = extra_params.split()
    bad: list[str] = []
    expect_value = False
    for tok in tokens:
        if expect_value:
            expect_value = False
            continue
        if not tok.startswith("-") or tok == "-":
            continue
        flag = tok.split("=", 1)[0]
        if flag not in SUPPORTED_FLAGS:
            bad.append(flag)
        elif "=" not in tok:
            expect_value = True
    return bad
