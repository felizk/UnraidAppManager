"""Move a source to another repo (typically a GitHub repo to its Forgejo mirror), keeping its apps."""

from __future__ import annotations

from dataclasses import dataclass, replace

from uam.contracts import AppRepository, GitHubClient, SourceRepository, Vault
from uam.domain import App, Source
from uam.errors import ConflictError, UamError, ValidationError


@dataclass(frozen=True, slots=True)
class MoveCheck:
    """What one template path looks like at the target repo."""

    template_path: str
    app_names: tuple[str, ...]
    ok: bool
    detail: str
    warning: bool = False


@dataclass(frozen=True, slots=True)
class MovePlan:
    """The checks for moving `source` to `target`; `ok` when every template was found."""

    source: Source
    target: Source
    checks: tuple[MoveCheck, ...]

    @property
    def ok(self) -> bool:
        """True when the move can be applied without breaking an app."""
        return all(c.ok for c in self.checks)


def _short(sha: str | None) -> str:
    return (sha or "?")[:10]


class SourceMover:
    """Checks a move against the target repo, then rewrites the source in place.

    Apps keep their source id, overrides and installed commit. When the target is a mirror
    (same history) nothing else changes; otherwise the target's template is offered as an
    ordinary template update on the next check.
    """

    def __init__(
        self, sources: SourceRepository, apps: AppRepository, github: GitHubClient, vault: Vault
    ) -> None:
        self._sources = sources
        self._apps = apps
        self._github = github
        self._vault = vault

    async def plan(self, source_id: int, target: Source) -> MovePlan:
        """Check that every template the source's apps use exists at `target`."""
        source = await self._sources.get(source_id)
        target = replace(target, id=source.id)
        if (target.forge_url, target.owner, target.repo) == (source.forge_url, source.owner, source.repo):
            raise ValidationError(f"{source.display_name} is already at that location")
        if target.credential_id is not None:
            await self._vault.get(target.credential_id)
        token = await self._vault.reveal(target.credential_id) if target.credential_id is not None else None
        apps = [a for a in await self._apps.list() if a.source_id == source.id]
        by_path: dict[str, list[App]] = {}
        for app in apps:
            by_path.setdefault(app.template_path, []).append(app)
        if not by_path:
            return MovePlan(source, target, (await self._check_discovery(target, token),))
        checks = [
            await self._check_path(target, path, group, token) for path, group in sorted(by_path.items())
        ]
        return MovePlan(source, target, tuple(checks))

    async def move(self, source_id: int, target: Source) -> MovePlan:
        """Check, then apply the move; raises ConflictError if a template is missing at the target."""
        plan = await self.plan(source_id, target)
        if not plan.ok:
            failed = ", ".join(c.template_path for c in plan.checks if not c.ok)
            raise ConflictError(f"not moving {plan.source.display_name}: {failed} not found at the target")
        await self._sources.update(plan.target)
        return plan

    async def _check_discovery(self, target: Source, token: str | None) -> MoveCheck:
        label = target.template_path or "(auto-discover)"
        try:
            found = await self._github.discover_templates(target, token)
        except UamError as exc:
            return MoveCheck(label, (), False, str(exc))
        return MoveCheck(label, (), True, f"{len(found)} template(s) found; no apps use this source")

    async def _check_path(self, target: Source, path: str, apps: list[App], token: str | None) -> MoveCheck:
        names = tuple(a.name for a in apps)
        try:
            current = await self._github.get_template(target, path, token)
        except UamError as exc:
            return MoveCheck(path, names, False, str(exc))
        installed = {a.installed_template_commit for a in apps if a.installed_template_commit}
        if not installed or installed == {current.commit_sha}:
            return MoveCheck(path, names, True, f"found at {_short(current.commit_sha)}, same as installed")
        missing: list[str] = []
        for sha in sorted(installed - {current.commit_sha}):
            try:
                await self._github.get_template(replace(target, ref=sha), path, token)
            except UamError:
                missing.append(sha)
        if not missing:
            return MoveCheck(
                path,
                names,
                True,
                f"found at {_short(current.commit_sha)}; installed commit is in its history",
            )
        return MoveCheck(
            path,
            names,
            True,
            f"found at {_short(current.commit_sha)}, but installed commit {_short(missing[0])} is not in "
            "the target's history; the target's template will be offered as an update",
            warning=True,
        )
