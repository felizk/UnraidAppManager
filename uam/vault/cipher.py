"""Fernet implementation of `uam.contracts.FieldCipher`."""

from __future__ import annotations

from cryptography.fernet import InvalidToken, MultiFernet

from uam.errors import AuthError


class FernetFieldCipher:
    """Encrypts secret columns with the same keys as the vault."""

    def __init__(self, fernet: MultiFernet) -> None:
        self._fernet = fernet

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt with the primary key."""
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt with any known key; `AuthError` if none fits."""
        try:
            return self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise AuthError("cannot decrypt") from exc
