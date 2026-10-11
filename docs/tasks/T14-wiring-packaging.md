# T14 — Wiring, packaging, CI, UAM's own Unraid template

**Model:** Sonnet (orchestrator reviews). **Phase:** 4. **Depends on:** T01–T13 merged.

## Owns
`uam/app.py`, `uam/__main__.py`, `Dockerfile`, `.dockerignore`, `.github/workflows/release.yml`,
`unraid/unraid-app-manager.xml`, `README.md`

## Requirements
- `uam/app.py`: `create_app(config) -> FastAPI`. The lifespan does the following: run DB
  migrations, build the real implementations (or the fakes when `UAM_FAKES=1`), wire the
  services, start the scheduler, and shut it down cleanly. The NPM client is built lazily
  from the settings and rebuilt when the settings change.
- `python -m uam` runs uvicorn on `0.0.0.0:${UAM_PORT:-8484}`.
- `Dockerfile`: multi-stage, `python:3.12-slim`, built with uv. Runs as root, which is
  needed for the docker socket and FAT flash writes on Unraid; document this.
  `HEALTHCHECK` on `/api/health`. `VOLUME /data`.
- `release.yml`: on a tag `v*` or a push to main, build multi-arch (amd64, arm64) and push
  to `ghcr.io/felizk/unraid-app-manager` with tags `latest`, `sha-*` and `vX.Y.Z`.
- `unraid/unraid-app-manager.xml`: an Unraid template for UAM itself, with the mounts
  from PLAN.md §3 and WebUI `http://[IP]:[PORT:8484]/`. (UAM can manage its own
  updates later; that is a stretch goal and needs a self-update helper.)
- README: install steps, required mounts, PAT scopes (a classic PAT with `repo` for
  private repos and `read:packages` for GHCR; GHCR does not accept fine-grained PATs for
  pulls at the time of writing), NPM setup, the security notes (docker socket = root on
  the host), and a prominent link to `docs/app-repo-guide.md`.

## Acceptance
- `docker build .` succeeds. The container starts with `UAM_FAKES=1` and `/api/health`
  returns 200.
