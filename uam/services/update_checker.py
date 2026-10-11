"""Tiered update checks (template commit, image digest) and automatic updates."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from uam.contracts import (
    AppRepository,
    EventLog,
    GitHubClient,
    RegistryClient,
    SettingsStore,
    SourceRepository,
    TemplateCodec,
    Vault,
)
from uam.domain import App, AppState, Source, TemplateConfig, UnraidTemplate, UpdateCheckResult
from uam.errors import RateLimited

log = logging.getLogger(__name__)

RATE_LIMIT_LOW_FRACTION = 0.1
JITTER_LOW, JITTER_HIGH = 0.9, 1.1


class Updater(Protocol):
    """The slice of the orchestrator that the checker needs."""

    async def update(self, app_id: int, apply_template: bool, apply_image: bool) -> App: ...


@dataclass(frozen=True, slots=True)
class TemplateDiff:
    """Structural difference between two templates. Config entries are override keys."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    old_image: str | None = None
    new_image: str | None = None
    new_required: tuple[str, ...] = ()  # added/changed configs that are required, empty and unset

    @property
    def image_changed(self) -> bool:
        """True when the template's repository differs."""
        return self.old_image != self.new_image


def config_key(c: TemplateConfig) -> str:
    """Override key of a config: `<Type>:<Target>`, or `Port:<Target>/<mode>` for ports."""
    if c.type == "Port":
        return f"Port:{c.target}/{c.mode or 'tcp'}"
    return f"{c.type}:{c.target}"


def diff_templates(
    old: UnraidTemplate | None, new: UnraidTemplate, overrides: dict[str, str] | None = None
) -> TemplateDiff:
    """Compare templates. With `old=None` every config counts as added."""
    overrides = overrides or {}
    old_cfg = {config_key(c): c for c in old.configs} if old else {}
    new_cfg = {config_key(c): c for c in new.configs}
    added = tuple(k for k in new_cfg if k not in old_cfg)
    removed = tuple(k for k in old_cfg if k not in new_cfg)
    changed = tuple(k for k in new_cfg if k in old_cfg and new_cfg[k] != old_cfg[k])

    def needs_input(k: str) -> bool:
        c = new_cfg[k]
        if not c.required or c.default or k in overrides:
            return False
        return k in added or not old_cfg[k].required

    required = tuple(k for k in (*added, *changed) if needs_input(k))
    return TemplateDiff(added, removed, changed, old.repository if old else None, new.repository, required)


def resolve_image(repository: str, tag_override: str | None) -> str:
    """The template's repository with `tag_override` replacing any tag or digest."""
    if not tag_override:
        return repository
    ref = repository.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]
    return f"{ref}:{tag_override}"


def _default_jitter() -> float:
    return random.uniform(JITTER_LOW, JITTER_HIGH)  # noqa: S311 - scheduling jitter, not security


class UpdateChecker:
    """Checks one app for template and image updates, persists the result and schedules the next check."""

    def __init__(
        self,
        apps: AppRepository,
        sources: SourceRepository,
        github: GitHubClient,
        registry: RegistryClient,
        vault: Vault,
        codec: TemplateCodec,
        settings: SettingsStore,
        updater: Updater,
        events: EventLog,
        clock: Callable[[], datetime],
        jitter: Callable[[], float] = _default_jitter,
    ) -> None:
        self._apps = apps
        self._sources = sources
        self._github = github
        self._registry = registry
        self._vault = vault
        self._codec = codec
        self._settings = settings
        self._updater = updater
        self._events = events
        self._clock = clock
        self._jitter = jitter
        self._templates: dict[tuple[int, str, str], UnraidTemplate] = {}

    async def check_now(self, app_id: int) -> UpdateCheckResult:
        """Run one check immediately (used by the UI)."""
        return await self.check(await self._apps.get(app_id))

    async def check(self, app: App) -> UpdateCheckResult:
        """Check `app`, persist the outcome, and auto-apply updates where the app allows it."""
        now = self._clock()
        settings = await self._settings.get()
        errors: list[str] = []
        rate_reset: datetime | None = None
        attention = app.needs_attention
        new_commit: str | None = None
        template: UnraidTemplate | None = None
        pending_commit = app.available_template_commit
        pending_image = app.available_image_digest
        source = await self._sources.get(app.source_id)

        try:
            token = await self._token(source)
            latest = await self._github.latest_commit_for_path(source, app.template_path, token)
            if latest is not None and latest[0] != app.installed_template_commit:
                new_commit = latest[0]
                template = await self._fetch(source, app.template_path, new_commit, token, at_ref=False)
                old = await self._installed_template(app, source, token)
                diff = diff_templates(old, template, app.overrides)
                attention = (
                    "New required setting(s) with no default: " + ", ".join(diff.new_required)
                    if diff.new_required
                    else None
                )
                pending_commit = new_commit
            elif latest is not None:
                pending_commit = None
                attention = None
        except RateLimited as e:
            rate_reset = e.reset_at
        except Exception as e:
            log.warning("template check failed for %s: %s", app.name, e)
            errors.append(f"template check failed: {e}")

        new_digest: str | None = None
        try:
            base = template or await self._installed_template(app, source, await self._token(source))
            if base is not None:
                image = resolve_image(base.repository, app.image_tag_override)
                new_digest = await self._registry.get_digest(
                    image, await self._vault.registry_auth_for(image)
                )
                pending_image = new_digest if new_digest != app.installed_image_digest else None
        except RateLimited as e:
            rate_reset = rate_reset or e.reset_at
        except Exception as e:
            log.warning("image check failed for %s: %s", app.name, e)
            errors.append(f"image check failed: {e}")

        template_changed = pending_commit is not None and pending_commit != app.installed_template_commit
        image_changed = pending_image is not None and pending_image != app.installed_image_digest
        newly = (new_commit is not None and new_commit != app.available_template_commit) or (
            new_digest is not None
            and pending_image is not None
            and pending_image != app.available_image_digest
        )

        app.available_template_commit = pending_commit if template_changed else None
        app.available_image_digest = pending_image if image_changed else None
        app.needs_attention = attention if template_changed else None
        app.last_checked_at = now
        if newly:
            app.last_change_at = now
        app.last_error = "; ".join(errors) if errors else None
        hot_window = timedelta(hours=settings.hot_window_hours)
        hot = app.last_change_at is not None and now - app.last_change_at <= hot_window
        if rate_reset is not None:
            app.next_check_at = rate_reset + timedelta(minutes=1)
        else:
            minutes = float(settings.hot_interval_minutes if hot else settings.cold_interval_hours * 60)
            if hot and await self._rate_limit_low():
                minutes *= 2
            app.next_check_at = now + timedelta(minutes=minutes * self._jitter())
        await self._apps.save(app)

        if newly:
            what = " and ".join(n for n, c in (("template", template_changed), ("image", image_changed)) if c)
            await self._record(app, "update_available", f"Update available ({what})")

        result = UpdateCheckResult(
            app_id=app.id,
            template_changed=template_changed,
            new_template_commit=app.available_template_commit,
            image_changed=image_changed,
            new_image_digest=app.available_image_digest,
            needs_attention=app.needs_attention,
            checked_at=now,
        )
        await self._auto_apply(app, result, newly)
        return result

    async def _auto_apply(self, app: App, result: UpdateCheckResult, newly: bool) -> None:
        # After a rolled-back update, wait for a new commit/digest instead of retrying the same one forever.
        if app.state == AppState.FAILED_UPDATE and not newly:
            return
        apply_image = result.image_changed and app.auto_update_image
        apply_template = result.template_changed and app.auto_update_template and not result.needs_attention
        if not (apply_image or apply_template):
            return
        try:
            await self._updater.update(app.id, apply_template, apply_image)
            await self._record(app, "auto_update", "Automatic update applied")
        except Exception as e:
            log.warning("auto update failed for %s: %s", app.name, e)
            try:
                fresh = await self._apps.get(app.id)
                fresh.last_error = f"auto update failed: {e}"
                await self._apps.save(fresh)
            except Exception:
                log.exception("could not record auto update failure for %s", app.name)
            await self._record(app, "auto_update_failed", f"Automatic update failed: {e}", "error")

    async def _record(self, app: App, kind: str, message: str, level: str = "info") -> None:
        try:
            await self._events.record(app.id, kind, message, level)
        except Exception:
            log.exception("could not record event for %s", app.name)

    async def _rate_limit_low(self) -> bool:
        try:
            return await self._github.rate_limit_remaining_fraction() < RATE_LIMIT_LOW_FRACTION
        except Exception:
            return False

    async def _token(self, source: Source) -> str | None:
        if source.credential_id is None:
            return None
        return await self._vault.reveal(source.credential_id)

    async def _fetch(
        self, source: Source, path: str, commit: str, token: str | None, at_ref: bool = True
    ) -> UnraidTemplate:
        """Parse the template for `commit`: at that commit, or (`at_ref=False`) at the source's own ref."""
        key = (source.id, path, commit)
        cached = self._templates.get(key)
        if cached is not None:
            return cached
        at_commit = replace(source, ref=commit) if at_ref else source
        tpl = self._codec.parse((await self._github.get_template(at_commit, path, token)).xml)
        self._templates[key] = tpl
        return tpl

    async def _installed_template(self, app: App, source: Source, token: str | None) -> UnraidTemplate | None:
        """The template as installed: fetched at the installed commit (cached), None if unavailable."""
        if app.installed_template_commit is None:
            return None
        try:
            return await self._fetch(source, app.template_path, app.installed_template_commit, token)
        except RateLimited:
            raise
        except Exception as e:
            log.info("installed template for %s unavailable: %s", app.name, e)
            return None
