"""Shared builders for the UI tests: a UI app over the same service stubs as the API tests."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from tests.conftest import load_template
from tests.fakes import FakeBundle
from tests.unit.web.test_api_support import CSRF, PASSWORD, ApiEnv, build_env
from uam.domain import (
    ImportCandidate,
    ImportOptions,
    ImportPlan,
    PortBinding,
    ProxySettings,
    Source,
    SourceMatch,
)
from uam.web.ui import create_ui_app

HX = {"HX-Request": "true"}


@dataclass
class UiEnv:
    api: ApiEnv
    client: httpx.AsyncClient

    @property
    def fakes(self) -> FakeBundle:
        return self.api.fakes

    async def login(self) -> None:
        """First-run setup through the UI (which signs the client in)."""
        r = await self.client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD})
        assert r.status_code == 303, r.text

    async def seed_source(self, template: str = "simple-web.xml", path: str = "unraid/notes.xml") -> Source:
        """A source with one template in the fake GitHub."""
        self.fakes.github.put_file("felizk", "notes", path, load_template(template))
        return await self.fakes.sources.add(Source(0, "felizk", "notes", "main", None, None))


def build_ui_env() -> UiEnv:
    """A UI app plus an httpx client that sends the CSRF header by default."""
    api = build_env()
    app = create_ui_app(api.services)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=dict(CSRF)
    )
    return UiEnv(api, client)


def make_candidate(
    name: str = "notes", *, confidence: str = "high", blockers: tuple[str, ...] = ()
) -> ImportCandidate:
    """A running container with one source match."""
    return ImportCandidate(
        container_id="abc",
        container_name=name,
        image="ghcr.io/felizk/notes:1",
        state="running",
        unraid_managed="dockerman",
        flash_template_path=None,
        flash_template=None,
        source_matches=(
            SourceMatch(1, "felizk", "notes", "unraid/notes.xml", confidence, "image repository"),  # type: ignore[arg-type]
        ),
        published_ports=(PortBinding(80, "tcp", 8080),),
        proxy_host=None,
        blockers=blockers,
        warnings=(),
    )


def make_plan(candidate: ImportCandidate | None = None) -> ImportPlan:
    """A plan with a dropped setting and a secret-looking override."""
    cand = candidate or make_candidate()
    return ImportPlan(
        candidate=cand,
        source_id=1,
        new_source=None,
        template_path="unraid/notes.xml",
        template_commit="c0ffee1234567890",
        overrides={"Variable:TZ": "UTC", "Variable:API_TOKEN": "supersecretvalue"},
        image_tag_override=None,
        installed_image_digest="sha256:aaa",
        pinned_ports=((80, "tcp", 8080),),
        proxy=ProxySettings(subdomain="notes"),
        adopt_proxy_host_id=None,
        options=ImportOptions(),
        template_diff="--- flash\n+++ source\n@@ -1 +1 @@\n-old line\n+new line\n",
        dropped=("env FOO=*** (not in template; will be removed on recreate)",),
        warnings=("port 80 was published on all interfaces",),
    )
