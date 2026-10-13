"""Credential vault backed by the `credentials` table."""

from __future__ import annotations

from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from uam.db.models import AppRow, CredentialRow
from uam.domain import Credential, CredentialKind, RegistryAuth
from uam.errors import AuthError, ConflictError, NotFound

SessionFactory = async_sessionmaker[AsyncSession]

DEFAULT_HOST = "docker.io"
_DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}


def normalize_host(host: str) -> str:
    """Lowercase a registry host, dropping scheme and path, mapping Docker Hub aliases to docker.io."""
    h = host.strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0]
    return DEFAULT_HOST if h in _DOCKER_HUB_ALIASES or not h else h


def registry_host_of(image_ref: str) -> str:
    """Registry host of an image reference; `docker.io` when the reference names none."""
    ref = image_ref.strip().split("@", 1)[0]
    first, sep, _rest = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return normalize_host(first)
    return DEFAULT_HOST


def _to_credential(row: CredentialRow) -> Credential:
    return Credential(
        id=row.id,
        name=row.name,
        kind=CredentialKind(row.kind),
        username=row.username,
        registry_host=row.registry_host,
    )


class FernetVault:
    """Stores credentials encrypted with Fernet. Secrets are only returned by `reveal`."""

    def __init__(self, session_factory: SessionFactory, fernet: MultiFernet) -> None:
        self._sf = session_factory
        self._fernet = fernet

    def _decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken as exc:
            raise AuthError("cannot decrypt") from exc

    async def _row(self, session: AsyncSession, credential_id: int) -> CredentialRow:
        row = await session.get(CredentialRow, credential_id)
        if row is None:
            raise NotFound(f"credential {credential_id} not found")
        return row

    async def store(
        self, name: str, kind: CredentialKind, username: str, secret: str, registry_host: str = ""
    ) -> Credential:
        """Encrypt and save a new credential."""
        row = CredentialRow(
            name=name,
            kind=kind.value,
            username=username,
            registry_host=registry_host,
            secret_ciphertext=self._fernet.encrypt(secret.encode()),
        )
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise ConflictError(f"credential named {name!r} already exists") from exc
            return _to_credential(row)

    async def update_secret(self, credential_id: int, secret: str) -> None:
        """Replace the secret of an existing credential."""
        async with self._sf() as session:
            row = await self._row(session, credential_id)
            row.secret_ciphertext = self._fernet.encrypt(secret.encode())
            await session.commit()

    async def update_username(self, credential_id: int, username: str) -> None:
        """Replace the username (GitHub user, registry user or NPM e-mail)."""
        async with self._sf() as session:
            row = await self._row(session, credential_id)
            row.username = username
            await session.commit()

    async def reveal(self, credential_id: int) -> str:
        """Return the decrypted secret."""
        async with self._sf() as session:
            row = await self._row(session, credential_id)
            return self._decrypt(row.secret_ciphertext)

    async def get(self, credential_id: int) -> Credential:
        """Credential metadata (no secret)."""
        async with self._sf() as session:
            return _to_credential(await self._row(session, credential_id))

    async def delete(self, credential_id: int) -> None:
        """Delete a credential."""
        async with self._sf() as session:
            await session.delete(await self._row(session, credential_id))
            await session.commit()

    async def registry_auth_for(self, image_ref: str) -> RegistryAuth | None:
        """Match the image host to a REGISTRY credential; ghcr.io falls back to the first GITHUB_PAT."""
        host = registry_host_of(image_ref)
        async with self._sf() as session:
            rows = (await session.scalars(select(CredentialRow).order_by(CredentialRow.id))).all()
            match = next(
                (
                    r
                    for r in rows
                    if r.kind == CredentialKind.REGISTRY.value and normalize_host(r.registry_host) == host
                ),
                None,
            )
            if match is None and host == "ghcr.io":
                match = next((r for r in rows if r.kind == CredentialKind.GITHUB_PAT.value), None)
            if match is None:
                return None
            return RegistryAuth(
                username=match.username, password=self._decrypt(match.secret_ciphertext), registry=host
            )

    async def rotate(self) -> int:
        """Re-encrypt every credential secret and app override with the primary key. Returns rows touched."""
        count = 0
        async with self._sf() as session:
            for cred in (await session.scalars(select(CredentialRow))).all():
                cred.secret_ciphertext = self._reencrypt(cred.secret_ciphertext)
                count += 1
            for app in (await session.scalars(select(AppRow))).all():
                app.overrides_ciphertext = self._reencrypt(app.overrides_ciphertext)
                count += 1
            await session.commit()
        return count

    def _reencrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.rotate(token)
        except InvalidToken as exc:
            raise AuthError("cannot decrypt") from exc

    async def list(self) -> list[Credential]:
        """All credentials, without secrets."""
        async with self._sf() as session:
            rows = (await session.scalars(select(CredentialRow).order_by(CredentialRow.id))).all()
            return [_to_credential(r) for r in rows]
