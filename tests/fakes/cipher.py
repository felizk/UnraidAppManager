"""Field ciphers for tests."""

from __future__ import annotations


class PlainCipher:
    """No-op cipher."""

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes) -> bytes:
        return ciphertext


class XorCipher:
    """Reversible scrambling so tests can check that a column is not stored in plain text."""

    def __init__(self, key: int = 0x5A) -> None:
        self.key = key

    def encrypt(self, plaintext: bytes) -> bytes:
        return bytes(b ^ self.key for b in plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return bytes(b ^ self.key for b in ciphertext)
