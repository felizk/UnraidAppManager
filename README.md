# Unraid App Manager (UAM)

UAM installs and updates **your own** apps on Unraid straight from their GitHub repositories,
replacing the Docker tab for those apps. Add a repository, and UAM:

- finds the Unraid template (`unraid/*.xml`) and pulls the image it names, private GHCR images included;
- gives every port a free host port from a range you choose (default `30000-30999`), and keeps it stable;
- registers `<app>.<your-domain>` in Nginx Proxy Manager, and removes it on uninstall;
- checks for updates every 5 minutes for recently changed apps and daily for the rest, then applies
  image updates automatically using blue/green deployment with rollback (template changes wait for your review);
- imports apps you already run from the Docker tab, keeping their ports, settings, image and subdomain;
- writes `my-<app>.xml` templates to the flash drive when you ask it to.

**Preparing an app repository?** Read [docs/app-repo-guide.md](docs/app-repo-guide.md). It covers
the template rules, the publish workflow, `uam-lint`, and a starter kit for people and coding agents.

## Install on Unraid

1. Copy [`unraid/unraid-app-manager.xml`](unraid/unraid-app-manager.xml) to
   `/boot/config/plugins/dockerMan/templates-user/my-unraid-app-manager.xml`.
2. In the Docker tab, choose **Add Container** and select *unraid-app-manager*.
3. Open the WebUI (port 8484) and choose an admin password.

| Host | Container | Why |
|---|---|---|
| `/var/run/docker.sock` | `/var/run/docker.sock` | create and update containers |
| `/mnt/user/appdata/unraid-app-manager` | `/data` | database, encryption key (`secret.key`), template backups |
| `/boot/config/plugins/dockerMan/templates-user` | `/unraid-templates` | template export and import |
| `/proc` (read-only, optional) | `/host/proc` | detect host ports in use and the LAN IP |

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `UAM_PORT` | `8484` | HTTP port |
| `UAM_DATA_DIR` | `/data` | data directory |
| `UAM_SECRET_KEY` | generated into `/data/secret.key` | Fernet key for stored secrets; `UAM_SECRET_KEY_OLD` holds comma-separated previous keys for rotation |
| `UAM_API_TOKEN` | – | enables `Authorization: Bearer <token>` for scripting the REST API |
| `UAM_PROC_ROOT` | `/host/proc` | where the host's `/proc` is mounted |
| `UAM_GITHUB_API_URL` | `https://api.github.com` | GitHub API base URL (GitHub Enterprise, or a stub in tests) |
| `UAM_LOG_LEVEL` | `INFO` | log level (JSON logs on stdout) |
| `UAM_SELF_AUTO_UPDATE` | off | `1` lets UAM install new versions of itself on its own (see below) |
| `UAM_SELF_CONTAINER` | detected | name or id of UAM's own container, if it can't be detected |

## Updating UAM itself

UAM watches its own image the same way it watches apps: every 6 hours (or with **Check now** under
Settings) it compares the registry's digest for its image tag with the image it is running. When a
new version is out, the dashboard says so, a Pushover notification goes out (if set up), and
Settings offers **Update UAM**.

UAM can't replace its own container from inside it, so the update works like this:

1. UAM pulls the new image. If that fails, nothing has changed.
2. It starts a short-lived helper container, `unraid-app-manager-self-update`, from the image it is
   running now, with only the Docker socket and the data folder mounted.
3. The helper stops UAM, renames it to `unraid-app-manager__uam_old`, and creates a new container with
   the same name and settings (ports, paths, variables, labels, networks and static IPs, restart
   policy) on the new image.
4. If the new container becomes healthy, the old one is removed. If it exits, restarts or stays
   unhealthy, the helper removes it and starts the old container again under its own name.
5. The helper removes itself. The UAM that comes up afterwards shows the outcome under Settings,
   records it in the event log and sends a notification.

UAM is unavailable for about a minute while this runs. The container keeps Unraid's
`net.unraid.docker.managed=dockerman` label, so Unraid's Docker tab still lists and edits it as
before, and updating from there keeps working too. With `UAM_SELF_AUTO_UPDATE=1`, UAM installs new
versions after the check finds them; an image that was rolled back once isn't tried again
automatically. Self-update needs the Docker socket at `/var/run/docker.sock` and an image tag (not a
digest); the old image is left on the server, like Unraid's own updates do.

## Credentials

- **GitHub (private repos and GHCR images).** UAM needs a **classic** personal access token with `repo`
  (to read private templates) and `read:packages` (to pull private GHCR images). At the time of
  writing GHCR doesn't accept fine-grained tokens for pulls. The token is also used for ghcr.io
  when no registry credential matches.
- **Other registries.** Add a registry credential for that host.
- **Nginx Proxy Manager.** Add an NPM credential (the e-mail and password of an NPM user), then set
  the NPM URL (for example `http://192.168.1.10:81`), base domain and default certificate in Settings.
  UAM only changes proxy hosts it created, or ones you explicitly take over during an import.
  It marks them with a `# managed-by: unraid-app-manager` line in the advanced config.
- **Pushover notifications.** Add a `pushover` credential with your Pushover user key as the
  username and an application API token (create one at pushover.net) as the secret, then pick it
  under *Notifications* in Settings and use *Send test notification*. UAM then notifies you when an
  app is updated (or an update fails and is rolled back) and when an app crashes: its container
  exits with an error, Docker restarts it on its own, or the container disappears. A clean stop from
  outside UAM (exit code 0) isn't treated as a crash, and crash messages for one app are sent at most
  once an hour. Crashes are spotted by the state check that runs every minute.

Secrets are encrypted at rest with the key in `/data/secret.key`. **Back that file up**: without
it the stored credentials can't be decrypted.

## How UAM shows up in Unraid

Containers UAM manages carry `net.unraid.docker.managed=uam`. In the Docker tab they appear as
"3rd Party": their icon and WebUI link still work, but Unraid's own update check, Edit link and
autostart toggle are off, so Unraid and UAM don't both try to manage the same container. UAM uses
Docker's `unless-stopped` restart policy instead of Unraid's autostart. See
[docs/unraid-labels.md](docs/unraid-labels.md).

## Security

Access to the Docker socket is equivalent to **root on the server**. Keep UAM on your LAN, behind
the admin password, and preferably behind an NPM access list. The container runs as root because
it needs the socket and writes to the FAT-formatted flash drive.

## Development

```bash
uv sync
make check   # ruff format + lint, mypy (strict), pytest
make dev     # run locally with in-memory fakes (UAM_FAKES=1) on http://localhost:8484
```

The design lives in [PLAN.md](PLAN.md), the interfaces in [docs/contracts.md](docs/contracts.md),
and the per-task briefs in `docs/tasks/`. Conventions for coding agents are in [CLAUDE.md](CLAUDE.md).
