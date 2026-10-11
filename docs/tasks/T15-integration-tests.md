# T15 — Integration / E2E tests

**Model:** Sonnet. **Phase:** 4. **Depends on:** T14.

## Owns
`tests/integration/**`, `docker-compose.test.yml`, `.github/workflows/integration.yml`

## Scenarios (real Docker daemon, a real NPM container `jc21/nginx-proxy-manager`, and a
local registry `registry:2` standing in for GHCR; GitHub is mocked with a tiny FastAPI
stub serving the fixture repos)
1. Add a source, discover templates, add an app and install it. Then check that the
   container is running with the expected labels, the port comes from the range, and
   the NPM proxy host exists and forwards correctly (curl through NPM with a Host header).
2. Push a new image to the local registry under the same tag. Run the check: the
   update is detected, auto-applied, the new digest is running and the old container
   is gone.
3. Push an image whose healthcheck fails. The update rolls back, the old container is
   running and the state is `FAILED_UPDATE`.
4. Change the template in the stub so it adds a required variable. `needs_attention`
   is set and nothing is auto-applied.
5. Uninstall: the container, the NPM host and the port allocations are all gone.
6. Template export into a temp dir: the file matches the golden file.
7. Import: start a container directly with `docker run`, using dockerMan-style labels and
   a matching `my-<name>.xml` in the export dir, plus a hand-made NPM proxy host. Then
   scan, plan and apply. Check that the container was recreated with UAM labels, has
   the same image digest, the same host port and the same env, and that the NPM host
   was claimed (and not duplicated).
8. Two apps that both prefer the same port get different host ports.

Mark the tests `@pytest.mark.integration`. They run in CI through `integration.yml`
(GitHub-hosted runners have Docker).
