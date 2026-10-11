# T00 — Scaffold, contracts, domain, DB, fakes

**Model:** orchestrator (recommended), or Sonnet with close review. **Phase:** 0. **Depends on:** nothing.

## Goal
Create the skeleton that every other task builds on. It contains no business logic.

## Owns
`pyproject.toml`, `uv.lock`, `Makefile`, `.gitignore`, `.python-version`, `uam/__init__.py`,
`uam/domain.py`, `uam/contracts.py`, `uam/errors.py`, `uam/settings.py`, `uam/logging.py`,
`uam/db/**`, `tests/conftest.py`, `tests/fakes/**`, `tests/fixtures/templates/*.xml`,
empty packages for every directory listed in the CLAUDE.md layout, `.github/workflows/check.yml`.

## Steps
1. Create `pyproject.toml` with the dependencies listed in CLAUDE.md. Dev dependencies: pytest,
   pytest-asyncio (auto mode), respx, ruff, mypy, time-machine. Configure ruff
   (line length 110, rules `E,F,I,UP,B,ASYNC,S`) and strict mypy for `uam`.
2. `Makefile` with the targets `check`, `fmt`, `test`, `dev`, `openapi` (the last one runs `python -m uam.web.openapi > docs/openapi.json`; T12 provides that module).
3. `domain.py`, `contracts.py`, `errors.py` and `settings.py`, exactly as in
   `docs/contracts.md`.
4. `logging.py`: structlog-style JSON logging to stdout, plus `redact(s)`, which masks
   `ghp_…`, `github_pat_…`, `Bearer …` and `password=…`.
5. `db/`: SQLAlchemy models for `sources`, `apps` (overrides and proxy settings stored as
   JSON), `port_allocations` (unique on `(host_port, protocol)`), `credentials`
   (`secret_ciphertext` BLOB), `settings` (a single JSON row), `events`, and `http_cache`
   (url → etag, body, for T03). Implement `AppRepository`, `SourceRepository`,
   `SettingsStore` and `EventLog`. Add an Alembic initial migration and
   `db.init(url)`, which runs migrations.
6. `tests/fakes/`: in-memory implementations of **every** Protocol in contracts.py:
   `FakeGitHub` (preloaded dict of repo→files), `FakeRegistry` (dict of image→digest),
   `FakeDocker` (container dict, simulated health, can be told to fail
   create/start/health), `FakePortAllocator`, `FakeProxyManager`, `FakeVault`,
   `FakeCodec` (delegates to a minimal parser that is good enough for fixtures), and
   in-memory repositories.
7. `tests/conftest.py`: fixtures `frozen_now`, `tmp_db`, and `fakes` (a bundle of all fakes).
8. `tests/fixtures/templates/`: at least 4 real-world-shaped templates: a simple web app;
   one with several ports including UDP; one with masked variables and `ExtraParams`;
   one in the legacy format with `<Networking><Publish><Port>`.
9. CI workflow that runs `make check`.

## Acceptance
- `make check` passes, with repository tests covering CRUD and `due_for_check`.
- Every fake passes `isinstance(fake, Protocol)` under `@runtime_checkable`.
