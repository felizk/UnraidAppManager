# T05 — Docker engine adapter

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `DockerEngine` (`uam/clients/docker_engine.py`, class `SdkDockerEngine`) using
the `docker` Python SDK, with every call wrapped in `asyncio.to_thread`.

## Owns
`uam/clients/docker_engine.py`, `tests/unit/clients/test_docker_engine.py`, `docs/unraid-labels.md`

## Requirements
- `pull(image, auth)`: pass `auth_config={"username","password"}` to `images.pull`. Never
  call `docker login` or write config files. Return the RepoDigest that matches the
  image's repository.
- `create(spec)`: map `ContainerSpec` to `containers.create(...)`: ports
  `{"8080/tcp": ("", 30001)}`, binds (`{host: {"bind": ctr, "mode": mode}}`), devices,
  env, labels, network_mode (a custom network name or `bridge`/`host`/`none`; ports are
  ignored for `host`), privileged, `restart_policy`, hostname, cap_add, `mem_limit`,
  `nano_cpus`, `cpuset_cpus`, `log_config`, and command. Connect `extra_networks` after
  creation. If the name already exists, raise `ConflictError`.
- `inspect` returns `None` for a missing container. Map `State.Health.Status` to `health`.
- `wait_healthy`: poll every 2 s. If a healthcheck is defined, wait for `healthy`, and
  fail on `unhealthy` or a timeout. If not, require `running` for `stable_s` seconds
  without a restart (watch `RestartCount`).
- `host_ip`: read `/host/proc/net/route` if it exists (default route interface), then
  `/host/proc/net/fib_trie`. Otherwise use the gateway of the docker0 bridge. The
  `Settings.host_ip` override is applied by the caller, not here.
- **Research deliverable:** `docs/unraid-labels.md`. From Unraid's dockerMan source
  (`emhttp/plugins/dynamix.docker.manager/include/DockerClient.php`), document how
  `net.unraid.docker.managed`, `.icon`, `.webui` and `.shell` are interpreted, and
  whether a non-`dockerman` value stops Unraid's update checks and "Edit" for
  the container. Cite file and line references.

## Tests
- Unit tests with `unittest.mock` patching the SDK client: verify the exact kwargs passed
  to `containers.create` for each fixture spec, and the `wait_healthy` state machine
  (simulated clock).
- A real-Docker smoke test marked `@pytest.mark.docker` (skipped by default): pull
  `alpine`, create, start, health, remove.
