"""Deployment orchestrator: install, update, reconfigure, uninstall and start/stop flows."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime

from uam.contracts import (
    AppRepository,
    DockerEngine,
    EventLog,
    GitHubClient,
    PortAllocator,
    ProxyManager,
    SettingsStore,
    SourceRepository,
    SpecResolver,
    TemplateCodec,
    Vault,
)
from uam.domain import (
    LABEL_SOURCE,
    App,
    AppState,
    ContainerInfo,
    ContainerSpec,
    ProxySettings,
    ResolvedApp,
    UnraidTemplate,
)
from uam.errors import ConflictError, DeployError, NotFound, ValidationError
from uam.services.locks import AppLocks

OLD_SUFFIX = "__uam_old"
_INSTALLABLE = {AppState.NOT_INSTALLED, AppState.FAILED}
_OPERABLE = {AppState.RUNNING, AppState.STOPPED, AppState.FAILED_UPDATE}


def _repo_of(image: str) -> str:
    """Image reference without tag or digest."""
    ref = image.split("@", 1)[0]
    return ref[: ref.rfind(":")] if ref.rfind(":") > ref.rfind("/") else ref


def pinned_ref(image: str, digest: str) -> str:
    """`repo@sha256:...` for `image` and a stored digest (which may already be a full reference)."""
    return digest if "@" in digest else f"{_repo_of(image)}@{digest}"


class Orchestrator:
    """Runs every state-changing flow for apps, one operation per app at a time."""

    def __init__(
        self,
        apps: AppRepository,
        sources: SourceRepository,
        github: GitHubClient,
        codec: TemplateCodec,
        spec_resolver: SpecResolver,
        docker: DockerEngine,
        proxy: ProxyManager | None,
        ports: PortAllocator,
        vault: Vault,
        settings: SettingsStore,
        events: EventLog,
        clock: Callable[[], datetime],
        locks: AppLocks | None = None,
    ) -> None:
        self._apps = apps
        self._sources = sources
        self._github = github
        self._codec = codec
        self._resolver = spec_resolver
        self._docker = docker
        self._proxy = proxy
        self._ports = ports
        self._vault = vault
        self._settings = settings
        self._events = events
        self._clock = clock
        self.locks = locks or AppLocks()

    # ------------------------------------------------------------------ helpers

    async def _event(self, app_id: int | None, kind: str, message: str, level: str = "info") -> None:
        await self._events.record(app_id, kind, message, level)

    async def _fetch(self, app: App, at_commit: str | None = None) -> tuple[UnraidTemplate, str]:
        """Fetch and parse the app's template at the source head, or at `at_commit` when given.

        Returns (template, commit sha).
        """
        source = await self._sources.get(app.source_id)
        if at_commit is not None:
            source = replace(source, ref=at_commit)
        token = await self._vault.reveal(source.credential_id) if source.credential_id is not None else None
        found = await self._github.get_template(source, app.template_path, token)
        return self._codec.parse(found.xml), found.commit_sha

    async def _with_source_label(self, app: App, spec: ContainerSpec) -> ContainerSpec:
        """Add the `uam.source` label (the resolver doesn't know the source)."""
        source = await self._sources.get(app.source_id)
        return replace(spec, labels={**spec.labels, LABEL_SOURCE: f"{source.owner}/{source.repo}"})

    async def _find_container(self, app: App) -> ContainerInfo | None:
        """Locate the app's container by id first, then by name."""
        if app.container_id:
            info = await self._docker.inspect(app.container_id)
            if info is not None:
                return info
        return await self._docker.inspect(app.name)

    # ------------------------------------------------------------------ add

    async def add_app(
        self,
        source_id: int,
        template_path: str,
        name: str | None,
        overrides: dict[str, str],
        proxy: ProxySettings,
        auto_ports: bool = True,
    ) -> App:
        """Register an app from a source template. Validates the template; does not install."""
        source = await self._sources.get(source_id)
        token = await self._vault.reveal(source.credential_id) if source.credential_id is not None else None
        found = await self._github.get_template(source, template_path, token)
        template = self._codec.parse(found.xml)
        app = App(
            id=0,
            source_id=source_id,
            template_path=template_path,
            name=name or template.name,
            overrides=dict(overrides),
            image_tag_override=None,
            auto_ports=auto_ports,
            auto_update_image=True,
            auto_update_template=False,
            proxy=proxy,
            state=AppState.NOT_INSTALLED,
            installed_template_commit=None,
            installed_image_digest=None,
            container_id=None,
            available_template_commit=None,
            available_image_digest=None,
            needs_attention=None,
            last_change_at=None,
            last_checked_at=None,
            next_check_at=None,
            last_error=None,
        )
        app = await self._apps.add(app)
        await self._event(app.id, "app.added", f"added {app.name} from {source.owner}/{source.repo}")
        return app

    # ------------------------------------------------------------------ install

    async def install(self, app_id: int) -> App:
        """Pull, create, start and health-check the app's container, then register it in NPM."""
        async with self.locks.hold(app_id):
            app = await self._apps.get(app_id)
            if app.state not in _INSTALLABLE:
                raise ConflictError(f"cannot install an app in state {app.state.value}")
            first_install = app.installed_template_commit is None
            app.state = AppState.INSTALLING
            app.last_error = None
            await self._apps.save(app)
            await self._event(app.id, "install.start", f"installing {app.name}")

            stage = "template"
            new_id: str | None = None
            try:
                template, commit = await self._fetch(app)
                await self._event(app.id, "install.template", f"template at {commit[:12]}")
                stage = "resolve"
                resolved = await self._resolver.resolve(
                    replace(app, installed_template_commit=commit), template
                )
                spec = await self._with_source_label(app, resolved.spec)
                stage = "pull"
                digest = await self._pull(app.id, "install", spec.image)
                stage = "create"
                new_id = await self._docker.create(spec)
                await self._event(app.id, "install.create", f"created {spec.name}")
                stage = "start"
                await self._docker.start(new_id)
                stage = "health"
                await self._wait_healthy(app.id, "install", new_id)
            except Exception as exc:
                if new_id is not None:
                    try:
                        await self._docker.remove(new_id, force=True)
                    except Exception as cleanup_exc:
                        await self._event(
                            app.id, "install.cleanup", f"could not remove container: {cleanup_exc}", "warning"
                        )
                if first_install:
                    await self._ports.release(app.id)
                app.container_id = None
                app.state = AppState.FAILED
                app.last_error = f"{stage}: {exc}"
                await self._apps.save(app)
                await self._event(app.id, "install.failed", f"{stage} failed: {exc}", "error")
                raise DeployError(stage, exc) from exc

            app.installed_template_commit = commit
            app.installed_image_digest = digest
            app.container_id = new_id
            app.state = AppState.RUNNING
            app.last_error = None
            app.labels_pending = False
            app.last_change_at = self._clock()
            await self._apply_proxy(app, resolved, None)
            await self._apps.save(app)
            await self._event(app.id, "install.done", f"{app.name} is running")
            return app

    async def _pull(self, app_id: int, op: str, image: str) -> str:
        auth = await self._vault.registry_auth_for(image)
        digest = await self._docker.pull(image, auth)
        await self._event(app_id, f"{op}.pull", f"pulled {image} ({digest})")
        return digest

    async def _wait_healthy(self, app_id: int, op: str, container_id: str) -> None:
        if not await self._docker.wait_healthy(container_id):
            raise RuntimeError("container did not become healthy")
        await self._event(app_id, f"{op}.health", "container is healthy")

    # ------------------------------------------------------------------ NPM

    @staticmethod
    def _domain(subdomain: str, base_domain: str) -> str:
        return f"{subdomain}.{base_domain}"

    async def _apply_proxy(self, app: App, resolved: ResolvedApp, previous: ProxySettings | None) -> None:
        """Create or update the NPM host. Failures become a warning event and `needs_attention`."""
        settings = await self._settings.get()
        old_sub = previous.subdomain if previous else None
        try:
            if self._proxy is None or not settings.npm_url:
                return
            if old_sub and old_sub != app.proxy.subdomain and settings.base_domain:
                old = await self._proxy.find_by_domain(self._domain(old_sub, settings.base_domain))
                if old is not None:
                    await self._proxy.delete_proxy_host(old.id)
            if not app.proxy.subdomain:
                return
            if not settings.base_domain:
                raise ValidationError("base_domain is not configured")
            host: str
            port: int | None
            if app.proxy.forward_mode == "container_network":
                host, port = app.name, resolved.webui_container_port
            else:
                host = settings.host_ip or await self._docker.host_ip()
                port = resolved.webui_host_port
            if port is None:
                raise ValidationError("template defines no WebUI port to forward to")
            domain = self._domain(app.proxy.subdomain, settings.base_domain)
            await self._event(app.id, "npm.sync", f"proxy {domain} -> {host}:{port}")
            await self._proxy.ensure_proxy_host(domain, host, port, app.proxy, None)
        except Exception as exc:
            app.needs_attention = f"NPM registration failed: {exc}"
            await self._event(app.id, "npm.failed", f"NPM step failed: {exc}", "warning")

    async def _delete_proxy(self, app: App) -> None:
        settings = await self._settings.get()
        if self._proxy is None or not settings.npm_url or not app.proxy.subdomain or not settings.base_domain:
            return
        try:
            host = await self._proxy.find_by_domain(self._domain(app.proxy.subdomain, settings.base_domain))
            if host is not None:
                await self._proxy.delete_proxy_host(host.id)
                await self._event(app.id, "uninstall.npm", f"deleted proxy host {host.id}")
        except NotFound:
            pass
        except Exception as exc:
            await self._event(app.id, "uninstall.npm", f"could not delete proxy host: {exc}", "warning")

    # ------------------------------------------------------------------ update / reconfigure

    async def update(self, app_id: int, apply_template: bool, apply_image: bool) -> App:
        """Blue/green update. `apply_image=False` reuses the installed image digest.

        `apply_template=True` deploys the template at the source head; otherwise the installed
        template commit is used, so template changes are never applied without being asked for.
        """
        async with self.locks.hold(app_id):
            app = await self._apps.get(app_id)
            return await self._redeploy("update", app, replace(app), apply_template, apply_image)

    async def reconfigure(
        self,
        app_id: int,
        overrides: dict[str, str] | None,
        proxy: ProxySettings | None,
        image_tag_override: str | None,
    ) -> App:
        """Recreate the container with new settings, reusing the exact running image.

        A changed `image_tag_override` pulls the new tag instead. Also clears `labels_pending`.
        """
        async with self.locks.hold(app_id):
            app = await self._apps.get(app_id)
            candidate = replace(app)
            if overrides is not None:
                candidate.overrides = dict(overrides)
            if proxy is not None:
                candidate.proxy = proxy
            new_tag = image_tag_override is not None and image_tag_override != app.image_tag_override
            if image_tag_override is not None:
                candidate.image_tag_override = image_tag_override
            return await self._redeploy("reconfigure", app, candidate, False, new_tag)

    async def _redeploy(
        self, op: str, app: App, candidate: App, apply_template: bool, pull_new_image: bool
    ) -> App:
        """Blue/green recreate. `candidate` carries the settings to apply; `app` is the stored state."""
        if app.state not in _OPERABLE:
            raise ConflictError(f"cannot {op} an app in state {app.state.value}")
        was_pending = app.labels_pending
        app.state = AppState.UPDATING
        app.last_error = None
        await self._apps.save(app)
        await self._event(app.id, f"{op}.start", f"{op} {app.name}")

        stage = "resolve"
        old: ContainerInfo | None = None
        old_name = f"{app.name}{OLD_SUFFIX}"
        renamed = False
        new_id: str | None = None
        try:
            pinned_commit = None if apply_template else app.installed_template_commit
            template, commit = await self._fetch(app, pinned_commit)
            resolved = await self._resolver.resolve(
                replace(candidate, installed_template_commit=commit), template
            )
            spec: ContainerSpec = await self._with_source_label(app, resolved.spec)
            pin = app.installed_image_digest
            reuse = not pull_new_image and pin is not None
            if reuse and pin is not None:
                spec = replace(spec, image=pinned_ref(spec.image, pin))
                digest = pin
                await self._event(app.id, f"{op}.pull", f"reusing installed image {spec.image}")
            else:
                stage = "pull"
                digest = await self._pull(app.id, op, spec.image)

            stage = "rename_old"
            old = await self._find_container(app)
            if old is None and was_pending:
                raise NotFound(f"container {app.container_id} no longer exists")
            if old is not None:
                stale = await self._docker.inspect(old_name)
                if stale is not None and stale.id != old.id:
                    await self._docker.remove(stale.id, force=True)
                await self._docker.rename(old.id, old_name)
                renamed = True
                await self._docker.stop(old.id)
                await self._event(app.id, f"{op}.rename_old", f"old container parked as {old_name}")

            stage = "create_new"
            new_id = await self._docker.create(spec)
            await self._event(app.id, f"{op}.create_new", f"created {spec.name}")
            stage = "start_new"
            await self._docker.start(new_id)
            stage = "health"
            await self._wait_healthy(app.id, op, new_id)
            stage = "cleanup_old"
            if old is not None:
                await self._docker.remove(old.id, force=True)
                await self._event(app.id, f"{op}.cleanup_old", "removed old container")
        except Exception as exc:
            await self._rollback(app, op, old, renamed, new_id)
            app.state = AppState.FAILED_UPDATE
            app.last_error = f"{stage}: {exc}"
            await self._apps.save(app)
            await self._event(app.id, f"{op}.failed", f"{stage} failed, rolled back: {exc}", "error")
            raise DeployError(stage, exc) from exc

        previous_proxy = app.proxy
        app.overrides = candidate.overrides
        app.proxy = candidate.proxy
        app.image_tag_override = candidate.image_tag_override
        app.installed_template_commit = commit
        app.installed_image_digest = digest
        app.container_id = new_id
        app.state = AppState.RUNNING
        app.labels_pending = False
        if apply_template and app.available_template_commit in (None, commit):
            app.available_template_commit = None
            app.needs_attention = None
        if not reuse and app.available_image_digest in (None, digest):
            app.available_image_digest = None
        if op == "update":
            app.last_change_at = self._clock()
        await self._apply_proxy(app, resolved, previous_proxy)
        await self._apps.save(app)
        await self._event(app.id, f"{op}.done", f"{app.name} is running")
        return app

    async def _rollback(
        self, app: App, op: str, old: ContainerInfo | None, renamed: bool, new_id: str | None
    ) -> None:
        """Best-effort: remove the new container, rename the old one back and start it."""
        steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        if new_id is not None:
            nid = new_id
            steps.append(("remove new", lambda: self._docker.remove(nid, force=True)))
        if old is not None:
            oid = old.id
            if renamed:
                steps.append(("rename old", lambda: self._docker.rename(oid, app.name)))
            if old.state == "running":
                steps.append(("start old", lambda: self._docker.start(oid)))
            app.container_id = oid
        for label, step in steps:
            try:
                await step()
            except Exception as exc:
                app.needs_attention = f"rollback step '{label}' failed: {exc}"
                await self._event(app.id, f"{op}.rollback", f"{label} failed: {exc}", "error")

    # ------------------------------------------------------------------ uninstall

    async def uninstall(self, app_id: int, delete_appdata: bool = False) -> None:
        """Remove the proxy host, container and ports, then the app row."""
        async with self.locks.hold(app_id):
            app = await self._apps.get(app_id)
            previous_state = app.state
            app.state = AppState.REMOVING
            await self._apps.save(app)
            await self._event(app.id, "uninstall.start", f"removing {app.name}")
            await self._delete_proxy(app)
            try:
                container = await self._find_container(app)
                mount_paths = [m.host_path for m in container.mounts] if container else []
                if container is not None:
                    if container.state == "running":
                        await self._docker.stop(container.id)
                    await self._docker.remove(container.id, force=True)
                    await self._event(app.id, "uninstall.container", "container removed")
            except Exception as exc:
                app.state = previous_state
                app.last_error = f"uninstall: {exc}"
                await self._apps.save(app)
                await self._event(app.id, "uninstall.failed", str(exc), "error")
                raise DeployError("remove", exc) from exc
            await self._ports.release(app.id)
            if delete_appdata:
                await self._delete_appdata(app, mount_paths)
            await self._apps.delete(app.id)
            await self._event(app.id, "uninstall.done", f"{app.name} removed")

    async def _delete_appdata(self, app: App, paths: list[str]) -> None:
        root = (await self._settings.get()).appdata_root
        for path in paths:
            deleted = await asyncio.to_thread(self._remove_under_root, root, path)
            if deleted is None:
                await self._event(app.id, "uninstall.appdata", f"refused to delete {path}", "warning")
            elif deleted:
                await self._event(app.id, "uninstall.appdata", f"deleted {path}")

    @staticmethod
    def _remove_under_root(root: str, path: str) -> bool | None:
        """Delete `path` if it resolves strictly inside `root`. None = refused, False = absent."""
        real_root = os.path.realpath(root)
        real = os.path.realpath(path)
        if real in ("/", real_root) or not real.startswith(real_root.rstrip(os.sep) + os.sep):
            return None
        if os.path.isdir(real):
            shutil.rmtree(real)
        elif os.path.lexists(real):
            os.unlink(real)
        else:
            return False
        return True

    # ------------------------------------------------------------------ start / stop / restart

    async def start(self, app_id: int) -> App:
        """Start the app's container."""
        return await self._power(app_id, "start")

    async def stop(self, app_id: int) -> App:
        """Stop the app's container."""
        return await self._power(app_id, "stop")

    async def restart(self, app_id: int) -> App:
        """Stop, then start, the app's container."""
        return await self._power(app_id, "restart")

    async def _power(self, app_id: int, action: str) -> App:
        async with self.locks.hold(app_id):
            app = await self._apps.get(app_id)
            if app.state not in _OPERABLE:
                raise ConflictError(f"cannot {action} an app in state {app.state.value}")
            container = await self._find_container(app)
            if container is None:
                raise NotFound(f"container for {app.name}")
            try:
                if action in ("stop", "restart"):
                    await self._docker.stop(container.id)
                if action in ("start", "restart"):
                    await self._docker.start(container.id)
            except Exception as exc:
                app.last_error = f"{action}: {exc}"
                await self._apps.save(app)
                await self._event(app.id, f"{action}.failed", str(exc), "error")
                raise DeployError(action, exc) from exc
            app.container_id = container.id
            app.state = AppState.STOPPED if action == "stop" else AppState.RUNNING
            await self._apps.save(app)
            await self._event(app.id, action, f"{action} {app.name}")
            return app

    # ------------------------------------------------------------------ sync

    async def sync_state(self) -> None:
        """Reconcile stored app state with Docker (missing, stopped or running containers)."""
        for app in await self._apps.list():
            if app.state not in _OPERABLE or self.locks.locked(app.id):
                continue
            info = await self._find_container(app)
            before = (app.state, app.container_id, app.last_error)
            if info is None:
                app.state = AppState.FAILED
                app.container_id = None
                app.last_error = "container missing"
                await self._event(app.id, "sync.missing", f"container for {app.name} is gone", "warning")
            else:
                app.container_id = info.id
                if info.state == "running" and app.state == AppState.STOPPED:
                    app.state = AppState.RUNNING
                elif info.state != "running" and app.state == AppState.RUNNING:
                    app.state = AppState.STOPPED
            if before != (app.state, app.container_id, app.last_error):
                await self._apps.save(app)
