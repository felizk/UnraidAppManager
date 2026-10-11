# Unraid dockerMan labels: how `net.unraid.docker.*` is interpreted

Research for T05. Source: `unraid/webgui`, branch `master`, fetched 2026-10-11 (raw file download,
line numbers counted on that copy; the commit SHA was not recorded, so lines may drift in later
versions). Base path: `emhttp/plugins/dynamix.docker.manager/`.

## 1. Where the labels are read

All label reads happen in `DockerClient::getDockerContainers()`,
`include/DockerClient.php`:

| Line | Code (abridged) | Stored in |
|---|---|---|
| 1015 | `$c['Manager'] = ...Labels['net.unraid.docker.managed'] ?? false;` | `Manager` |
| 1016 | `if ($c['Manager'] == 'composeman') { $c['ComposeProject'] = ...Labels['com.docker.compose.project']; }` | compose detection |
| 1022 | `$c['Icon'] = ...Labels['net.unraid.docker.icon'] ?? false;` | `Icon` |
| 1023 | `$c['Url'] = ...Labels['net.unraid.docker.webui'] ?? false;` | `Url` |
| 1024-1025 | `net.unraid.docker.tailscale.webui` / `.tailscale.hostname` | `TSUrl`, `TSHostname` |
| 1026 | `$c['Shell'] = ...Labels['net.unraid.docker.shell'] ?? false;` | `Shell` |
| 1027 | `$c['Manager'] = ...Labels['net.unraid.docker.managed'] ?? false;` (duplicate of 1015) | `Manager` |

`?? false` means an absent label yields `false`, not `null`.

## 2. `net.unraid.docker.managed`

* Written by dockerMan when it creates a container: `include/Helpers.php:441`
  (`$Labels[] = 'net.unraid.docker.managed=dockerman';`). The `-l ...managed=dockerman` string is
  also string-matched in `include/CreateDocker.php:221` and `:301` (Tailscale env injection).
* Read at `DockerClient.php:1015/1027` into `$ct['Manager']`.
* Consumed by exactly these checks (all compare against the literal `dockerman`):
  * `DockerClient.php:415`: `if ($ct['Manager'] !== 'dockerman') { $tmp['template'] = null; $tmp['updated'] = null; }`
    inside `DockerClient::getAllInfo()`. The `else if` branch (`:418` onward) is the only place
    that loads the user template (`getUserTemplate($name)`) and the update status
    (`getUpdateStatus($image)`) into the per-container info.
  * `include/DockerContainers.php:158`, `:169`, `:182`: update column. Only `dockerman` shows
    "up-to-date"/"force update" (`:158-160`) and "apply update" (`:169-171`) and the not-available
    "force update" (`:182-184`); any other value falls to the `else` branch that prints
    "3rd Party" (or "Compose" when a compose project label is present).
  * `include/DockerContainers.php:297`: the autostart checkbox is only rendered for `dockerman`;
    others show "3rd Party".
  * `DockerClient.php:1016`: the value `composeman` is special-cased for Compose Manager.

## 3. `.icon`, `.webui`, `.shell`

* Written by dockerMan from the template: `Helpers.php:442` (`webui`, only if non-empty) and
  `:443` (`icon`, only if non-empty). `shell` is not written by dockerMan; it is a user/third-party
  label.
* `.webui`: `DockerClient.php:331` `$tmp['url'] = $ct['Url'] ?? $tmp['url'] ?? '';` then, when
  running, `:366-368` expand `[IP]` and `[PORT:n]` placeholders (`getControlURL`) unless the URL
  has no placeholders (then used as-is). The label is used regardless of the `managed` value, and
  takes precedence over the template's `WebUI` (`getTemplateValue(..., 'WebUI')` is only the
  fallback, ~`:364`).
* `.icon`: `DockerClient.php:338` `$labelIconUrl = $ct['Icon'] ?? null;`, passed at `:343` to
  `getIcon($image, $name, $labelIconUrl)` (`:433`), which prefers the template `Icon` and uses the
  label URL only as fallback (`:435-436`), then caches a downloaded PNG. Independent of `managed`.
* `.shell`: `DockerClient.php:333` `if (isset($ct['Shell'])) $tmp['shell'] = $ct['Shell'];`
  (always set because of `?? false`); `:406-407` fall back to the template's `Shell` when the value
  is falsy. The value feeds the console menu entry (`DockerContainers.php:84`, passed into
  `addDockerContainerContext` at `:93`). Independent of `managed`.

## 4. Does a non-`dockerman` `managed` value stop update checks and "Edit"?

**Update checks: partially. The UI stops showing and offering them; the underlying check is not
label-gated.**
* `DockerClient.php:415-417` nulls `updated` for any container whose `managed` is not exactly
  `dockerman` (strict `!==`, so `uam`, `composeman`, empty or absent all count). With `updated`
  null, `DockerContainers.php:82` computes `$updateStatus = 3`, and `:182-190` render "3rd Party"
  with no apply/force-update link (`:158-190`).
* The image-level check `DockerUpdate::reloadUpdateStatus()` (`DockerClient.php:612-628`) iterates
  images (or one image), not containers, and does not read the label. So Unraid may still compute
  remote digests for the image; it just does not surface them for the container. Whether other
  code paths (the scheduled auto-update / "Check for updates" scripts, Community Applications
  Auto Update plugin) skip non-`dockerman` containers was NOT verified.

**Edit: yes, the name-link edit is lost.**
* `getUserTemplate($name)` is only called in the `dockerman` branch (`DockerClient.php:418-419`);
  otherwise `template` is `null`.
* `DockerContainers.php:83` `$template = $info['template'] ?? '';` and `:139-140`: the container
  name is only rendered as an `editContainer(...)` link `if ($template && empty($composestack))`.
  Non-`dockerman` containers get plain text.
* The template string is also passed into the right-click menu (`DockerContainers.php:93`).
  I did not locate `addDockerContainerContext` (JavaScript) in the fetched files, so whether the
  menu's "Edit" item is also hidden when the template is empty is NOT verified (very likely, since
  it receives an empty template path).

Other effects of a non-`dockerman` value: no autostart checkbox (`DockerContainers.php:297`),
no "force update". Icon, WebUI, shell and start/stop/logs/console actions keep working.

## 5. Consequences for UAM

* UAM must not set `managed=dockerman` if it wants Unraid to stay out of the container's
  lifecycle: with `dockerman`, Unraid would offer its own update and edit, which would recreate the
  container from a flash template and drop UAM's labels. A distinct value (default `uam`) disables
  Unraid's update/Edit UI for the container as per section 4.
* The cost: UAM-managed containers show "3rd Party" in the Unraid Docker tab and have no autostart
  checkbox there (restart policy is handled by UAM via Docker's `restart_policy`).
* `net.unraid.docker.icon` and `.webui` are honoured regardless of `managed`, so keep setting them.
  Note `[IP]`/`[PORT:n]` are expanded by Unraid at display time.
* Import (T16): `managed == "dockerman"` identifies containers created by Unraid; they have a flash
  template under `/boot/config/plugins/dockerMan/templates-user/my-<name>.xml`
  (path convention from memory; not verified in this research).

## 6. Not verified

* The exact commit of the fetched `master`; line numbers may differ on other Unraid versions.
* The JavaScript that builds the context menu (Edit/Logs/Console items).
* Scheduled auto-update scripts and plugin behaviour for non-`dockerman` containers.
* The flash template path convention above.
