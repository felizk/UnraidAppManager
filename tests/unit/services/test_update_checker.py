from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import FrozenClock
from tests.fakes import FakeBundle, FakeCodec, make_app, make_source
from uam.domain import App, AppState, CredentialKind, RegistryAuth
from uam.errors import RateLimited, RemoteError
from uam.services.update_checker import UpdateChecker, diff_templates, resolve_image

IMAGE = "ghcr.io/felizk/notes:latest"
PATH = "unraid/notes.xml"
VAR_A = '<Config Name="A" Target="A" Default="1" Type="Variable"/>'
REQUIRED = '<Config Name="Key" Target="KEY" Default="" Type="Variable" Required="true"></Config>'


def tpl(repo: str = IMAGE, extra: str = "") -> str:
    return (
        f'<Container version="2"><Name>notes</Name><Repository>{repo}</Repository>'
        f'<Config Name="Port" Target="80" Default="8080" Mode="tcp" Type="Port">8080</Config>{extra}'
        "</Container>"
    )


class Updates:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool, bool]] = []
        self.error: Exception | None = None

    async def update(self, app_id: int, apply_template: bool, apply_image: bool) -> App:
        self.calls.append((app_id, apply_template, apply_image))
        if self.error:
            raise self.error
        return make_app(id=app_id)


class Env:
    def __init__(self, fakes: FakeBundle, clock: FrozenClock) -> None:
        self.f = fakes
        self.clock = clock
        self.updater = Updates()
        self.jitter = 1.0
        self.checker = UpdateChecker(
            fakes.apps,
            fakes.sources,
            fakes.github,
            fakes.registry,
            fakes.vault,
            fakes.codec,
            fakes.settings,
            self.updater,
            fakes.events,
            clock,
            jitter=lambda: self.jitter,
        )

    async def add_app(self, **kw: object) -> App:
        src = await self.f.sources.add(make_source())
        defaults: dict[str, object] = {
            "source_id": src.id,
            "template_path": PATH,
            "state": AppState.RUNNING,
            "installed_template_commit": "c1",
            "installed_image_digest": "sha256:old",
            "auto_update_image": False,
        }
        defaults.update(kw)
        self.put(tpl(), "c1", ref="c1")
        self.put(tpl(), "c1")
        self.f.registry.set_digest(IMAGE, "sha256:old")
        return await self.f.apps.add(make_app(**defaults))

    def put(self, xml: str, commit: str, ref: str = "main") -> None:
        self.f.github.put_file("felizk", "notes", PATH, xml, ref=ref, commit_sha=commit)

    async def stored(self, app: App) -> App:
        return await self.f.apps.get(app.id)

    def gets(self) -> int:
        return len([c for c in self.f.github.calls if c[0] == "get"])


@pytest.fixture
def env(fakes: FakeBundle, frozen_now: FrozenClock) -> Env:
    return Env(fakes, frozen_now)


async def test_nothing_changed_cold_schedule(env: Env) -> None:
    app = await env.add_app()
    r = await env.checker.check(app)
    assert not r.template_changed and not r.image_changed
    s = await env.stored(app)
    assert s.last_checked_at == env.clock.now
    assert s.last_change_at is None
    assert s.next_check_at == env.clock.now + timedelta(hours=24)


async def test_304_means_no_template_fetch(env: Env) -> None:
    app = await env.add_app()
    env.put(tpl(extra=VAR_A), "c2")
    await env.checker.check(app)
    gets_after_first = env.gets()
    assert gets_after_first > 1  # the commit lookup plus the template fetches
    await env.checker.check(await env.stored(app))
    # second check: the fake's latest_commit_for_path does one internal get and returns None
    assert env.gets() == gets_after_first + 1


async def test_template_change_detected_and_hot(env: Env) -> None:
    app = await env.add_app()
    env.put(tpl(extra=VAR_A), "c2")
    r = await env.checker.check(app)
    assert r.template_changed and r.new_template_commit == "c2"
    assert r.needs_attention is None
    s = await env.stored(app)
    assert s.available_template_commit == "c2"
    assert s.last_change_at == env.clock.now
    assert s.next_check_at == env.clock.now + timedelta(minutes=5)
    assert "update_available" in env.f.events.kinds(app.id)


async def test_image_change_hot_and_no_auto(env: Env) -> None:
    app = await env.add_app()
    env.f.registry.set_digest(IMAGE, "sha256:new")
    r = await env.checker.check(app)
    assert r.image_changed and r.new_image_digest == "sha256:new"
    assert env.updater.calls == []
    s = await env.stored(app)
    assert s.available_image_digest == "sha256:new"
    assert s.next_check_at == env.clock.now + timedelta(minutes=5)


async def test_hot_window_edges(env: Env) -> None:
    inside = await env.add_app(last_change_at=env.clock.now - timedelta(hours=167))
    await env.checker.check(inside)
    assert (await env.stored(inside)).next_check_at == env.clock.now + timedelta(minutes=5)
    outside = await env.f.apps.add(
        make_app(
            name="b",
            source_id=inside.source_id,
            template_path=PATH,
            state=AppState.RUNNING,
            installed_template_commit="c1",
            installed_image_digest="sha256:old",
            last_change_at=env.clock.now - timedelta(hours=169),
        )
    )
    await env.checker.check(outside)
    assert (await env.stored(outside)).next_check_at == env.clock.now + timedelta(hours=24)


@pytest.mark.parametrize("jitter", [0.9, 1.1])
async def test_jitter_applied(env: Env, jitter: float) -> None:
    app = await env.add_app()
    env.jitter = jitter
    await env.checker.check(app)
    assert (await env.stored(app)).next_check_at == env.clock.now + timedelta(hours=24 * jitter)


async def test_default_jitter_in_range(fakes: FakeBundle, frozen_now: FrozenClock) -> None:
    e = Env(fakes, frozen_now)
    app = await e.add_app()
    checker = UpdateChecker(
        fakes.apps,
        fakes.sources,
        fakes.github,
        fakes.registry,
        fakes.vault,
        fakes.codec,
        fakes.settings,
        e.updater,
        fakes.events,
        frozen_now,
    )
    for _ in range(5):
        await checker.check(await e.stored(app))
        nxt = (await e.stored(app)).next_check_at
        assert nxt is not None
        assert timedelta(hours=24 * 0.9) <= nxt - frozen_now.now <= timedelta(hours=24 * 1.1)


async def test_low_rate_limit_doubles_hot_interval(env: Env) -> None:
    app = await env.add_app()
    env.f.registry.set_digest(IMAGE, "sha256:new")
    env.f.github.rate_fraction = 0.05
    await env.checker.check(app)
    assert (await env.stored(app)).next_check_at == env.clock.now + timedelta(minutes=10)


async def test_rate_limited_backoff(env: Env) -> None:
    app = await env.add_app()
    reset = env.clock.now + timedelta(minutes=30)

    async def boom(*a: object, **k: object) -> None:
        raise RateLimited(reset)

    env.f.github.latest_commit_for_path = boom  # type: ignore[method-assign]
    await env.checker.check(app)
    assert (await env.stored(app)).next_check_at == reset + timedelta(minutes=1)


async def test_image_digest_change_auto_updates(env: Env) -> None:
    app = await env.add_app(auto_update_image=True)
    env.f.registry.set_digest(IMAGE, "sha256:new")
    await env.checker.check(app)
    assert env.updater.calls == [(app.id, False, True)]


async def test_required_var_blocks_template_auto_apply(env: Env) -> None:
    app = await env.add_app(auto_update_template=True)
    env.put(tpl(extra=REQUIRED), "c2")
    r = await env.checker.check(app)
    assert r.template_changed and r.needs_attention and "Variable:KEY" in r.needs_attention
    assert env.updater.calls == []
    assert (await env.stored(app)).needs_attention == r.needs_attention


async def test_required_var_with_override_allows_auto_apply(env: Env) -> None:
    app = await env.add_app(auto_update_template=True, overrides={"Variable:KEY": "x"})
    env.put(tpl(extra=REQUIRED), "c2")
    r = await env.checker.check(app)
    assert r.needs_attention is None
    assert env.updater.calls == [(app.id, True, False)]


async def test_template_auto_apply_off_by_default(env: Env) -> None:
    app = await env.add_app()
    env.put(tpl(extra=VAR_A), "c2")
    await env.checker.check(app)
    assert env.updater.calls == []


async def test_updater_error_recorded_not_raised(env: Env) -> None:
    app = await env.add_app(auto_update_image=True)
    env.f.registry.set_digest(IMAGE, "sha256:new")
    env.updater.error = RuntimeError("kaboom")
    r = await env.checker.check(app)
    assert r.image_changed
    assert "kaboom" in ((await env.stored(app)).last_error or "")
    assert "auto_update_failed" in env.f.events.kinds(app.id)


async def test_registry_failure_recorded(env: Env) -> None:
    app = await env.add_app()
    env.f.registry.digests.clear()
    r = await env.checker.check(app)
    assert not r.image_changed
    s = await env.stored(app)
    assert "image check failed" in (s.last_error or "")
    assert s.next_check_at is not None


async def test_remote_error_in_template_step_does_not_raise(env: Env) -> None:
    app = await env.add_app()

    async def boom(*a: object, **k: object) -> None:
        raise RemoteError(500, "x")

    env.f.github.latest_commit_for_path = boom  # type: ignore[method-assign]
    await env.checker.check(app)
    assert "template check failed" in ((await env.stored(app)).last_error or "")


async def test_tag_override_used_for_registry(env: Env) -> None:
    app = await env.add_app(image_tag_override="beta")
    env.f.registry.set_digest("ghcr.io/felizk/notes:beta", "sha256:beta")
    r = await env.checker.check(app)
    assert r.new_image_digest == "sha256:beta"
    assert env.f.registry.calls == ["ghcr.io/felizk/notes:beta"]


async def test_registry_auth_passed(env: Env) -> None:
    await env.f.vault.store("ghcr", CredentialKind.REGISTRY, "me", "pw", "ghcr.io")
    app = await env.add_app()
    env.f.registry.require_auth[IMAGE] = RegistryAuth("me", "pw", "ghcr.io")
    await env.checker.check(app)
    assert (await env.stored(app)).last_error is None


async def test_pending_update_survives_304(env: Env) -> None:
    app = await env.add_app(auto_update_template=True)
    env.put(tpl(extra=REQUIRED), "c2")
    await env.checker.check(app)
    r = await env.checker.check(await env.stored(app))  # 304 now
    assert r.template_changed and r.needs_attention
    assert env.updater.calls == []


async def test_check_now(env: Env) -> None:
    app = await env.add_app()
    env.f.registry.set_digest(IMAGE, "sha256:new")
    r = await env.checker.check_now(app.id)
    assert r.app_id == app.id and r.image_changed


def test_diff_and_helpers() -> None:
    c = FakeCodec()
    old = c.parse(tpl(extra=VAR_A))
    new = c.parse(tpl("img:2", extra='<Config Name="A" Target="A" Default="2" Type="Variable"/>' + REQUIRED))
    d = diff_templates(old, new)
    assert d.added == ("Variable:KEY",) and d.changed == ("Variable:A",) and d.removed == ()
    assert d.image_changed and d.new_required == ("Variable:KEY",)
    assert diff_templates(new, old).removed == ("Variable:KEY",)
    assert resolve_image("a/b:1", "2") == "a/b:2"
    assert resolve_image("localhost:5000/b", "2") == "localhost:5000/b:2"
    assert resolve_image("a/b@sha256:x", "2") == "a/b:2"
    assert resolve_image("a/b:1", None) == "a/b:1"
