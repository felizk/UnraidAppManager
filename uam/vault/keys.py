"""Loading of the Fernet encryption keys."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet

KEY_ENV = "UAM_SECRET_KEY"
OLD_KEYS_ENV = "UAM_SECRET_KEY_OLD"
KEY_FILE = "secret.key"


def load_keys(data_dir: Path, env: Mapping[str, str] | None = None) -> list[bytes]:
    """Return the keys to use, primary first.

    The primary key is `UAM_SECRET_KEY` if set, else `<data_dir>/secret.key`, which is
    generated with mode 0600 when missing. `UAM_SECRET_KEY_OLD` appends older keys.
    """
    env = os.environ if env is None else env
    primary = env.get(KEY_ENV, "").strip()
    if primary:
        key = primary.encode()
    else:
        path = data_dir / KEY_FILE
        if path.exists():
            key = path.read_bytes().strip()
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
    old = [k.strip().encode() for k in env.get(OLD_KEYS_ENV, "").split(",") if k.strip()]
    return [key, *old]


def build_fernet(keys: list[bytes]) -> MultiFernet:
    """MultiFernet that encrypts with the first key and decrypts with any."""
    return MultiFernet([Fernet(k) for k in keys])


def load_fernet(data_dir: Path, env: Mapping[str, str] | None = None) -> MultiFernet:
    """Convenience: `build_fernet(load_keys(...))`."""
    return build_fernet(load_keys(data_dir, env))
