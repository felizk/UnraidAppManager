# T07 — Nginx Proxy Manager client

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `ProxyManager` (`uam/clients/npm.py`, class `NpmClient`) against the NPM v2 REST API.

## Owns
`uam/clients/npm.py`, `tests/unit/clients/test_npm.py`, `docs/npm-api-notes.md`

## Constructor inputs
`base_url`, `identity`, `secret` (from Vault), and an httpx client.

## Requirements
- Auth: `POST /api/tokens {"identity","secret"}` returns `{token, expires}`. Cache the
  token, refresh it 5 minutes before it expires, and refresh once on any 401.
- `find_by_domain`: `GET /api/nginx/proxy-hosts`, then match `domain_names`
  case-insensitively.
- `ensure_proxy_host(domain, forward_host, forward_port, settings, existing_id)`:
  - Choose the target host: `existing_id` if it still exists, otherwise
    `find_by_domain(domain)`.
  - If there is a target, `PUT /api/nginx/proxy-hosts/{id}`. Only send a PUT when a
    managed field differs, which keeps the call idempotent.
  - If not, `POST /api/nginx/proxy-hosts` with `domain_names`, `forward_scheme: "http"`,
    `forward_host`, `forward_port`, `certificate_id` (0 when none), `ssl_forced`,
    `allow_websocket_upgrade`, `access_list_id` (0 when none), `block_exploits: true`,
    `http2_support: true`, `caching_enabled: false`, `meta: {}`, `advanced_config: ""`,
    `locations: []`.
  - Return the id.
  - If a host for that domain exists but was **not** created by UAM, raise
    `ConflictError`. UAM-created hosts carry a marker comment in `advanced_config`:
    `# managed-by: unraid-app-manager app=<name>`. Set `advanced_config` to that marker
    on create.
- `delete_proxy_host`: `DELETE /api/nginx/proxy-hosts/{id}`. A 404 is treated as success.
- `list_certificates` → `GET /api/nginx/certificates` as `(id, nice_name)`.
  `list_access_lists` → `GET /api/nginx/access-lists`.
- Map errors to the types in `uam.errors`.

## Acceptance
- respx tests: token caching and refresh on 401, create, idempotent ensure (no PUT when
  nothing changed), update when the port changed, adopting an existing UAM-marked host,
  refusing a foreign host, and a delete returning 404.
- Write a short `docs/npm-api-notes.md` that lists the payload fields you verified against
  the NPM source (`backend/schema/components/proxy-host-object.json`).
