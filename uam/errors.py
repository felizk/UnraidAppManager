"""Error types shared by every UAM module."""

from __future__ import annotations

from datetime import datetime


class UamError(Exception):
    """Base class for all UAM errors."""


class NotFound(UamError):
    pass


class ConflictError(UamError):
    pass


class AuthError(UamError):
    pass


class ValidationError(UamError):
    pass


class TemplateParseError(UamError):
    pass


class PortRangeExhausted(UamError):
    pass


class RateLimited(UamError):
    """A remote API refused the call until `reset_at`."""

    def __init__(self, reset_at: datetime, message: str = "rate limited") -> None:
        super().__init__(f"{message} until {reset_at.isoformat()}")
        self.reset_at = reset_at


class RemoteError(UamError):
    """A remote API returned an unexpected status."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"remote error {status}: {body[:200]}")
        self.status = status
        self.body = body


class DeployError(UamError):
    """A deployment stage failed; `stage` names it (pull, create_new, health, …)."""

    def __init__(self, stage: str, cause: BaseException | str) -> None:
        super().__init__(f"{stage} failed: {cause}")
        self.stage = stage
        self.cause = cause
