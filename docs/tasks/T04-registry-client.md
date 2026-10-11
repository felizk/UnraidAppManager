# T04 — Registry client (digest lookup)

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `RegistryClient` (`uam/clients/registry.py`, class `HttpRegistryClient`). It
returns the current manifest digest for an image tag without pulling the image.

## Owns
`uam/clients/registry.py`, `tests/unit/clients/test_registry.py`

## Requirements
- Parse the image ref into registry, repository and tag (default `latest`). `docker.io`
  maps to the API host `registry-1.docker.io` and adds `library/` for single-name images.
  If the ref already contains a digest, return that digest.
- `HEAD https://<host>/v2/<repo>/manifests/<tag>` with an `Accept` header listing OCI
  index, OCI manifest, Docker manifest list v2 and Docker manifest v2. Return the
  `Docker-Content-Digest` header. If HEAD has no digest header, fall back to GET and
  compute the sha256 of the body.
- Token auth: on a 401, parse `WWW-Authenticate: Bearer realm=…,service=…,scope=…` and
  request a token from the realm (using basic auth when `RegistryAuth` is given), then
  retry. Cache tokens per (host, repo, user) until `expires_in` runs out.
  - GHCR with a PAT: basic auth `username:PAT` against `https://ghcr.io/token`.
  - Anonymous GHCR for public images works through the same flow without basic auth.
- Errors: 401 after a token → `AuthError`; 404 → `NotFound`; 429 → `RateLimited`.
  Docker Hub's anonymous limit applies to GET only, so prefer HEAD.

## Acceptance
- respx tests: GHCR public, GHCR private (basic → bearer), Docker Hub `library/`, a
  multi-arch index digest, the HEAD-without-digest fallback, token caching (one token
  call for two lookups), and an error table.
