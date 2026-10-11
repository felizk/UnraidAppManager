# NPM API notes

Verified against NginxProxyManager/nginx-proxy-manager (`develop`), `backend/schema/`.

## Auth: `POST /api/tokens`
Body `{identity, secret, scope?}` (`scope` only `user`). 200 returns `{token, expires}`
(`expires` ISO 8601 with `Z`, token is an RS512 JWT). The schema is a `oneOf` with
`token-challenge.json` (2FA); UAM treats a response without `token` as an `AuthError`.

## Proxy host object (`components/proxy-host-object.json`)
Fields: `id`, `created_on`, `modified_on`, `owner_user_id`, `domain_names`, `forward_scheme`
(`http`|`https`), `forward_host` (1-255), `forward_port` (1-65535), `access_list_id`,
`certificate_id`, `ssl_forced`, `caching_enabled`, `block_exploits`, `advanced_config` (string),
`meta` (object), `allow_websocket_upgrade`, `http2_support`, `enabled`, `locations[]`,
`hsts_enabled`, `hsts_subdomains`, `trust_forwarded_proto`; optional expansions
`certificate`, `owner`, `access_list`.

## POST / PUT `/api/nginx/proxy-hosts[/{id}]`
Both request schemas have `additionalProperties: false` and accept exactly: `domain_names`,
`forward_scheme`, `forward_host`, `forward_port`, `certificate_id`, `ssl_forced`,
`hsts_enabled`, `hsts_subdomains`, `trust_forwarded_proto`, `http2_support`, `block_exploits`,
`caching_enabled`, `allow_websocket_upgrade`, `access_list_id`, `advanced_config`, `enabled`,
`meta`, `locations`. POST requires `domain_names`, `forward_scheme`, `forward_host`,
`forward_port`. PUT has `minProperties: 1`. So UAM never echoes read-only fields (`id`,
`created_on`, ...) back in a PUT; it sends only the managed fields that differ.

## Not verified
- `common.json` types for `certificate_id`, `access_list_id` (UAM sends integer 0 for none,
  per the brief) and whether `ssl_forced: true` without a certificate is rejected.
- Response shapes of `/nginx/certificates` (`nice_name`) and `/nginx/access-lists` (`name`):
  taken from the brief and general knowledge, not fetched.
- The `backend/routes/` handlers (e.g. whether DELETE of a missing id returns 404) were not
  fetched.
