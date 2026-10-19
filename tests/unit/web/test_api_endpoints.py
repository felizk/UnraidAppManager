"""Happy paths, error mapping and secret redaction for every endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import load_template
from tests.fakes import make_app, make_source
from tests.unit.web.test_api_support import ApiEnv, build_env
from uam.domain import (
    AppState,
    ContainerInfo,
    CredentialKind,
    ImportCandidate,
    ImportOptions,
    ImportPlan,
    PortBinding,
    ProxySettings,
)
from uam.errors import (
    AuthError,
    ConflictError,
    DeployError,
    NotFound,
    RateLimited,
    RemoteError,
    ValidationError,
)


@pytest.fixture
async def env() -> AsyncIterator[ApiEnv]:
    e = build_env()
    await e.login()
    yield e
    await e.client.aclose()


# ---------------------------------------------------------------- settings
async def test_settings_roundtrip(env: ApiEnv) -> None:
    body = (await env.client.get("/api/settings")).json()
    assert body["port_range_start"] == 30000
    body.update(port_range_start=31000, port_range_end=31100, base_domain="example.com")
    r = await env.client.put("/api/settings", json=body)
    assert r.status_code == 200
    saved = await env.fakes.settings.get()
    assert (saved.port_range_start, saved.base_domain) == (31000, "example.com")


async def test_settings_validation(env: ApiEnv) -> None:
    body = (await env.client.get("/api/settings")).json()
    body.update(port_range_start=40000, port_range_end=30000)
    assert (await env.client.put("/api/settings", json=body)).status_code == 422


# ---------------------------------------------------------------- credentials
async def test_credentials_never_expose_secret(env: ApiEnv) -> None:
    secret = "ghp_SUPERSECRETTOKEN1234567890"
    r = await env.client.post(
        "/api/credentials", json={"name": "gh", "kind": "github_pat", "username": "me", "secret": secret}
    )
    assert r.status_code == 201
    cred = r.json()
    assert set(cred) == {"id", "name", "kind", "username", "registry_host"}
    listing = await env.client.get("/api/credentials")
    assert listing.json() == [cred]
    assert secret not in r.text and secret not in listing.text


async def test_credential_duplicate_is_409(env: ApiEnv) -> None:
    body = {"name": "gh", "kind": "github_pat", "username": "me", "secret": "x"}
    await env.client.post("/api/credentials", json=body)
    assert (await env.client.post("/api/credentials", json=body)).status_code == 409


async def test_credential_test_github(env: ApiEnv) -> None:
    env.fakes.github.token_scopes["good"] = ["repo", "read:packages"]
    ok = (
        await env.client.post(
            "/api/credentials", json={"name": "ok", "kind": "github_pat", "username": "u", "secret": "good"}
        )
    ).json()
    bad = (
        await env.client.post(
            "/api/credentials",
            json={"name": "bad", "kind": "github_pat", "username": "u", "secret": "ghp_bad123"},
        )
    ).json()
    r = await env.client.post(f"/api/credentials/{ok['id']}/test")
    assert r.json()["ok"] is True and r.json()["scopes"] == ["repo", "read:packages"]
    r = await env.client.post(f"/api/credentials/{bad['id']}/test")
    assert r.status_code == 200 and r.json()["ok"] is False


async def test_credential_test_npm_and_registry(env: ApiEnv) -> None:
    npm = (
        await env.client.post(
            "/api/credentials", json={"name": "npm", "kind": "npm", "username": "a@b.c", "secret": "pw"}
        )
    ).json()
    reg = (
        await env.client.post(
            "/api/credentials",
            json={
                "name": "reg",
                "kind": "registry",
                "username": "u",
                "secret": "pw",
                "registry_host": "ghcr.io",
            },
        )
    ).json()
    assert (await env.client.post(f"/api/credentials/{npm['id']}/test")).json()["ok"] is True
    r = (await env.client.post(f"/api/credentials/{reg['id']}/test")).json()
    assert r["tested"] is False


async def test_credential_delete_and_missing(env: ApiEnv) -> None:
    cred = (
        await env.client.post(
            "/api/credentials", json={"name": "n", "kind": "npm", "username": "u", "secret": "s"}
        )
    ).json()
    assert (await env.client.delete(f"/api/credentials/{cred['id']}")).status_code == 204
    assert (await env.client.delete(f"/api/credentials/{cred['id']}")).status_code == 404
    assert (await env.client.post("/api/credentials/99/test")).status_code == 404


# ---------------------------------------------------------------- sources
async def test_sources_crud(env: ApiEnv) -> None:
    r = await env.client.post("/api/sources", json={"owner": "felizk", "repo": "notes"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["ref"] == "main"
    assert [s["id"] for s in (await env.client.get("/api/sources")).json()] == [sid]
    assert (await env.client.delete(f"/api/sources/{sid}")).status_code == 204
    assert (await env.client.delete(f"/api/sources/{sid}")).status_code == 404


async def test_source_with_unknown_credential_is_404(env: ApiEnv) -> None:
    r = await env.client.post("/api/sources", json={"owner": "a", "repo": "b", "credential_id": 42})
    assert r.status_code == 404


async def test_source_in_use_is_409(env: ApiEnv) -> None:
    src = await env.fakes.sources.add(make_source())
    await env.fakes.apps.add(make_app(source_id=src.id))
    assert (await env.client.delete(f"/api/sources/{src.id}")).status_code == 409


async def test_discover_and_preview_templates(env: ApiEnv) -> None:
    src = await env.fakes.sources.add(make_source(owner="felizk", repo="notes"))
    env.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", load_template("simple-web.xml"))
    env.fakes.github.put_file("felizk", "notes", "unraid/broken.xml", "<Container><oops")
    found = (await env.client.get(f"/api/sources/{src.id}/templates")).json()
    by_path = {t["path"]: t for t in found}
    assert set(by_path) == {"unraid/notes.xml", "unraid/broken.xml"}
    assert by_path["unraid/broken.xml"]["parse_error"]
    found = [by_path["unraid/notes.xml"]]
    assert found[0]["name"] and found[0]["repository"]

    r = await env.client.get(f"/api/sources/{src.id}/templates/preview", params={"path": "unraid/notes.xml"})
    assert r.status_code == 200
    preview = r.json()
    assert preview["repository"] == found[0]["repository"]
    assert any(c["type"] == "Port" for c in preview["configs"])

    missing = await env.client.get(f"/api/sources/{src.id}/templates/preview", params={"path": "nope.xml"})
    assert missing.status_code == 404


async def test_discover_uses_source_token(env: ApiEnv) -> None:
    cred = await env.fakes.vault.store("gh", CredentialKind.GITHUB_PAT, "u", "tok")
    src = await env.fakes.sources.add(make_source(owner="o", repo="priv", credential_id=cred.id))
    env.fakes.github.put_file("o", "priv", "a.xml", load_template("simple-web.xml"))
    env.fakes.github.make_private("o", "priv", "tok")
    assert (await env.client.get(f"/api/sources/{src.id}/templates")).status_code == 200
    plain = await env.fakes.sources.add(make_source(owner="o", repo="priv"))
    assert (await env.client.get(f"/api/sources/{plain.id}/templates")).status_code == 404


# ---------------------------------------------------------------- apps
async def test_create_app_installs_in_background(env: ApiEnv) -> None:
    r = await env.client.post(
        "/api/apps",
        json={
            "source_id": 1,
            "template_path": "unraid/notes.xml",
            "name": "notes",
            "overrides": {"Variable:TZ": "UTC"},
            "proxy": {"subdomain": "notes"},
        },
    )
    assert r.status_code == 202
    app_id = r.json()["app_id"]
    names = [c[0] for c in env.orchestrator.calls]
    assert names == ["add_app", "install"]
    assert env.orchestrator.calls[0][1][4] == ProxySettings(subdomain="notes")
    got = (await env.client.get(f"/api/apps/{app_id}")).json()
    assert got["state"] == "running" and got["proxy"]["subdomain"] == "notes"


async def test_create_app_without_install(env: ApiEnv) -> None:
    r = await env.client.post("/api/apps", json={"source_id": 1, "template_path": "x.xml", "install": False})
    assert r.status_code == 201
    assert [c[0] for c in env.orchestrator.calls] == ["add_app"]


async def test_background_failure_is_recorded_not_raised(env: ApiEnv) -> None:
    env.orchestrator.fail["install"] = DeployError("pull", "boom token=abc123secret")
    r = await env.client.post("/api/apps", json={"source_id": 1, "template_path": "x.xml"})
    assert r.status_code == 202
    events = await env.fakes.events.recent(r.json()["app_id"])
    assert events[0]["level"] == "error" and "pull" in events[0]["message"]
    assert "abc123secret" not in events[0]["message"]


async def test_list_get_and_missing_app(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app(name="a1", state=AppState.RUNNING))
    assert [a["name"] for a in (await env.client.get("/api/apps")).json()] == ["a1"]
    assert (await env.client.get(f"/api/apps/{app.id}")).json()["state"] == "running"
    assert (await env.client.get("/api/apps/999")).status_code == 404


async def test_patch_flags_applies_immediately(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app(auto_update_image=True))
    r = await env.client.patch(
        f"/api/apps/{app.id}", json={"auto_update_image": False, "auto_update_template": True}
    )
    assert r.status_code == 200
    assert r.json()["auto_update_image"] is False
    saved = await env.fakes.apps.get(app.id)
    assert saved.auto_update_template is True
    assert env.orchestrator.calls == []


async def test_patch_overrides_reconfigures_in_background(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    r = await env.client.patch(
        f"/api/apps/{app.id}", json={"overrides": {"Variable:A": "1"}, "proxy": {"subdomain": "x"}}
    )
    assert r.status_code == 202 and r.json() == {"app_id": app.id}
    name, args = env.orchestrator.calls[0]
    assert name == "reconfigure"
    assert args == (app.id, {"Variable:A": "1"}, ProxySettings(subdomain="x"), None)


async def test_delete_app_passes_delete_appdata(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    r = await env.client.delete(f"/api/apps/{app.id}", params={"delete_appdata": "true"})
    assert r.status_code == 204
    assert env.orchestrator.calls == [("uninstall", (app.id, True))]
    assert (await env.client.delete(f"/api/apps/{app.id}")).status_code == 404


async def test_install_and_update_return_202(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    r = await env.client.post(f"/api/apps/{app.id}/install")
    assert r.status_code == 202 and r.json() == {"app_id": app.id}
    r = await env.client.post(
        f"/api/apps/{app.id}/update", json={"apply_template": False, "apply_image": True}
    )
    assert r.status_code == 202
    r = await env.client.post(f"/api/apps/{app.id}/update")
    assert r.status_code == 202
    calls = [c for c in env.orchestrator.calls if c[0] == "update"]
    assert calls == [("update", (app.id, False, True)), ("update", (app.id, True, True))]
    assert (await env.client.post("/api/apps/999/install")).status_code == 404


async def test_start_stop_restart(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app(state=AppState.RUNNING))
    assert (await env.client.post(f"/api/apps/{app.id}/stop")).json()["state"] == "stopped"
    assert (await env.client.post(f"/api/apps/{app.id}/start")).json()["state"] == "running"
    assert (await env.client.post(f"/api/apps/{app.id}/restart")).json()["state"] == "running"
    assert (await env.client.post("/api/apps/999/stop")).status_code == 404


async def test_check_now(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    r = await env.client.post(f"/api/apps/{app.id}/check")
    assert r.status_code == 200
    assert r.json()["template_changed"] is True and r.json()["new_template_commit"] == "abc123"
    assert env.checker.calls == [app.id]


async def test_logs_are_tailed_and_redacted(env: ApiEnv) -> None:
    info = env.fakes.docker.seed_container(ContainerInfo("c1", "notes", "img", None, "running", None, {}, ()))
    env.fakes.docker.log_text[info.id] = "one\ntwo\nlogin with ghp_abcdefghijklmnop123456\nfour"
    app = await env.fakes.apps.add(make_app(container_id="c1"))
    r = await env.client.get(f"/api/apps/{app.id}/logs", params={"tail": 2})
    assert r.status_code == 200
    assert r.json()["logs"] == "login with ***\nfour"
    assert (await env.client.get(f"/api/apps/{app.id}/logs", params={"tail": 0})).status_code == 422


async def test_logs_without_container_is_409(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    assert (await env.client.get(f"/api/apps/{app.id}/logs")).status_code == 409


async def test_events(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    await env.fakes.events.record(app.id, "install", "installed")
    await env.fakes.events.record(None, "scan", "global")
    mine = (await env.client.get(f"/api/apps/{app.id}/events")).json()
    assert [e["kind"] for e in mine] == ["install"]
    everything = (await env.client.get("/api/events", params={"limit": 10})).json()
    assert {e["kind"] for e in everything} == {"install", "scan"}
    assert (await env.client.get("/api/apps/999/events")).status_code == 404


# ---------------------------------------------------------------- ports / npm
async def test_ports_summary(env: ApiEnv) -> None:
    await env.fakes.ports.allocate(1, [(8080, "tcp", None)])
    r = (await env.client.get("/api/ports")).json()
    assert r["range_start"] == 30000
    assert r["allocations"][0]["container_port"] == 8080
    host = r["allocations"][0]["host_port"]
    assert {"host_port": host, "protocol": "tcp"} in r["in_use"]


async def test_npm_lists(env: ApiEnv) -> None:
    assert (await env.client.get("/api/npm/certificates")).json() == [{"id": 1, "name": "*.example.com"}]
    assert (await env.client.get("/api/npm/access-lists")).json() == [{"id": 1, "name": "LAN only"}]


async def test_npm_not_configured_is_422() -> None:
    env = build_env(with_proxy=False)
    try:
        await env.login()
        assert (await env.client.get("/api/npm/certificates")).status_code == 422
    finally:
        await env.client.aclose()


# ---------------------------------------------------------------- export
async def test_export_plan_and_apply(env: ApiEnv) -> None:
    plans = (await env.client.post("/api/templates/export/plan", json={"app_ids": [3, 4]})).json()
    assert [p["app_id"] for p in plans] == [3, 4]
    assert plans[0]["diff"] == "+<new/>"
    assert len((await env.client.post("/api/templates/export/plan", json={})).json()) == 1
    r = await env.client.post("/api/templates/export/apply", json={"plans": plans, "include_secrets": False})
    assert r.status_code == 200
    assert r.json()["written"] == [p["path"] for p in plans]
    applied, include = env.exporter.applied[0]
    assert include is False and applied[0].app_id == 3


async def test_export_apply_stale_plan_is_409(env: ApiEnv) -> None:
    env.exporter.apply_error = ConflictError("template changed since the plan was made")
    plans = (await env.client.post("/api/templates/export/plan", json={"app_ids": [1]})).json()
    r = await env.client.post("/api/templates/export/apply", json={"plans": plans})
    assert r.status_code == 409


# ---------------------------------------------------------------- imports
def _plan(name: str = "notes") -> ImportPlan:
    cand = ImportCandidate(
        container_id="abc",
        container_name=name,
        image="ghcr.io/felizk/notes:1",
        state="running",
        unraid_managed="dockerman",
        flash_template_path=None,
        flash_template=None,
        source_matches=(),
        published_ports=(PortBinding(80, "tcp", 8080),),
        proxy_host=None,
        blockers=(),
        warnings=(),
    )
    return ImportPlan(
        candidate=cand,
        source_id=1,
        new_source=None,
        template_path="unraid/notes.xml",
        template_commit="c0ffee",
        overrides={"Variable:TZ": "UTC"},
        image_tag_override=None,
        installed_image_digest="sha256:aaa",
        pinned_ports=((80, "tcp", 8080),),
        proxy=ProxySettings(subdomain="notes"),
        adopt_proxy_host_id=None,
        options=ImportOptions(),
        template_diff="",
        dropped=("env FOO=*** (not in template; will be removed on recreate)",),
        warnings=(),
    )


async def test_import_candidates(env: ApiEnv) -> None:
    env.fakes.importer.candidates = [_plan().candidate]
    r = (await env.client.get("/api/imports/candidates")).json()
    assert r[0]["container_name"] == "notes"
    assert r[0]["published_ports"] == [
        {"container_port": 80, "protocol": "tcp", "host_port": 8080, "host_ip": ""}
    ]


async def test_import_plan_then_apply_roundtrip(env: ApiEnv) -> None:
    env.fakes.importer.plans["notes"] = _plan()
    r = await env.client.post(
        "/api/imports/plan",
        json={"container_name": "notes", "source_id": 1, "template_path": "unraid/notes.xml", "options": {}},
    )
    assert r.status_code == 200
    plan_json = r.json()
    assert plan_json["pinned_ports"] == [[80, "tcp", 8080]]

    applied = await env.client.post("/api/imports/apply", json=plan_json)
    assert applied.status_code == 202
    assert applied.json()["app_id"] == 1
    got = env.fakes.importer.applied[0]
    assert got == _plan()


async def test_import_plan_needs_exactly_one_source(env: ApiEnv) -> None:
    neither = await env.client.post("/api/imports/plan", json={"container_name": "notes"})
    both = await env.client.post(
        "/api/imports/plan",
        json={"container_name": "notes", "source_id": 1, "new_source": {"owner": "a", "repo": "b"}},
    )
    assert neither.status_code == 422 and both.status_code == 422
    new = await env.client.post(
        "/api/imports/plan", json={"container_name": "ghost", "new_source": {"owner": "a", "repo": "b"}}
    )
    assert new.status_code == 404


async def test_import_apply_conflict(env: ApiEnv) -> None:
    def boom(plan: ImportPlan) -> object:
        raise ConflictError("stale plan")

    env.fakes.importer.on_apply = boom  # type: ignore[assignment]
    env.fakes.importer.plans["notes"] = _plan()
    plan_json = (
        await env.client.post("/api/imports/plan", json={"container_name": "notes", "source_id": 1})
    ).json()
    assert (await env.client.post("/api/imports/apply", json=plan_json)).status_code == 409


# ---------------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (NotFound("x"), 404),
        (ConflictError("x"), 409),
        (ValidationError("x"), 422),
        (AuthError("x"), 401),
        (DeployError("health", "unhealthy"), 500),
    ],
)
async def test_error_mapping(env: ApiEnv, exc: Exception, status: int) -> None:
    app = await env.fakes.apps.add(make_app())
    env.orchestrator.fail["start"] = exc
    r = await env.client.post(f"/api/apps/{app.id}/start")
    assert r.status_code == status
    assert "detail" in r.json()


async def test_deploy_error_includes_stage(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    env.orchestrator.fail["start"] = DeployError("create_new", "no space")
    r = await env.client.post(f"/api/apps/{app.id}/start")
    assert r.status_code == 500 and r.json()["stage"] == "create_new"


async def test_rate_limited_sets_retry_after(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    env.orchestrator.fail["start"] = RateLimited(datetime.now(UTC) + timedelta(seconds=120))
    r = await env.client.post(f"/api/apps/{app.id}/start")
    assert r.status_code == 429
    assert 100 <= int(r.headers["retry-after"]) <= 121


async def test_error_messages_redact_secrets(env: ApiEnv) -> None:
    app = await env.fakes.apps.add(make_app())
    env.orchestrator.fail["start"] = ConflictError("denied for ghp_abcdefghijklmnop123456")
    r = await env.client.post(f"/api/apps/{app.id}/start")
    assert "ghp_abcdefghijklmnop123456" not in r.text


async def test_openapi_lists_all_routes(env: ApiEnv) -> None:
    from uam.web.api import create_api_app

    paths = create_api_app().openapi()["paths"]
    for p in (
        "/api/health",
        "/api/apps/{app_id}/install",
        "/api/imports/apply",
        "/api/templates/export/plan",
    ):
        assert p in paths


async def test_patch_credential_and_source(env: ApiEnv) -> None:
    cred = (
        await env.client.post(
            "/api/credentials", json={"name": "gh", "kind": "github_pat", "username": "me", "secret": "old"}
        )
    ).json()
    r = await env.client.patch(
        f"/api/credentials/{cred['id']}", json={"secret": "rotated", "username": "you"}
    )
    assert r.status_code == 200 and r.json()["username"] == "you" and "rotated" not in r.text
    assert await env.fakes.vault.reveal(cred["id"]) == "rotated"

    src = (await env.client.post("/api/sources", json={"owner": "felizk", "repo": "notes"})).json()
    r = await env.client.patch(f"/api/sources/{src['id']}", json={"credential_id": cred["id"], "ref": "dev"})
    assert r.status_code == 200 and (r.json()["credential_id"], r.json()["ref"]) == (cred["id"], "dev")
    r = await env.client.patch(f"/api/sources/{src['id']}", json={"credential_id": None})
    assert r.json()["credential_id"] is None and r.json()["ref"] == "dev"
    assert (
        await env.client.patch(f"/api/sources/{src['id']}", json={"credential_id": 999})
    ).status_code == 404
    assert (await env.client.patch("/api/credentials/999", json={"secret": "x"})).status_code == 404


async def test_credential_test_pushover(env: ApiEnv) -> None:
    cred = (
        await env.client.post(
            "/api/credentials", json={"name": "push", "kind": "pushover", "username": "u", "secret": "t"}
        )
    ).json()
    r = (await env.client.post(f"/api/credentials/{cred['id']}/test")).json()
    assert r["ok"] is True and env.fakes.notifier.tests == [cred["id"]]
    env.fakes.notifier.fail_test = RemoteError(400, "bad token")
    r = (await env.client.post(f"/api/credentials/{cred['id']}/test")).json()
    assert r["ok"] is False and "bad token" in r["detail"]
