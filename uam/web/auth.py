"""Admin password, signed session cookies, optional bearer token and the CSRF header check."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from uam.contracts import KeyValueStore
from uam.errors import AuthError, ConflictError, ValidationError
from uam.web.deps import get_auth

PASSWORD_KEY = "admin_password_hash"  # noqa: S105
SECRET_KEY = "session_secret"  # noqa: S105
COOKIE_NAME = "uam_session"
SESSION_MAX_AGE = 7 * 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "uam"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MIN_PASSWORD_LENGTH = 8


class CsrfError(AuthError):
    """The `X-Requested-With: uam` header is missing on a mutating request (maps to 403)."""


class AuthManager:
    """Owns the admin password hash and session-signing secret (both kept in the KeyValueStore)."""

    def __init__(self, kv: KeyValueStore, api_token: str | None = None) -> None:
        self._kv = kv
        self._api_token = api_token or None
        self._hasher = PasswordHasher()
        self._secret: str | None = None
        self._lock = asyncio.Lock()

    async def is_setup(self) -> bool:
        """True once an admin password exists."""
        return await self._kv.get(PASSWORD_KEY) is not None

    async def set_password(self, password: str) -> None:
        """First-run setup. Raises ConflictError if a password already exists."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        async with self._lock:
            if await self.is_setup():
                raise ConflictError("admin password is already set")
            hashed = await asyncio.to_thread(self._hasher.hash, password)
            await self._kv.set(PASSWORD_KEY, hashed)

    async def verify_password(self, password: str) -> bool:
        """Check `password` against the stored argon2 hash."""
        stored = await self._kv.get(PASSWORD_KEY)
        if stored is None:
            return False
        try:
            return await asyncio.to_thread(self._hasher.verify, stored, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    async def _serializer(self) -> URLSafeTimedSerializer:
        async with self._lock:
            if self._secret is None:
                stored = await self._kv.get(SECRET_KEY)
                if stored is None:
                    stored = secrets.token_urlsafe(48)
                    await self._kv.set(SECRET_KEY, stored)
                self._secret = stored
            return URLSafeTimedSerializer(self._secret, salt="uam-session")

    async def issue_session(self) -> str:
        """A signed session cookie value."""
        return (await self._serializer()).dumps({"user": "admin"})

    async def session_valid(self, cookie: str | None) -> bool:
        """True if `cookie` carries a valid, unexpired signature."""
        if not cookie:
            return False
        try:
            (await self._serializer()).loads(cookie, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return False
        return True

    def bearer_valid(self, request: Request) -> bool:
        """True if `UAM_API_TOKEN` is set and the request presents it as a bearer token."""
        if not self._api_token:
            return False
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), self._api_token)


async def csrf_guard(request: Request, auth: Annotated[AuthManager, Depends(get_auth)]) -> None:
    """Require `X-Requested-With: uam` on mutating requests (bearer-token callers are exempt)."""
    if request.method in MUTATING and not auth.bearer_valid(request):
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise CsrfError("missing X-Requested-With: uam header")


async def require_session(request: Request, auth: Annotated[AuthManager, Depends(get_auth)]) -> None:
    """Allow a valid session cookie or the optional bearer token; otherwise AuthError (401)."""
    if auth.bearer_valid(request):
        return
    if await auth.session_valid(request.cookies.get(COOKIE_NAME)):
        return
    raise AuthError("authentication required")
