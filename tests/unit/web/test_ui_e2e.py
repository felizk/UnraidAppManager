"""Playwright smoke test (deselected by default; run with `pytest -m e2e`)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.e2e

playwright_sync = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

CHROMIUM_DIR = "/opt/pw-browsers"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server() -> Iterator[str]:
    """The UI over fakes on a real socket; the install stub flips the app to running."""
    from tests.conftest import load_template
    from tests.unit.web.test_api_support import build_env
    from uam.web.ui import create_ui_app

    env = build_env()
    env.fakes.github.put_file("felizk", "notes", "unraid/notes.xml", load_template("simple-web.xml"))
    app = create_ui_app(env.services)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_login_add_source_install_and_see_running(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", CHROMIUM_DIR)
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(server + "/")
        page.wait_for_url("**/setup")
        page.fill("input[name=password]", "correct horse battery")
        page.fill("input[name=confirm]", "correct horse battery")
        page.click("button[type=submit]")
        page.wait_for_url(server + "/")

        page.goto(server + "/sources")
        page.fill("input[name=repo]", "felizk/notes")
        page.click("text=Add source")
        page.wait_for_selector("text=felizk/notes")
        page.click("text=Discover templates")
        page.click("text=Add app")
        page.wait_for_selector("text=Container name")
        page.click("button:has-text('Add app')")

        page.wait_for_url("**/apps/1")
        page.goto(server + "/")
        page.wait_for_selector(".badge.state-running", timeout=20000)
        browser.close()
