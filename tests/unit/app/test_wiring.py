import inspect

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.fakes import FakeVault, InMemorySettingsStore
from uam.app import AppConfig, build_runtime, create_app
from uam.domain import CredentialKind, ProxySettings
from uam.errors import ValidationError
from uam.proxy_switch import SettingsBackedProxyManager
from uam.services.importer import AppReconfigurer
from uam.services.orchestrator import Orchestrator


def test_config_from_env(tmp_path):
    cfg = AppConfig.from_env({"UAM_DATA_DIR": str(tmp_path), "UAM_FAKES": "1", "UAM_SCHEDULER": "0"})
    assert cfg.fakes and not cfg.start_scheduler
    assert cfg.database_url == f"sqlite+aiosqlite:///{tmp_path / 'uam.sqlite3'}"


@pytest.mark.parametrize("fakes", [False, True])
def test_app_starts_and_serves_health(tmp_path, fakes):
    """The real runtime must start without a Docker daemon or network (lazy clients)."""
    cfg = AppConfig(data_dir=tmp_path, fakes=fakes, start_scheduler=False, proc_root=str(tmp_path / "proc"))
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/auth/status").json()["setup_required"] is True
    if not fakes:
        assert (tmp_path / "uam.sqlite3").exists() and (tmp_path / "secret.key").exists()


async def test_real_runtime_scheduler_lifecycle(tmp_path):
    runtime = await build_runtime(AppConfig(data_dir=tmp_path, proc_root=str(tmp_path)))
    assert runtime.scheduler is not None
    runtime.scheduler.start()
    runtime.scheduler.shutdown()
    await runtime.close()


def test_importer_can_call_reconfigure_with_app_id_only():
    # The importer calls reconfigure(app_id); the orchestrator's other parameters must be optional.
    inspect.signature(Orchestrator.reconfigure).bind(object(), 1)
    proto = inspect.signature(AppReconfigurer.reconfigure)
    impl = inspect.signature(Orchestrator.reconfigure)
    assert list(proto.parameters) == list(impl.parameters)


async def test_proxy_switch_unconfigured():
    proxy = SettingsBackedProxyManager(InMemorySettingsStore(), FakeVault(), httpx.AsyncClient())
    assert await proxy.list_proxy_hosts() == []
    assert await proxy.find_by_domain("a.example.com") is None
    assert await proxy.list_certificates() == []
    with pytest.raises(ValidationError):
        await proxy.ensure_proxy_host("a.example.com", "h", 1, ProxySettings(), None)


@respx.mock
async def test_proxy_switch_follows_settings():
    settings, vault = InMemorySettingsStore(), FakeVault()
    cred = await vault.store("npm", CredentialKind.NPM, "admin@example.com", "pw")
    s = await settings.get()
    s.npm_url, s.npm_credential_id = "http://npm:81", cred.id
    await settings.save(s)
    respx.post("http://npm:81/api/tokens").mock(
        return_value=httpx.Response(200, json={"token": "t", "expires": "2099-01-01T00:00:00Z"})
    )
    respx.get("http://npm:81/api/nginx/proxy-hosts").mock(return_value=httpx.Response(200, json=[]))
    respx.get("http://npm2:81/api/nginx/proxy-hosts").mock(return_value=httpx.Response(200, json=[]))
    respx.post("http://npm2:81/api/tokens").mock(
        return_value=httpx.Response(200, json={"token": "t", "expires": "2099-01-01T00:00:00Z"})
    )
    async with httpx.AsyncClient() as http:
        proxy = SettingsBackedProxyManager(settings, vault, http)
        assert await proxy.list_proxy_hosts() == []
        s.npm_url = "http://npm2:81"
        await settings.save(s)
        assert await proxy.list_proxy_hosts() == []
    assert respx.calls.call_count == 4  # token + list on each server
