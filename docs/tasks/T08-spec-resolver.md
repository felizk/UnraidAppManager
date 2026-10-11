# T08 — Spec resolver

**Model:** Sonnet. **Phase:** 2. **Depends on:** T00 (use the fakes). The real T01 and T06
are only needed for the final wiring.

## Goal
Implement `SpecResolver` (`uam/services/spec_resolver.py`, class `DefaultSpecResolver`).
It turns `(App, UnraidTemplate)` into a `ResolvedApp`.

## Owns
`uam/services/spec_resolver.py`, `uam/services/extra_params.py`, `tests/unit/services/test_spec_resolver.py`, `tests/unit/services/test_extra_params.py`

## Constructor inputs
`PortAllocator` and `SettingsStore`.

## Requirements
- **Override keys:** `"<Type>:<Target>"`, for example `Variable:TZ`, `Path:/config`,
  `Port:8080/tcp`. The value is the override, else `config.value`, else `config.default`.
- **Ports:** if `app.auto_ports` is set, call
  `allocate(app.id, [(target, mode or tcp, preferred)])`, where `preferred` is the
  resolved value. If it is off, use the resolved value as-is. In both cases, check that
  every port is an int between 1 and 65535 and raise `ValidationError` otherwise. Skip
  ports when `network == "host"` and add a warning.
- **Paths:** substitute `[APPDATA]` → `Settings.appdata_root` (non-standard but handy).
  A required path with an empty value raises `ValidationError`. Mode defaults to `rw`.
- **Variables:** become env entries. A required variable with an empty value raises
  `ValidationError` listing every missing name.
- **Labels and Devices:** map directly.
- **Image:** `template.repository`, with the tag replaced by `app.image_tag_override`
  when that is set (keep the registry host and handle `host:port` correctly).
- **ExtraParams:** parse with `shlex` in `extra_params.py`. Supported flags: `--restart`,
  `--memory/-m`, `--cpus`, `--cpuset-cpus`, `--hostname/-h`, `--cap-add`,
  `--device`, `-e/--env`, `-l/--label`, `--log-opt`, `--network` (extra network),
  `--add-host` (map into a new field only if the contract allows it; otherwise add a
  warning). Both `--flag=value` and `--flag value` forms must work. Anything
  unsupported becomes a warning instead of an error.
- **PostArgs:** split with `shlex`. The result becomes `command`.
- **Labels:** add the UAM labels from contracts.md. `net.unraid.docker.webui` uses the
  WebUI string with `[PORT:N]` replaced by the host port allocated for container port
  N. Leave `[IP]` alone, because Unraid substitutes it.
- Fill in `webui_*` from the WebUI field.

## Acceptance
- Table-driven tests for every fixture template, the override precedence, the
  required-field errors, the ExtraParams matrix (at least 20 cases) and the image tag
  override (`ghcr.io/a/b:1` → `:2`, `localhost:5000/x` → `localhost:5000/x:dev`).
