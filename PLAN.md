# Unraid App Manager (UAM) — Plan

UAM is a self-hosted web app that runs as a container on an Unraid server and replaces the
stock Docker tab for installing and updating *your own* apps. You register GitHub
repositories. UAM finds the Unraid template (XML) in each repo, pulls the image it points
to (usually `ghcr.io/...`, private ones too), allocates host ports, creates the
container, registers a subdomain in Nginx Proxy Manager (NPM), and keeps it all up to date.

## 1. Feature scope

| # | Feature | Summary |
|---|---------|---------|
| F1 | GitHub sources | Add a repo (`owner/repo`, branch, optional template path). UAM finds `*.xml` files with a `<Container>` root (default search: `unraid/`, `.unraid/`, `templates/`, then the repo root). A repo can contain several templates, which become several apps. |
| F2 | Install / update / uninstall | Build the container from the template plus per-app overrides, using the Docker Engine API through the mounted socket. Updates are blue/green with automatic rollback. |
| F3 | Auto port mapping | Every `Type="Port"` config gets a free host port from a configured range (default `30000-30999`). Allocations are stable across updates and are released on uninstall. |
| F4 | NPM subdomains | On install, create or update a proxy host `<subdomain>.<base_domain>` that points at the app's WebUI port. On uninstall, delete it. Options: SSL cert id, force SSL, websockets, access list. |
| F5 | Private repos and packages | Credentials (GitHub PATs, registry logins) are stored encrypted. Each source and each image registry can use a credential. Docker pulls pass `auth_config` per pull and never write `~/.docker/config.json`. |
| F6 | Tiered update checks | "Hot" apps (template or image changed or installed within `hot_window`, default 7 days) are checked every `hot_interval` (default 5 min). Every other app is checked once per day. Checks are cheap: conditional GitHub requests (ETag, so a 304 costs no rate limit) and registry `HEAD` requests for the manifest digest. A per-app `auto_update` setting chooses between applying an update and only flagging it. |
| F8 | Import existing apps | Scan the containers already on the server (typically ones created by Unraid's Docker tab, with a `my-<name>.xml` on flash). Match each one to a GitHub source and template, keep its current ports, settings, running image and NPM subdomain, then recreate it under UAM management. You review a plan first; settings the repo template can't express are listed before anything changes. |
| F7 | Template export to flash | Runs only when you trigger it, per app or for all apps. Writes `my-<Name>.xml` with the resolved values into `/boot/config/plugins/dockerMan/templates-user`. You get a diff preview first, and the existing file is backed up before it is overwritten. |

Out of scope for v1 (stretch goals): GitHub webhooks for instant updates, Docker Compose
apps, notifications (ntfy/Unraid notify), multi-user accounts, and registries other than
GHCR and Docker Hub (the design keeps this open).

## 2. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.12, fully async | Strong libraries (docker SDK, httpx, FastAPI) and easy for Sonnet subagents to work in. |
| Web | FastAPI + Jinja2 + HTMX + Pico.css | No JS build step, so a single container stays small. |
| Storage | SQLite (SQLAlchemy 2.0 async + aiosqlite), Alembic migrations | Single-file database in `/data`. |
| Scheduling | One APScheduler "tick" job every 60 s that picks apps where `next_check_at <= now` | Simpler and more robust than one scheduler job per app. |
| Docker access | `docker` SDK wrapped in `asyncio.to_thread`, socket mounted at `/var/run/docker.sock` | Standard approach. |
| Secrets | Fernet; key from `UAM_SECRET_KEY` env, or generated once into `/data/secret.key` | Encrypted at rest without extra infrastructure. |
| Containers UAM owns | Label `uam.managed=true`, `uam.app_id=<id>`, plus Unraid UI labels `net.unraid.docker.managed=uam`, `net.unraid.docker.icon`, `net.unraid.docker.webui` | The app's icon and WebUI link show in Unraid, and stock dockerMan does not try to update the container. **Verify on real Unraid** (T05). |
| UI auth | One admin password (argon2 hash in DB), session cookie | Basic protection on the LAN. NPM access lists can be layered on top. |

## 3. Architecture

```
            ┌──────────────── web (FastAPI routes + HTMX views) ────────────────┐
            │                         api/   ui/                                │
            └───────────────────────────────┬───────────────────────────────────┘
                                            │ calls
            ┌───────────────────────────────▼───────────────────────────────────┐
            │ services/  orchestrator (install/update/uninstall/rollback)       │
            │            update_checker (tiered polling)  scheduler (tick)      │
            │            spec_resolver (template+overrides+ports → ContainerSpec)│
            │            template_export (flash writer)                         │
            └──┬─────────┬──────────┬──────────┬──────────┬──────────┬──────────┘
               │         │          │          │          │          │
         unraid_template github  registry   docker_engine ports    npm      vault
          (parse/render) client   client    adapter      allocator client  (creds)
               └─────────┴──────────┴──────────┴──────────┴──────────┴── db/ (models, repos)
```

* Leaf modules (bottom row) depend only on `uam/contracts.py`, `uam/domain.py` and
  third-party libraries. They do not depend on each other, so they can be built in parallel.
* Services depend on leaf modules **through the Protocols in `contracts.py` only**.
  Wiring happens in `uam/app.py`, which builds a container/registry of implementations.
* Every external system has a Protocol and an in-memory fake in `tests/fakes/`. Services
  and the web layer are tested against the fakes, so they can be built before the real
  clients exist.

The interfaces are pinned in **[docs/contracts.md](docs/contracts.md)**. That document is
the source of truth that every task builds against.

### Deployment on Unraid (UAM's own container)

| Host | Container | Mode |
|---|---|---|
| `/var/run/docker.sock` | `/var/run/docker.sock` | rw |
| `/mnt/user/appdata/unraid-app-manager` | `/data` | rw |
| `/boot/config/plugins/dockerMan/templates-user` | `/unraid-templates` | rw |
| `/proc` (optional, used for host-port detection) | `/host/proc` | ro |

## 4. Core flows

**Install**
1. Fetch the template at the source's `ref` and parse it. Record `template_sha`.
2. Resolve the spec: template defaults, then user overrides, then port allocations (F3),
   then UAM labels. Parse `ExtraParams` into the supported subset and warn about the rest.
3. Resolve the registry credential for the image host and pull. Record the image digest.
4. Create and start the container, then wait until it is healthy (Docker healthcheck if
   defined, otherwise "running for 15 s").
5. If a subdomain is set, call `ensure_proxy_host` in NPM and store `npm_proxy_host_id`.
6. Write an audit event. Mark the app hot (`last_change_at = now`).

**Update (blue/green):** pull the new image, rename the old container to `<name>__uam_old`
and stop it, create and start the new one, then health-check. If it passes, remove the old
container. If it fails, remove the new one, rename the old one back and start it, and set
the app to `failed_update` with the error. If the WebUI port changed, the NPM entry is
updated.

**Uninstall:** stop and remove the container, delete the NPM host, release the ports.
Appdata is kept unless you ask for it to be deleted (a confirmation checkbox, and only
under the configured appdata root).

**Update check (per app):**
* Template: `GET /repos/{o}/{r}/commits?path=<tpl>&sha=<ref>&per_page=1` with
  `If-None-Match`. If the SHA is new, fetch and parse the template, then diff it against
  the installed one. If a new required variable has no default, set
  `needs_attention`; never auto-apply that.
* Image: registry `HEAD /v2/<name>/manifests/<tag>` → `Docker-Content-Digest`, compared
  with the installed digest. Multi-arch images are compared at the manifest-list digest.
* Scheduling: `next_check_at = now + (hot_interval if hot else daily)`, with ±10% jitter.
  If the GitHub rate-limit remaining drops below 10%, the hot interval is doubled until
  the limit resets.

**Import (F8, T16):**
1. **Scan:** list every container without `uam.managed`, and read its flash template if
   there is one.
2. **Match a source:** use the image's `org.opencontainers.image.source` label, the
   `ghcr.io/<owner>/<repo>` image name, or GitHub URLs in the template. Pick the repo
   template whose image matches.
3. **Plan:** derive overrides from the live container (`inspect`), with the flash template
   as a fallback. Pin the current host ports (even outside the range). Pin the running tag
   and digest, so the import never upgrades the app. Match the NPM host by domain or port.
   List the *dropped* settings, which the repo template has no field for.
4. **Apply:** save the app with `labels_pending=True`, pin the ports and claim the NPM
   host, which adds the UAM marker. Then recreate the container blue/green from the same
   digest so it gets UAM labels. If the recreate fails, the old container is restored and
   the app stays imported but flagged.

**Template export:** render `UnraidTemplate` with resolved values (actual host ports and
override values). The user chooses whether to include masked secrets (default: include,
which matches what Unraid itself stores). Diff the result against the existing
`my-<Name>.xml`, back up the old file to `/data/template-backups/<ts>/`, then write it
atomically (temporary file, then rename).

## 5. Task breakdown and dependency graph

Each task has a self-contained brief in `docs/tasks/`. A Sonnet subagent should only
need `CLAUDE.md`, `docs/contracts.md` and its own brief.

| ID | Task | Phase | Depends on | Owns (only touches) |
|----|------|-------|-----------|---------------------|
| T00 | Scaffold, contracts, domain, DB, fakes | 0 | – | everything listed in its brief |
| T01 | Unraid template parse/render | 1 | T00 | `uam/unraid_template/` |
| T02 | Credential vault | 1 | T00 | `uam/vault/` |
| T03 | GitHub client | 1 | T00 | `uam/clients/github.py` |
| T04 | Registry client (digests, tokens) | 1 | T00 | `uam/clients/registry.py` |
| T05 | Docker engine adapter | 1 | T00 | `uam/clients/docker_engine.py` |
| T06 | Port allocator | 1 | T00 | `uam/ports/` |
| T07 | NPM client | 1 | T00 | `uam/clients/npm.py` |
| T08 | Spec resolver | 2 | T01, T06 | `uam/services/spec_resolver.py` |
| T09 | Deployment orchestrator | 2 | T00 (fakes); real wiring after T02–T08 | `uam/services/orchestrator.py` |
| T10 | Update checker and scheduler | 2 | T00 (fakes) | `uam/services/update_checker.py`, `uam/services/scheduler.py` |
| T11 | Template export to flash | 2 | T01 | `uam/services/template_export.py` |
| T12 | REST API and auth | 3 | T00 (fakes) | `uam/web/api/`, `uam/web/auth.py` |
| T13 | Web UI (HTMX) | 3 | T12 | `uam/web/ui/`, `uam/web/templates/`, `uam/web/static/` |
| T14 | Wiring, packaging, CI, UAM's own template | 4 | all | `uam/app.py`, `Dockerfile`, `.github/`, `unraid/` |
| T16 | Import existing Unraid apps | 2 | T00 (fakes) | `uam/services/importer.py`, `uam/services/import_matching.py` |
| T15 | Integration/E2E tests (real Docker, NPM in compose) | 4 | T14 | `tests/integration/` |

```
T00 ──┬─ T01 ─┬─ T08 ─┐
      ├─ T02  │       │
      ├─ T03  ├─ T11  │
      ├─ T04  │       ├─ T14 ─ T15
      ├─ T05  │       │
      ├─ T06 ─┘       │
      ├─ T07          │
      ├─ T09 (fakes) ─┤
      ├─ T10 (fakes) ─┤
      ├─ T16 (fakes) ─┤
      └─ T12 ── T13 ──┘
```

Because services and the web layer build against fakes, **up to 13 tasks can run in
parallel right after T00**. In practice, batches of 4–6 are easier to review:

* Batch A: T01, T02, T03, T04, T05, T06, T07 (pure leaf modules)
* Batch B: T08, T09, T10, T11, T12, T16
* Batch C: T13, then T14, then T15

## 6. How to run this plan with subagents

1. **The orchestrator (main session, stronger model) does T00 itself.** The contracts are
   the one artifact every other task depends on, and a mistake there spreads everywhere.
   Commit T00 before starting any other task.
2. For each task in a batch, launch a subagent:
   * `Agent(subagent_type="general-purpose", model="sonnet", isolation="worktree", run_in_background=true)`
   * Prompt template:
     > Read `CLAUDE.md`, `docs/contracts.md` and `docs/tasks/<ID>-*.md`. Implement the task
     > exactly as specified. Touch only the paths the brief lists under *Owns*. Do not modify
     > `uam/contracts.py`, `uam/domain.py` or `tests/fakes/`. If the contract is insufficient,
     > stop and report the exact change you need. Run `make check` until it passes. Commit
     > on your worktree branch with message `<ID>: <title>`. Reply with: a summary of what you
     > built, any deviations from the brief, any open questions, and the test count.
3. **Review:** for each finished worktree, read the diff, run `make check`, and check the
   brief's acceptance criteria. Then merge it into the development branch. Because tasks
   own disjoint paths, merges should not conflict.
4. **Contract changes:** only the orchestrator edits `contracts.py`, `domain.py` or the
   fakes. Then it rebases any in-flight tasks that are affected, or tells them about the
   change.
5. After each batch, run the full suite before starting the next batch.

## 7. Open questions (defaults chosen; override if you prefer)

1. **Unraid managed label:** `net.unraid.docker.managed=uam` (default) or `dockerman`?
   With `dockerman`, Unraid's own Edit/Update buttons work on exported templates, but
   Unraid may also try to update the containers itself.
2. **Auto-update default for new apps:** default is *on* for image-digest changes and
   *off* for template changes, which need review.
3. **NPM forward target:** default is `<unraid_host_ip>:<allocated_port>`. Optionally,
   containers can be attached to NPM's Docker network and the proxy can forward to
   `<container_name>:<container_port>` (per-app toggle).
4. **Port range:** a single global range `30000-30999` by default. Per-protocol or
   per-app ranges could be added later.
5. **Secrets in overrides:** masked template values (from installs and imports) currently
   sit in the `apps.overrides` JSON in plain text. Proposal: T00 stores the overrides of
   masked configs encrypted with the vault key. Decide before T00 starts.
6. **Python vs Go:** Python is chosen for subagent productivity. Go would give a smaller
   single binary. The contracts translate one-to-one if this changes.
