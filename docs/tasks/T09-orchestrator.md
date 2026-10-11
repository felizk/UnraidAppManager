# T09 — Deployment orchestrator

**Model:** Sonnet. **Phase:** 2. **Depends on:** T00 (build and test entirely against the fakes).

## Goal
Implement `uam/services/orchestrator.py`, class `Orchestrator`. It holds the install,
update, uninstall, rollback and start/stop/restart flows. It also provides the
`reconfigure` step that T16 (import) uses.

## Owns
`uam/services/orchestrator.py`, `uam/services/locks.py`, `tests/unit/services/test_orchestrator.py`

## Constructor inputs
`AppRepository`, `SourceRepository`, `GitHubClient`, `TemplateCodec`, `SpecResolver`,
`DockerEngine`, `ProxyManager | None`, `PortAllocator`, `Vault`, `SettingsStore`,
`EventLog`, and `clock: Callable[[], datetime]`.

## Public API
```python
async def add_app(source_id: int, template_path: str, name: str | None, overrides: dict[str,str], proxy: ProxySettings, auto_ports: bool = True) -> App
async def install(app_id: int) -> App
async def update(app_id: int, apply_template: bool, apply_image: bool) -> App
async def reconfigure(app_id: int, overrides: dict[str,str] | None, proxy: ProxySettings | None, image_tag_override: str | None) -> App   # recreate with same image
async def uninstall(app_id: int, delete_appdata: bool = False) -> None
async def start(app_id) / stop(app_id) / restart(app_id)
async def sync_state() -> None   # reconcile App.state with DockerEngine (container missing, stopped, etc.)
```

## Requirements
- **Per-app locking** (`locks.py`): `asyncio.Lock` per app_id. A second operation on the
  same app while one is running raises `ConflictError("operation in progress")`.
- **Install:** follow PLAN.md §4. The state moves `INSTALLING` → `RUNNING`, or to `FAILED`
  with `last_error`. On failure, clean up partial state: remove the container, release
  ports **only if this was a first install**, and do not create an NPM host.
- **Update:** blue/green as in PLAN.md §4. The stages are pull, rename_old, create_new,
  start_new, health, cleanup_old and npm. If any stage fails, roll back and raise
  `DeployError(stage, cause)`. The resulting state is `FAILED_UPDATE` with the old
  container running again. Pull **before** touching the old container.
- **NPM:** only when `proxy.subdomain` and `Settings.npm_url` are set. The domain is
  `f"{subdomain}.{base_domain}"`. The forward target depends on `forward_mode`: host
  mode uses `(host_ip, webui_host_port)`, container_network mode uses
  `(container_name, webui_container_port)`. An NPM failure **does not** fail the deploy:
  record a warning event and set `needs_attention`.
- **Uninstall:** NPM delete (ignore NotFound), stop, remove, release ports, delete the
  app row. `delete_appdata` only removes paths under `Settings.appdata_root`, using
  `os.path.realpath`, never `/`, and never the root itself.
- **Imported apps (`labels_pending=True`, see T16):** find the container by
  `app.container_id`, not by UAM labels. `reconfigure` and `update` recreate it blue/green
  in the same way, then clear `labels_pending`. `reconfigure` must reuse the exact running
  image: create from `installed_image_digest` (`repo@sha256:…`) so nothing new is pulled.
  `sync_state` matches imported apps by container id or name.
- Every stage writes an `EventLog` entry. A successful install or update sets
  `last_change_at = now`, which makes the app hot.

## Acceptance
- Tests that use `FakeDocker` failure injection at every stage, and assert the final
  container set, app state, port allocations and NPM state. Also test lock
  contention, reconfigure of a `labels_pending` app (labels applied, same digest, rollback
  on failure), and sync_state for a container that was deleted outside UAM.
