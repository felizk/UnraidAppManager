# CLAUDE.md — conventions for every agent working on UAM

Read `PLAN.md` for the big picture and `docs/contracts.md` for the interfaces. If you were
given a task brief (`docs/tasks/Txx-*.md`), it overrides anything general here.

## Stack
Python 3.12, `uv` for dependencies, FastAPI, SQLAlchemy 2.0 async + aiosqlite, Alembic,
httpx (async), docker SDK, APScheduler 3.x, Jinja2 + HTMX + Pico.css, cryptography (Fernet),
argon2-cffi, defusedxml.

## Commands
- `make check`: ruff format --check, ruff lint, mypy (strict for `uam/`), pytest. **Must pass before you commit.**
- `make fmt`: auto-format.
- `make test`: pytest only. `make dev`: run locally with fakes (`UAM_FAKES=1`).

## Rules
1. **Stay in your lane.** Only create or modify the paths your brief lists under *Owns*,
   plus new test files under `tests/unit/<your module>/`.
2. **Never edit** `uam/contracts.py`, `uam/domain.py`, `uam/errors.py`, `uam/settings.py`,
   or `tests/fakes/`. If they don't fit your needs, stop and report the exact change you need.
3. Depend on other modules only through `uam.contracts` Protocols, which are injected
   through the constructor. Never import another leaf module's implementation.
4. All I/O is async. Wrap blocking libraries (docker SDK, file I/O over 1 MB) in `asyncio.to_thread`.
5. Never log secrets. Use `uam.logging.redact()` for anything that may hold a token.
6. HTTP is mocked with `respx`, Docker with `tests/fakes/fake_docker.py`, and time with the
   `frozen_now` fixture. **No real network in unit tests.**
7. Use type hints everywhere. Add a docstring on every public function. Don't add comments
   that only repeat what the code says.
8. Commit message: `<TaskID>: <short title>`. One logical commit per task is fine.
9. Finish with a report: what you built, deviations, open questions, and the test count.

## App-repo guide
`docs/app-repo-guide.md` and `docs/app-repo-kit/` describe UAM's behaviour to the people
who maintain app repos: discovery paths, override keys, update and health rules,
supported ExtraParams, and lint rule IDs. **If your task changes any of that behaviour,
update the guide in the same commit and tell the orchestrator.** The lint rule table in
§8 is the spec for T17.

## Layout
```
uam/
  domain.py contracts.py errors.py settings.py logging.py
  db/                 # SQLAlchemy models, repositories, alembic
  unraid_template/    # T01
  vault/              # T02
  clients/            # github.py T03, registry.py T04, docker_engine.py T05, npm.py T07
  ports/              # T06
  services/           # spec_resolver T08, orchestrator T09, update_checker+scheduler T10, template_export T11
  web/                # api/ T12, auth.py T12, ui/ templates/ static/ T13
  app.py              # wiring (T14)
tests/
  fakes/ unit/ integration/ fixtures/templates/*.xml
```
