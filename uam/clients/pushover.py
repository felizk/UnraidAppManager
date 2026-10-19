"""Pushover notifier: sends app update and crash messages through api.pushover.net."""

from __future__ import annotations

import logging

import httpx

from uam.contracts import NotifyTopic, SettingsStore, Vault
from uam.errors import RemoteError, UamError, ValidationError
from uam.logging import redact

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TITLE_MAX = 250
MESSAGE_MAX = 1024


class PushoverNotifier:
    """Reads `Settings.pushover_credential_id` on every send, so a settings change needs no restart.

    The credential's username is the Pushover user (or group) key and its secret is the
    application API token.
    """

    def __init__(
        self, settings: SettingsStore, vault: Vault, http: httpx.AsyncClient, url: str = PUSHOVER_URL
    ) -> None:
        self._settings = settings
        self._vault = vault
        self._http = http
        self._url = url

    async def notify(self, topic: NotifyTopic, title: str, message: str, priority: int = 0) -> None:
        """Send if Pushover is configured and `topic` is enabled; failures are only logged."""
        try:
            s = await self._settings.get()
            enabled = s.notify_on_update if topic == "update" else s.notify_on_crash
            if s.pushover_credential_id is None or not enabled:
                return
            await self._send(s.pushover_credential_id, title, message, priority)
        except Exception as exc:
            log.warning("pushover notification failed: %s", redact(str(exc)))

    async def send_test(self, credential_id: int | None = None) -> None:
        """Send a test message with `credential_id`, or the saved credential when None."""
        if credential_id is None:
            credential_id = (await self._settings.get()).pushover_credential_id
        if credential_id is None:
            raise ValidationError("Pushover is not configured (choose a Pushover credential in Settings)")
        await self._send(
            credential_id, "Unraid App Manager", "Test notification: Pushover is set up correctly.", 0
        )

    async def _send(self, credential_id: int, title: str, message: str, priority: int) -> None:
        cred = await self._vault.get(credential_id)
        token = await self._vault.reveal(credential_id)
        if not cred.username:
            raise ValidationError("set the Pushover credential's username to your Pushover user key")
        data = {
            "token": token,
            "user": cred.username,
            "title": title[:TITLE_MAX],
            "message": message[:MESSAGE_MAX] or title[:MESSAGE_MAX],
            "priority": str(priority),
        }
        try:
            resp = await self._http.post(self._url, data=data)
        except httpx.HTTPError as exc:
            raise UamError(f"could not reach Pushover: {exc}") from exc
        if resp.status_code != 200:
            raise RemoteError(resp.status_code, _errors(resp))


def _errors(resp: httpx.Response) -> str:
    """Pushover's own error list (it never echoes the token), or the raw body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    errors = body.get("errors") if isinstance(body, dict) else None
    return "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else resp.text
