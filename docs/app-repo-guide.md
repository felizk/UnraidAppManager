# Preparing an app repository for UAM

This guide is for people and coding agents who work on an **app repository**, meaning a
repo that Unraid App Manager (UAM) installs and keeps up to date. Follow it and UAM can
discover, install, auto-update, proxy and import the app without manual fixes.

**Quick start:** copy the files in [`docs/app-repo-kit/`](app-repo-kit/) into the app
repo, rename `myapp`, then work through the [checklist](#9-checklist). For agents, the kit
includes `UAM.md`, a short rules file to reference from the app repo's `CLAUDE.md` or
`AGENTS.md`.

---

## 1. How UAM uses your repo

| UAM does | It reads | So your repo must |
|---|---|---|
| Discovers templates | `*.xml` with a `<Container>` root in `unraid/`, `.unraid/`, `templates/` or the repo root (max depth 2) | Keep the template in `unraid/<app>.xml` |
| Identifies the app | The **template file path** plus the source repo | **Never move or rename the template file** once it's in use |
| Pulls the image | `<Repository>` (for example `ghcr.io/owner/app:latest`) | Publish an image there for every release |
| Detects image updates | The digest of that exact tag, checked every 5 minutes for recently changed apps and daily otherwise | Reference a *moving* tag (`latest`, `stable`, `1`) |
| Detects template updates | The last commit that touched the template file | Commit template changes on the tracked branch (default `main`) |
| Decides whether an update succeeded | The container's Docker `HEALTHCHECK`, or "still running after 15 s" if there is none | Ship a meaningful `HEALTHCHECK` |
| Allocates host ports and proxies the WebUI | `Type="Port"` configs and `<WebUI>` `[PORT:N]` | Declare every port and point WebUI at the HTTP one |
| Remembers user settings | Override key `<Type>:<Target>` (for example `Variable:TZ`) | **Never rename a config's `Target`** casually |
| Matches running containers when importing | The image label `org.opencontainers.image.source` | Build with `docker/metadata-action` (adds the label automatically) |

## 2. Repository layout

```
unraid/
  myapp.xml              # the Unraid template (one per app; several are allowed)
  icon.png               # optional, referenced via raw.githubusercontent.com URL
Dockerfile               # with HEALTHCHECK
.github/workflows/
  publish.yml            # builds and pushes ghcr.io/<owner>/<repo>
```

* **One app per template.** A repo can hold several templates (for example
  `unraid/myapp.xml` and `unraid/myapp-worker.xml`). Each becomes its own UAM app.
* The template's `<Repository>` image should normally be `ghcr.io/<owner>/<repo>`, with
  lowercase owner and repo.

## 3. The template

Start from [`app-repo-kit/unraid/myapp.xml`](app-repo-kit/unraid/myapp.xml). Rules:

### Header elements
| Element | Rule |
|---|---|
| `Name` | Container name. Must match `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. Keep it stable. |
| `Repository` | Full image ref with a **moving tag**. If you pin `:1.4.2`, the app only updates when the template changes. |
| `Network` | `bridge` unless you really need `host`. Ports are **not** mapped with `host`, so there's no auto-port or proxy for them. |
| `WebUI` | `http://[IP]:[PORT:<container port>]/`. `N` must be a declared Port target. This is the port the reverse proxy forwards to. |
| `Icon`, `Overview`, `Project`, `Support` | Fill them in. `Project` should be the GitHub URL, which helps import matching. |
| `ExtraParams` | Avoid. If needed, only these flags are supported: `--restart --memory/-m --cpus --cpuset-cpus --hostname/-h --cap-add --device -e/--env -l/--label --log-opt --network`. Anything else is ignored, with a warning. |
| `PostArgs` | The container command/args, split shell-style. |

### `<Config>` entries
* `Type` is one of `Port`, `Path`, `Variable`, `Label`, `Device`.
* **Port:**
  * `Target` = the container port.
  * `Mode` = `tcp` or `udp`.
  * `Default` = the preferred host port. UAM uses it if it's free and inside the user's
    range; otherwise it picks another port.
  * Declare a port that needs both protocols twice, once with each mode. UAM gives both
    the same host port.
* **Path:**
  * `Target` = the container path.
  * `Default` = the host path, conventionally `/mnt/user/appdata/<app>/...`.
    `[APPDATA]/<app>` is also accepted and expands to the user's appdata root.
  * `Mode` = `rw` or `ro`.
* **Variable:**
  * Mark secrets (password, token, key, secret) with `Mask="true"`.
  * Give a sensible `Default` wherever possible.
* `Required="true"` with an empty `Default` forces the user to fill in a value.
  **Adding such a variable later blocks auto-updates** until the user provides it, so
  add a default where you can.
* `Display="advanced"` hides rarely used settings in the UI.
* Use the modern `<Config>` format. The legacy `<Networking>`/`<Data>`/`<Environment>`
  blocks are still read, but are flagged by the linter.

## 4. Changing a template safely

Users' settings are keyed by `<Type>:<Target>`. When you change the template:

| Change | Effect on installed apps | Do instead |
|---|---|---|
| Add a config **with** a default | Applied on the next template update | — |
| Add a **required** config without a default | Marked *needs attention*; no auto-update | Provide a default if at all possible |
| Rename `Target` (for example `/config` → `/data`) | The user's value is lost; the new default is used | Keep the old target, or tell the user in the commit or release notes |
| Remove a config | The value is dropped; the port is released | Fine when it's intentional |
| Change `Name` | The container is renamed on the next update | Avoid |
| Move or rename the template file | UAM loses track of the app | **Don't.** Add a new file instead and keep the old one until users have migrated |
| Change `Repository` (new image name) | The next template update pulls the new image | Keep publishing the old image for a while |

Template updates are not auto-applied by default. The user reviews a diff first. Image
updates (a new digest for the same tag) are auto-applied by default.

## 5. The image

* **Build it in GitHub Actions and push to GHCR.** Use
  [`app-repo-kit/.github/workflows/publish.yml`](app-repo-kit/.github/workflows/publish.yml).
  It uses `docker/metadata-action`, which adds `org.opencontainers.image.source`.
* **Tags:** push `latest` from the default branch. Add semver tags (`1.4.2`, `1.4`, `1`) on
  release tags. If the template references `:latest`, every merge to `main` that builds an
  image becomes an update for users. Use a `stable` tag that only moves on releases if
  you want slower updates.
* **Platform:** Unraid runs on `linux/amd64`. Build arm64 too only if you need it.
* **HEALTHCHECK:** required in practice. The update is rolled back if the new container
  turns `unhealthy` within 90 s. Keep the check local, cheap and free of external
  network calls, for example `HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1`.
  Make sure the tool you call (`wget`/`curl`) exists in the image.
* **Behind a reverse proxy:** the app is served at the root of `https://<sub>.<domain>/`
  through Nginx Proxy Manager (HTTP upstream, websockets on). Respect `X-Forwarded-Proto`
  and `X-Forwarded-Host`, and don't hardcode absolute `http://` URLs.
* **Persistent data** must live under the declared Path targets. Everything else is lost
  on update, because updates recreate the container.
* **Permissions:** Unraid's convention is UID 99 / GID 100 (`nobody:users`). Support
  `PUID`/`PGID` variables or run as 99:100 so appdata stays accessible from shares.
* **Shutdown:** handle `SIGTERM` and exit within 20 s. Blue/green updates stop the old
  container before starting the new one, so a slow shutdown means downtime.
* **Startup:** be ready (healthy) within 90 s, and run migrations idempotently on start.
  If an update rolls back, the *old* version starts against data the new version may have
  migrated. Prefer backward-compatible migrations.

## 6. Private repositories and packages

* UAM needs a GitHub **classic** PAT with:
  * `repo`, to read a private repo's template;
  * `read:packages`, to pull private GHCR images.

  The user adds it once in UAM under Credentials and selects it on the source. GHCR
  pulls fall back to that PAT automatically.
* At the time of writing, GHCR does not accept fine-grained PATs for pulls. Check
  GitHub's docs if that has changed.
* Link the package to the repo (the metadata-action source label does this when the
  package is first pushed) so it inherits the repo's visibility and access.
* Never commit secrets to the template's `Default`. Declare them as masked Variables
  with an empty default.

## 7. Importing an existing install

If the app already runs from Unraid's Docker tab, UAM can import it (F8). The import
works best when:
* the image carries the `org.opencontainers.image.source` label (from the publish workflow);
* the repo template's `Repository` (without the tag) equals the running image;
* the repo template's `Target`s match what the running container uses. Anything not in
  the template is shown as *dropped* during import.

## 8. Validate before you push

`uam-lint` (task T17) checks a repo against these rules:

```bash
uvx --from git+https://github.com/felizk/unraidappmanager uam-lint .                    # whole repo
uvx --from git+https://github.com/felizk/unraidappmanager uam-lint . --base origin/main  # also check breaking changes
```

`--base` needs the base commit locally; in CI check out with `fetch-depth: 0`.
In CI, use the `lint` job in the kit's `publish.yml`, or the composite action
`felizk/unraidappmanager/action/lint-template`. The UAM repo must be public, or Actions
access to it must be allowed, for other repos to use the action.

| ID | Level | Rule |
|---|---|---|
| UAM001 | error | File is not well-formed XML, or the root isn't `<Container>` |
| UAM002 | error | `Name` is missing or not a valid container name |
| UAM003 | error | `Repository` is missing or not a valid image reference |
| UAM004 | error | Duplicate `(Type, Target)`; for Ports, duplicate `(Target, Mode)` |
| UAM005 | error | Port `Target` isn't 1–65535, or `Mode` isn't tcp/udp |
| UAM006 | error | `WebUI` `[PORT:N]` refers to an undeclared port |
| UAM007 | error | A template is outside the discovery locations |
| UAM101 | error (with `--base`) | A template file was moved or deleted |
| UAM102 | warning (with `--base`) | A config `Target` was removed or renamed; user values will be lost |
| UAM103 | warning (with `--base`) | A required config was added without a default; this blocks auto-update |
| UAM104 | warning (with `--base`) | `Name` or `Repository` changed |
| UAM201 | warning | Legacy template format |
| UAM202 | warning | `ExtraParams` has unsupported flags |
| UAM203 | warning | A secret-looking Variable (`PASS`, `TOKEN`, `SECRET`, `KEY`) isn't `Mask="true"` |
| UAM204 | warning | A required Variable or Path has no default |
| UAM205 | warning | `Network` is `host`, so ports won't be mapped or proxied |
| UAM206 | warning | The `Dockerfile` next to the template has no `HEALTHCHECK` |
| UAM301 | info | Image tag is pinned, so there are no digest-based updates |
| UAM302 | info | Image registry isn't ghcr.io or docker.io |
| UAM303 | info | `Icon`, `WebUI` or `Project` is missing |

## 9. Checklist

- [ ] `unraid/<app>.xml` exists, starts from the kit template, and `uam-lint .` passes with no errors.
- [ ] `Repository` = `ghcr.io/<owner>/<repo>:latest` (or another moving tag).
- [ ] Every listening port is a `Port` config; `WebUI` uses `[PORT:N]` for the HTTP port.
- [ ] All persistent data is under declared `Path` configs with `/mnt/user/appdata/<app>` defaults.
- [ ] Secrets are masked Variables with empty defaults.
- [ ] The Dockerfile has a `HEALTHCHECK`, handles `SIGTERM`, and runs as 99:100 or supports `PUID`/`PGID`.
- [ ] `publish.yml` pushes to GHCR on `main` and on tags, using `docker/metadata-action`.
- [ ] The app works behind a reverse proxy at a subdomain root.
- [ ] Private repo: the user's PAT has `repo` and `read:packages`.
- [ ] Changes to an existing template were checked with `uam-lint . --base origin/main`.
