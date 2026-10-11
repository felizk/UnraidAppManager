# UAM rules for this repository

This app is installed and updated by **Unraid App Manager (UAM)**. Full guide:
https://github.com/felizk/unraidappmanager/blob/main/docs/app-repo-guide.md

Reference this file from `CLAUDE.md` / `AGENTS.md` with a line like
"Follow UAM.md for anything touching `unraid/`, the Dockerfile or the publish workflow".

## Never
- Move or rename `unraid/*.xml`. UAM identifies the app by this path.
- Rename a `<Config>` `Target`. Installed users lose their value for it.
- Put secrets in a template `Default`.
- Remove the `HEALTHCHECK` from the Dockerfile. Updates rely on it for automatic rollback.

## Always
- Keep `<Repository>` on a moving tag (`ghcr.io/<owner>/<repo>:latest`).
- Declare every listening port as a `Port` config, and point `<WebUI>` at the HTTP port
  with `[PORT:N]`.
- Store persistent data only under declared `Path` configs
  (`/mnt/user/appdata/<app>/...`).
- Give new configs a `Default`. A new required config without one blocks auto-updates.
- Mark secret Variables `Mask="true"`.
- Keep migrations backward compatible, because a failed update rolls back to the previous image.
- Run `uam-lint . --base origin/main` before committing template changes, and fix every error.
- Mention template changes (new, removed or renamed settings) in the commit message; UAM
  shows the diff to the user.
