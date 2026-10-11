# T02 — Credential vault

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `Vault` (`uam/vault/vault.py`, class `FernetVault`) on top of the `credentials`
table from T00 (`uam/db/models.py: CredentialRow`). Also implement `FieldCipher`
(`uam/vault/cipher.py`, class `FernetFieldCipher`), using the same keys. `SqlAppRepository`
uses it to encrypt app overrides.

## Owns
`uam/vault/**`, `tests/unit/vault/**`

## Requirements
- Key loading (`uam/vault/keys.py`): use the env var `UAM_SECRET_KEY` (urlsafe base64) if
  set. Otherwise read `<data_dir>/secret.key`. If neither exists, generate the key file
  with mode 0600. Support key rotation through `MultiFernet`: `UAM_SECRET_KEY_OLD` may
  hold comma-separated older keys, and `rotate()` re-encrypts every row.
- `reveal()` raises `NotFound` for unknown ids, and `AuthError("cannot decrypt")` if the
  key is wrong.
- `registry_auth_for(image_ref)`: parse the host with `docker.io` as the default (handle
  `library/` images, `host:port` hosts and digests). Look for a REGISTRY credential whose
  `registry_host` matches. For `ghcr.io` with no match, fall back to the first GITHUB_PAT,
  using its username. Return `None` if nothing matches.
- `list()` and `get()` never include the secret.
- `FernetFieldCipher.decrypt` raises `AuthError("cannot decrypt")` when the key is wrong.
  `rotate()` must also re-encrypt `apps.overrides_ciphertext`.

## Acceptance
- Tests: round trip, wrong key, rotation, host parsing table
  (`nginx`, `ghcr.io/a/b:1`, `registry:5000/x`, `docker.io/library/x@sha256:…`), ghcr fallback.
