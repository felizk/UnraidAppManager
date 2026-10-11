# T12 — REST API and auth

**Model:** Sonnet. **Phase:** 3. **Depends on:** T00. Service classes are injected as
FastAPI dependencies; in tests, override them with fakes or simple stubs whose method
names follow the T09–T11 briefs.

## Owns
`uam/web/api/**`, `uam/web/auth.py`, `uam/web/deps.py`, `uam/web/openapi.py`, `docs/openapi.json`, `tests/unit/web/test_api_*.py`

## Auth
- On first run (no password set), `/setup` takes the admin password. It is stored as an
  argon2 hash in the settings table, under a separate key that is never exposed.
- `POST /api/login` sets a signed session cookie (itsdangerous, `HttpOnly`,
  `SameSite=Lax`, 7 days). `POST /api/logout`.
- Every `/api/**` route except login/setup/health requires a session. The optional
  `UAM_API_TOKEN` env enables `Authorization: Bearer` for scripting.
- CSRF: require the `X-Requested-With: uam` header on mutating requests. HTMX sends it
  through `hx-headers`.

## Endpoints (JSON, Pydantic v2 models in `uam/web/api/schemas.py`)
```
GET  /api/health
GET/PUT  /api/settings
GET/POST /api/credentials            DELETE /api/credentials/{id}   POST /api/credentials/{id}/test
GET/POST /api/sources                DELETE /api/sources/{id}
GET  /api/sources/{id}/templates     # discover
GET  /api/sources/{id}/templates/preview?path=   # parsed template + config list for the add-app form
GET/POST /api/apps                   GET/PATCH/DELETE /api/apps/{id}   (DELETE ?delete_appdata=)
POST /api/apps/{id}/install|update|start|stop|restart|check
GET  /api/apps/{id}/logs?tail=       GET /api/apps/{id}/events
GET  /api/ports                      # allocations + in-use summary
GET  /api/npm/certificates           GET /api/npm/access-lists
POST /api/templates/export/plan      POST /api/templates/export/apply
GET  /api/events
```
- Credential responses never include secrets.
- Errors are mapped centrally: NotFound → 404, ConflictError → 409, ValidationError →
  422, AuthError → 401/403, RateLimited → 429 with `Retry-After`, DeployError → 500
  with the stage.
- Long operations (install/update) run as background tasks and return `202 {app_id}`
  straight away. The UI polls `GET /api/apps/{id}`.

## Acceptance
- httpx `AsyncClient` tests for auth (setup, login, rejected without a session, CSRF
  header), every endpoint's happy path, the error mapping, and secret redaction.
- The OpenAPI schema is generated, and `make openapi` writes it to `docs/openapi.json`.
