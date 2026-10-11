# T16 — Import existing Unraid apps

**Model:** Sonnet. **Phase:** 2. **Depends on:** T00 (build and test against the fakes,
including `FakeOrchestrator`-style stubs for `reconfigure`).

## Goal
Bring a container that is already running on the server (usually one created by Unraid's
Docker tab, `net.unraid.docker.managed=dockerman`) under UAM management. Its data, ports,
subdomain and running image must not change in the process. Implement the `Importer`
Protocol from `docs/contracts.md`.

## Owns
`uam/services/importer.py`, `uam/services/import_matching.py`, `tests/unit/services/test_importer.py`,
`tests/unit/services/test_import_matching.py`, `tests/fixtures/import/**`

## Constructor inputs
`DockerEngine`, `TemplateCodec`, `GitHubClient`, `SourceRepository`, `AppRepository`,
`PortAllocator`, `ProxyManager | None`, `Vault`, `SettingsStore`, `EventLog`, and an
`AppReconfigurer` Protocol defined in this module with one method,
`reconfigure(app_id, overrides=None, proxy=None, image_tag_override=None) -> App`. In production, T09's orchestrator provides it.

## scan() → list[ImportCandidate]
1. `DockerEngine.list_containers(all=True)`. Skip containers labelled `uam.managed=true`
   and UAM's own container (detect it with `HOSTNAME`, which equals the container id
   prefix).
2. **Flash template.** Look in `Settings.template_export_dir` for
   `my-<container name>.xml`. If it isn't there, parse every `my-*.xml` (cache them by
   mtime) and match on `<Name>` == container name. If a template fails to parse, the
   candidate gets a warning but is not dropped.
3. **Source matching** (`import_matching.py`, pure functions) produces `SourceMatch`es,
   best first:
   - **high:** the image label `org.opencontainers.image.source` = `https://github.com/<o>/<r>`
     (GHCR images built by GitHub Actions carry it). Read it from
     `ContainerInfo.image_labels`.
   - **high:** the image is `ghcr.io/<o>/<r>[:tag]` and `<o>/<r>` is an existing source,
     compared case-insensitively.
   - **medium:** the image is `ghcr.io/<o>/<r>` but there is no matching source yet. This
     is suggested as a *new* source.
   - **medium:** the flash template's `<Project>`, `<Support>` or `<TemplateURL>`
     (in `unknown_elements`) holds a github.com URL.
   - **low:** an existing source has a discovered template whose `Repository` image
     (without its tag) equals the container's image.
   If a match points at an existing source, fill in `template_path` by running
   discovery and choosing the template whose `repository` (ignoring the tag) matches
   the image. If that fails, match on `name`.
4. **Proxy host.** If the proxy manager is configured, choose the first match:
   - `domain == <container name>.<base_domain>`;
   - `forward_port` equals one of the container's published host ports, and
     `forward_host` is the host IP;
   - `forward_host == container name`.
5. **Blockers:** a UAM app with the same name already exists; the container is UAM's own.

## plan(container_name, source_id | new_source, template_path, options) → ImportPlan
1. Fetch the repo template at the latest commit for `template_path`. Use a
   `Source(id=0, …)` for a new source; it isn't saved until `apply`.
2. **Overrides.** Use the actual container state (`inspect`) as the truth and the flash
   template only as a fallback. For each repo-template config:
   - Variable: env value. Path: the host path of the mount whose container path ==
     target. Device: the matching device. Label: the label value.
   - Write an override only when the value differs from the repo template's
     value/default. Masked values are included.
3. **Dropped settings.** Container settings the repo template has no config for (extra
   env vars, mounts, devices and ports, excluding image-provided env and `net.unraid.*`
   labels) go into `dropped`. Each one is a human-readable line such as
   `env FOO=… (not in template; will be removed on recreate)`. Masked values print as `***`.
4. **Ports.** If `options.keep_host_ports` (the default), pin every published binding
   whose container port is a template Port config, even when the port is outside the
   range. Otherwise leave the ports unpinned so the allocator remaps them, and add a
   warning that the WebUI and proxy port will change.
5. **Proxy.** `ProxySettings(subdomain=options.subdomain or a subdomain derived from the
   matched host's domain)`. `adopt_proxy_host_id` is set when `claim_proxy_host` is on
   and a host matched. Matching a proxy host whose `forward_port` is not the WebUI port
   is a warning.
6. **template_diff:** a unified diff between the flash template, rendered by the codec
   with its values blanked, and the repo template rendered the same way. It shows the
   user what structure will change when the container is recreated. It is empty when
   there is no flash template.
7. **Image.** `installed_image_digest` is the running container's RepoDigest. If the
   template's tag differs from the running tag, set `image_tag_override` to the running
   tag so that recreating does not upgrade the app. If the image has no RepoDigest
   (built locally), add a blocker.

## apply(plan) → App
Run the steps in this order. If any step up to and including the NPM claim fails, undo
the earlier steps.
1. Re-run `scan()` for this container. If the container id changed or a blocker
   appeared, raise `ConflictError` (stale plan).
2. If `new_source` is set, save it to `SourceRepository`.
3. Create the `App` row: state from the container (RUNNING or STOPPED),
   `container_id`, `installed_image_digest`, `installed_template_commit` = the commit
   from step 1 of `plan`, `labels_pending=True`, `last_change_at=now` (which makes the
   app hot), and `next_check_at=now`.
4. `PortAllocator.pin(app_id, …, ignore_container_id=container_id)`.
5. `ProxyManager.claim_proxy_host(adopt_proxy_host_id, app.name)` if it is set.
6. Log an `EventLog` entry `import` that includes the dropped items.
7. If `options.recreate_now`, call `reconfigure(app_id)`. That is T09's blue/green
   recreate with rollback, so the container picks up the UAM labels and
   `labels_pending` becomes False. A failure here does **not** undo the import. The
   app stays imported with `labels_pending=True`, and `needs_attention` is set.
8. If `options.archive_flash_template` is set, move the flash template to
   `/data/template-backups/imported/` after a successful recreate only.

## Acceptance
Unit tests with fakes and fixture `inspect` payloads cover:
- every matching rule and its confidence ordering;
- override derivation (unchanged default → no override; changed path; masked env);
- the dropped list;
- the tag pin;
- port pinning outside the range;
- proxy matching by domain and by port;
- stale plan conflict;
- rollback when the NPM claim fails;
- recreate failure leaving the app imported and flagged;
- a locally built image producing a blocker.
