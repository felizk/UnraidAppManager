"""Tests for FernetVault, FernetFieldCipher and key loading."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from uam.contracts import FieldCipher, Vault
from uam.db import Database
from uam.db.models import AppRow, CredentialRow, SourceRow
from uam.domain import AppState, CredentialKind
from uam.errors import AuthError, ConflictError, NotFound
from uam.vault import FernetFieldCipher, FernetVault, build_fernet, load_keys
from uam.vault.vault import registry_host_of


def make_vault(db: Database, key: bytes | None = None) -> FernetVault:
    return FernetVault(db.session_factory, build_fernet([key or Fernet.generate_key()]))


async def test_round_trip_and_protocols(tmp_db: Database) -> None:
    vault = make_vault(tmp_db)
    assert isinstance(vault, Vault)
    assert isinstance(FernetFieldCipher(vault._fernet), FieldCipher)
    cred = await vault.store("gh", CredentialKind.GITHUB_PAT, "me", "s3cret")
    assert await vault.reveal(cred.id) == "s3cret"
    assert await vault.get(cred.id) == cred
    assert [c.id for c in await vault.list()] == [cred.id]
    assert "s3cret" not in repr(await vault.list())
    await vault.update_secret(cred.id, "new")
    assert await vault.reveal(cred.id) == "new"
    await vault.update_username(cred.id, "someone-else")
    assert (await vault.get(cred.id)).username == "someone-else"
    assert await vault.reveal(cred.id) == "new"
    await vault.delete(cred.id)
    with pytest.raises(NotFound):
        await vault.get(cred.id)


async def test_unknown_and_duplicate(tmp_db: Database) -> None:
    vault = make_vault(tmp_db)
    with pytest.raises(NotFound):
        await vault.reveal(99)
    with pytest.raises(NotFound):
        await vault.update_secret(99, "x")
    with pytest.raises(NotFound):
        await vault.delete(99)
    await vault.store("a", CredentialKind.NPM, "u", "x")
    with pytest.raises(ConflictError):
        await vault.store("a", CredentialKind.NPM, "u", "y")


async def test_wrong_key(tmp_db: Database) -> None:
    cred = await make_vault(tmp_db).store("a", CredentialKind.NPM, "u", "x")
    other = make_vault(tmp_db)
    with pytest.raises(AuthError, match="cannot decrypt"):
        await other.reveal(cred.id)


def test_cipher_round_trip_and_wrong_key() -> None:
    cipher = FernetFieldCipher(build_fernet([Fernet.generate_key()]))
    token = cipher.encrypt(b"{}")
    assert cipher.decrypt(token) == b"{}"
    with pytest.raises(AuthError, match="cannot decrypt"):
        FernetFieldCipher(build_fernet([Fernet.generate_key()])).decrypt(token)


async def test_rotation(tmp_db: Database) -> None:
    old, new = Fernet.generate_key(), Fernet.generate_key()
    old_fernet = build_fernet([old])
    vault_old = FernetVault(tmp_db.session_factory, old_fernet)
    cred = await vault_old.store("a", CredentialKind.REGISTRY, "u", "pw", "ghcr.io")
    async with tmp_db.session_factory() as s:
        src = SourceRow(owner="o", repo="r")
        s.add(src)
        await s.flush()
        s.add(
            AppRow(
                source_id=src.id,
                template_path="t.xml",
                name="app",
                overrides_ciphertext=FernetFieldCipher(old_fernet).encrypt(b'{"a": "b"}'),
                state=AppState.RUNNING.value,
            )
        )
        await s.commit()

    rotating = build_fernet([new, old])
    vault = FernetVault(tmp_db.session_factory, rotating)
    assert await vault.reveal(cred.id) == "pw"  # old key still readable
    assert await vault.rotate() == 2

    only_new = build_fernet([new])
    assert await FernetVault(tmp_db.session_factory, only_new).reveal(cred.id) == "pw"
    async with tmp_db.session_factory() as s:
        app = await s.get(AppRow, 1)
        assert app is not None
        assert FernetFieldCipher(only_new).decrypt(app.overrides_ciphertext) == b'{"a": "b"}'
        row = await s.get(CredentialRow, cred.id)
        assert row is not None
        with pytest.raises(AuthError):
            FernetFieldCipher(old_fernet).decrypt(row.secret_ciphertext)


@pytest.mark.parametrize(
    ("ref", "host"),
    [
        ("nginx", "docker.io"),
        ("nginx:1.25", "docker.io"),
        ("library/nginx", "docker.io"),
        ("user/app:tag", "docker.io"),
        ("ghcr.io/a/b:1", "ghcr.io"),
        ("registry:5000/x", "registry:5000"),
        ("localhost/x", "localhost"),
        ("docker.io/library/x@sha256:" + "a" * 64, "docker.io"),
        ("index.docker.io/library/x", "docker.io"),
        ("GHCR.io/a/b", "ghcr.io"),
        ("x@sha256:" + "a" * 64, "docker.io"),
    ],
)
def test_host_parsing(ref: str, host: str) -> None:
    assert registry_host_of(ref) == host


async def test_registry_auth(tmp_db: Database) -> None:
    vault = make_vault(tmp_db)
    assert await vault.registry_auth_for("nginx") is None
    await vault.store("hub", CredentialKind.REGISTRY, "hubuser", "hubpw", "docker.io")
    await vault.store("priv", CredentialKind.REGISTRY, "pu", "ppw", "registry:5000")
    auth = await vault.registry_auth_for("library/nginx")
    assert auth is not None
    assert (auth.username, auth.password, auth.registry) == ("hubuser", "hubpw", "docker.io")
    auth = await vault.registry_auth_for("registry:5000/x:1")
    assert auth is not None
    assert auth.password == "ppw"
    assert await vault.registry_auth_for("quay.io/x/y") is None


async def test_ghcr_fallback(tmp_db: Database) -> None:
    vault = make_vault(tmp_db)
    assert await vault.registry_auth_for("ghcr.io/a/b") is None
    await vault.store("npm", CredentialKind.NPM, "n", "npmpw")
    await vault.store("pat1", CredentialKind.GITHUB_PAT, "octo", "tok1")
    await vault.store("pat2", CredentialKind.GITHUB_PAT, "other", "tok2")
    auth = await vault.registry_auth_for("ghcr.io/a/b:1")
    assert auth is not None
    assert (auth.username, auth.password, auth.registry) == ("octo", "tok1", "ghcr.io")
    await vault.store("reg", CredentialKind.REGISTRY, "ru", "rpw", "ghcr.io")
    auth = await vault.registry_auth_for("ghcr.io/a/b:1")
    assert auth is not None
    assert auth.username == "ru"
    assert await vault.registry_auth_for("nginx") is None  # PAT never used for docker.io


def test_keys_generate_file(tmp_path: Path) -> None:
    keys = load_keys(tmp_path / "data", {})
    path = tmp_path / "data" / "secret.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    Fernet(keys[0])
    assert load_keys(tmp_path / "data", {}) == keys


def test_keys_env_and_old(tmp_path: Path) -> None:
    a, b, c = (Fernet.generate_key().decode() for _ in range(3))
    keys = load_keys(tmp_path, {"UAM_SECRET_KEY": a, "UAM_SECRET_KEY_OLD": f"{b}, {c}"})
    assert keys == [a.encode(), b.encode(), c.encode()]
    assert not (tmp_path / "secret.key").exists()


async def test_ghcr_fallback_without_username_uses_placeholder(tmp_db: Database) -> None:
    """GHCR's token endpoint rejects an empty username (403) but accepts any other one."""
    vault = make_vault(tmp_db)
    await vault.store("gh", CredentialKind.GITHUB_PAT, "", "ghp_x")
    auth = await vault.registry_auth_for("ghcr.io/me/app:latest")
    assert auth is not None and auth.username == "uam" and auth.password == "ghp_x"
