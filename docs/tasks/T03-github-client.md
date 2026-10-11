# T03 — GitHub client

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `GitHubClient` (`uam/clients/github.py`, class `HttpGitHubClient`) using httpx
against `https://api.github.com` (base URL configurable for tests).

## Owns
`uam/clients/github.py`, `tests/unit/clients/test_github.py`

## Requirements
- Headers: `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, and
  `Authorization: Bearer <token>` when a token is given.
- `discover_templates`: if `source.template_path` is set, fetch only that path. Otherwise
  use the Git Trees API (`GET /repos/{o}/{r}/git/trees/{ref}?recursive=1`), take `*.xml`
  files under `unraid/`, `.unraid/` or `templates/`, or at the repo root (max depth 2),
  fetch each one, and keep the files whose root element is `<Container>` (cheap regex
  check; no full parse).
- `get_template`: Contents API at `ref`, base64-decoded. Get `commit_sha` and
  `committed_at` from `GET /commits?path=&sha=&per_page=1`.
- `latest_commit_for_path`: conditional request using an ETag stored through the injected
  `HttpCache` Protocol (`SqlHttpCache` in production, `InMemoryHttpCache` in tests). Cache keys include a hash of the token, never the token itself.
  A 304 returns `None`.
- Rate limits: track `x-ratelimit-remaining`/`-limit`/`-reset`. On a 403 or 429 with
  remaining = 0, raise `RateLimited(reset_at)`. `rate_limit_remaining_fraction()` returns
  the last value seen, or 1.0.
- 401 → `AuthError`, 404 → `NotFound` (a private repo with no token looks like a 404, and
  the error message should say so). Other 4xx/5xx → `RemoteError`.
- `validate_token`: `GET /user` and return the `x-oauth-scopes` header split into a list
  (fine-grained tokens return an empty list, which is acceptable).
- Retry 502/503/504 up to 3 times with backoff (use `asyncio.sleep`, patched in tests).

## Acceptance
- respx tests for every path above, including the ETag round trip and the 304.
