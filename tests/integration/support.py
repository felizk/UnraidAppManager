"""Helpers for the integration tests: local registry, test images, NPM API, template builders, UAM driver."""

from __future__ import annotations

import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

TEST_PREFIX = "uamtest-"
NPM_LOGIN = ("admin@example.com", "changeme")
BASE_DOMAIN = "uam.test"
PORT_RANGE = (31000, 31099)
OCI_SOURCE_LABEL = "org.opencontainers.image.source"
SETTLED_EXCLUDED = {"installing", "updating", "removing", "not_installed"}


def free_port() -> int:
    """A currently unused TCP port on the loopback interface."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(
    check: Callable[[], Any], *, timeout: float, interval: float = 0.5, what: str = "condition"
) -> Any:
    """Poll `check` until it returns something truthy; raise `TimeoutError` with `what` otherwise."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while True:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - polling helper
            last_exc = exc
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"timed out after {timeout}s waiting for {what}" + (f" ({last_exc})" if last_exc else "")
            )
        time.sleep(interval)


def local_get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """GET that ignores proxy environment variables."""
    return httpx.get(url, headers=headers, timeout=10.0, trust_env=False)


# --------------------------------------------------------------------------- images / registry
# Alpine's busybox has no httpd, so the test app is a netcat loop. It exits promptly on SIGTERM
# (PID 1 would otherwise ignore it and every `docker stop` would wait for the kill timeout).
SERVE_SCRIPT = r"""#!/bin/sh
body=$(cat /www/index.html)
trap 'kill $pid 2>/dev/null; exit 0' TERM INT
while true; do
  printf 'HTTP/1.0 200 OK\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s\n' \
    "$((${#body} + 1))" "$body" | nc -l -p 3000 >/dev/null &
  pid=$!
  wait $pid
done
"""


class LocalRegistry:
    """A `registry:2` container on `localhost:<port>` plus helpers to build and push tiny test apps."""

    def __init__(self, port: int, base_image: str) -> None:
        self.port = port
        self.host = f"localhost:{port}"
        self._base_image = base_image

    def image(self, repo: str, tag: str = "latest") -> str:
        """Full reference `localhost:<port>/<repo>:<tag>`."""
        return f"{self.host}/{repo}:{tag}"

    def build_and_push(
        self,
        repo: str,
        *,
        version: str = "v1",
        healthy: bool = True,
        source_repo: str | None = None,
        tag: str = "latest",
    ) -> str:
        """Build an image serving `version` on port 3000 (netcat loop), push it, return the manifest digest.

        `healthy=False` gives the image a HEALTHCHECK that always fails.
        """
        check = "wget -q -O /dev/null http://127.0.0.1:3000/ || exit 1" if healthy else "exit 1"
        labels = f'LABEL {OCI_SOURCE_LABEL}="https://github.com/uamtest/{source_repo or repo}"\n'
        dockerfile = (
            f"FROM {self._base_image}\n"
            f"{labels}"
            f"LABEL uamtest.version={version}\n"
            f"RUN mkdir -p /www && echo {version} > /www/index.html\n"
            "EXPOSE 3000\n"
            f"HEALTHCHECK --interval=1s --timeout=2s --retries=2 CMD {check}\n"
            "COPY serve.sh /serve.sh\n"
            'CMD ["/bin/sh", "/serve.sh"]\n'
        )
        ref = self.image(repo, tag)
        with tempfile.TemporaryDirectory(prefix="uamtest-build-") as ctx:
            (Path(ctx) / "Dockerfile").write_text(dockerfile)
            (Path(ctx) / "serve.sh").write_text(SERVE_SCRIPT)
            subprocess.run(  # noqa: S603
                ["docker", "build", "-q", "--provenance=false", "-t", ref, ctx],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(["docker", "push", ref], check=True, capture_output=True, text=True)  # noqa: S603, S607
        return self.digest(repo, tag)

    def digest(self, repo: str, tag: str = "latest") -> str:
        """The manifest digest the registry currently serves for `repo:tag`."""
        accept = ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        )
        resp = httpx.head(
            f"http://{self.host}/v2/{repo}/manifests/{tag}",
            headers={"Accept": accept},
            trust_env=False,
            timeout=10,
        )
        resp.raise_for_status()
        return str(resp.headers["Docker-Content-Digest"])


# --------------------------------------------------------------------------- NPM
class NpmApi:
    """Direct access to the real NPM container (admin API on `admin_port`, proxy on `proxy_port`)."""

    def __init__(self, admin_port: int, proxy_port: int) -> None:
        self.admin_url = f"http://127.0.0.1:{admin_port}"
        self.proxy_port = proxy_port
        self._token: str | None = None

    def login(self) -> bool:
        """Log in with the default credentials; raises if NPM is not ready."""
        resp = httpx.post(
            f"{self.admin_url}/api/tokens",
            json={"identity": NPM_LOGIN[0], "secret": NPM_LOGIN[1]},
            timeout=10.0,
            trust_env=False,
        )
        resp.raise_for_status()
        self._token = str(resp.json()["token"])
        return True

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = httpx.request(
            method,
            f"{self.admin_url}/api{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
            trust_env=False,
            **kw,
        )
        resp.raise_for_status()
        return resp

    def hosts(self) -> list[dict[str, Any]]:
        """All proxy hosts."""
        data: list[dict[str, Any]] = self._call("GET", "/nginx/proxy-hosts").json()
        return data

    def create_host(self, domain: str, forward_host: str, forward_port: int) -> int:
        """Create a plain (non-UAM) proxy host and return its id."""
        body = {
            "domain_names": [domain],
            "forward_scheme": "http",
            "forward_host": forward_host,
            "forward_port": forward_port,
            "certificate_id": 0,
            "ssl_forced": False,
            "allow_websocket_upgrade": False,
            "access_list_id": 0,
            "block_exploits": True,
            "http2_support": False,
            "caching_enabled": False,
            "meta": {},
            "advanced_config": "",
            "locations": [],
        }
        return int(self._call("POST", "/nginx/proxy-hosts", json=body).json()["id"])

    def delete_host(self, host_id: int) -> None:
        """Delete a proxy host."""
        self._call("DELETE", f"/nginx/proxy-hosts/{host_id}")

    def hosts_for(self, domain: str) -> list[dict[str, Any]]:
        """Proxy hosts that serve `domain`."""
        return [h for h in self.hosts() if domain in h.get("domain_names", [])]

    def fetch(self, domain: str) -> httpx.Response:
        """GET `/` through NPM's port 80 with `Host: <domain>`, bypassing any proxy."""
        return local_get(f"http://127.0.0.1:{self.proxy_port}/", headers={"Host": domain})

    def body_via(self, domain: str, expected: str, timeout: float = 45.0) -> str:
        """Poll until the NPM route for `domain` returns a body that equals `expected` (stripped)."""

        def probe() -> str | None:
            resp = self.fetch(domain)
            return resp.text.strip() if resp.status_code == 200 and resp.text.strip() == expected else None

        return str(wait_for(probe, timeout=timeout, what=f"{domain} to return {expected!r} through NPM"))


# --------------------------------------------------------------------------- template builders
def config(
    name: str,
    target: str,
    kind: str,
    *,
    default: str = "",
    value: str | None = None,
    mode: str = "",
    required: bool = False,
    mask: bool = False,
) -> str:
    """One `<Config>` element."""
    v = default if value is None else value
    return (
        f'  <Config Name="{name}" Target="{target}" Default="{default}" Mode="{mode}" Description="" '
        f'Type="{kind}" Display="always" Required="{str(required).lower()}" '
        f'Mask="{str(mask).lower()}">{v}</Config>\n'
    )


def template_xml(
    name: str,
    image: str,
    *,
    repo: str,
    port: str = "3000",
    port_value: str | None = None,
    tz: str = "Europe/Copenhagen",
    tz_value: str | None = None,
    data_path: str = "/tmp/uamtest-appdata",  # noqa: S108
    extra_configs: str = "",
) -> str:
    """An Unraid template for the tiny test app (WebUI on container port 3000)."""
    return (
        '<?xml version="1.0"?>\n<Container version="2">\n'
        f"  <Name>{name}</Name>\n"
        f"  <Repository>{image}</Repository>\n"
        "  <Registry/>\n  <Network>bridge</Network>\n  <Privileged>false</Privileged>\n  <Shell>sh</Shell>\n"
        f"  <Support>https://github.com/uamtest/{repo}/issues</Support>\n"
        f"  <Project>https://github.com/uamtest/{repo}</Project>\n"
        "  <Overview>Integration test app.</Overview>\n  <Category>Tools:</Category>\n"
        "  <WebUI>http://[IP]:[PORT:3000]/</WebUI>\n  <Icon/>\n  <ExtraParams/>\n  <PostArgs/>\n  <CPUset/>\n"
        + config("Web UI", "3000", "Port", default=port, value=port_value, mode="tcp", required=True)
        + config("Data", "/data", "Path", default=f"{data_path}/{name}", mode="rw", required=True)
        + config("TZ", "TZ", "Variable", default=tz, value=tz_value)
        + extra_configs
        + "</Container>\n"
    )


# --------------------------------------------------------------------------- UAM driver
class Uam:
    """Drives an in-process UAM through its REST API."""

    def __init__(self, client: TestClient, tmp: Path) -> None:
        self.client = client
        self.tmp = tmp
        self.appdata = tmp / "appdata"
        self.flash = tmp / "flash"

    def api(
        self, method: str, path: str, *, expect: int | tuple[int, ...] = (200, 201, 202, 204), **kw: Any
    ) -> Any:
        """Call `/api<path>`, assert the status, and return the JSON body (None for 204)."""
        resp = self.client.request(method, f"/api{path}", **kw)
        codes = (expect,) if isinstance(expect, int) else expect
        assert resp.status_code in codes, f"{method} {path} -> {resp.status_code}: {resp.text}"
        return resp.json() if resp.content else None

    def data_dir(self, name: str) -> str:
        """A pre-created host directory for an app's `/data` mount."""
        path = self.appdata / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def add_source(self, repo: str, owner: str = "uamtest") -> int:
        """Register a GitHub source and return its id."""
        return int(self.api("POST", "/sources", json={"owner": owner, "repo": repo})["id"])

    def app(self, app_id: int) -> dict[str, Any]:
        """The app as the API reports it."""
        out: dict[str, Any] = self.api("GET", f"/apps/{app_id}")
        return out

    def settle(self, app_id: int, timeout: float = 120.0) -> dict[str, Any]:
        """Poll until the app leaves its transient states (installing, updating, removing)."""

        def done() -> dict[str, Any] | None:
            app = self.app(app_id)
            return app if app["state"] not in SETTLED_EXCLUDED else None

        out: dict[str, Any] = wait_for(done, timeout=timeout, what=f"app {app_id} to settle")
        return out

    def install(
        self,
        source_id: int,
        template_path: str,
        *,
        data_name: str,
        subdomain: str | None = None,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Add an app and install it; returns the settled app (asserts it ended up running)."""
        body = {
            "source_id": source_id,
            "template_path": template_path,
            "overrides": {"Path:/data": self.data_dir(data_name), **(overrides or {})},
            "proxy": {"subdomain": subdomain, "ssl_forced": False, "websockets": False},
        }
        app_id = int(self.api("POST", "/apps", json=body)["app_id"])
        app = self.settle(app_id)
        if app["state"] != "running":
            events = self.api("GET", f"/apps/{app_id}/events")
            raise AssertionError(f"install ended in {app['state']}: {app['last_error']}\n{events}")
        return app

    def check(self, app_id: int) -> dict[str, Any]:
        """Run an update check now (auto-applies updates the app allows)."""
        out: dict[str, Any] = self.api("POST", f"/apps/{app_id}/check")
        return out

    def host_ports(self, app_id: int) -> dict[int, int]:
        """`container_port -> host_port` allocations of an app."""
        ports = self.api("GET", "/ports")
        return {a["container_port"]: a["host_port"] for a in ports["allocations"] if a["app_id"] == app_id}
