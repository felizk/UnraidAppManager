# T13 — Web UI (HTMX)

**Model:** Sonnet. **Phase:** 3. **Depends on:** T12 (calls the same service layer; the UI routes render HTML).

## Owns
`uam/web/ui/**`, `uam/web/templates/**`, `uam/web/static/**`, `tests/unit/web/test_ui_*.py`

## Stack
Jinja2, HTMX 2.x and Pico.css, vendored into `static/` with their versions pinned (no CDN
at runtime). Dark mode follows the system setting. The UI is mobile-friendly. There is
no JS build step.

## Pages
1. **Dashboard** `/`: one card per app with icon, name, state badge, WebUI link, an
   "update available" badge (template and/or image), needs-attention warnings, quick
   actions (start/stop/restart/update/check now), and the last-checked time. Cards
   auto-refresh with `hx-trigger="every 10s"` and also while an operation is in
   progress.
2. **Sources** `/sources`: list, add (owner/repo or a pasted GitHub URL, branch, optional
   path, credential dropdown), discover templates, and an "Add app" button per template.
3. **Add/Edit app** `/apps/new?source=&path=` and `/apps/{id}/edit`: a form generated
   from the template configs, grouped as Ports/Paths/Variables/Labels/Devices. Fields
   with `display=advanced*` go in a collapsible section. Masked fields are password
   inputs. Ports show "auto (next free: N)" when auto_ports is on. Further fields:
   subdomain with a live `subdomain.base_domain` preview, cert and access-list dropdowns,
   auto-update toggles, and the image tag override. Validation errors are shown inline.
4. **App detail** `/apps/{id}`: config summary, port table, NPM link, a template diff
   when an update is available, an events timeline, and a logs tail (with a refresh
   button).
5. **Templates export** `/export`: select apps, "Preview" shows the diffs (`<pre>` with
   +/- colouring), then "Write to flash" with an include-secrets checkbox.
6. **Import** `/import` (feature F8, service T16):
   - A table of candidates: container, image, Unraid-managed badge, best source match
     with its confidence, matched subdomain, and blockers.
   - Each row has an "Import…" wizard:
     1. Choose the source: a dropdown of matches, or "new source" pre-filled with
        owner/repo. Choose the template.
     2. Options: keep ports, claim proxy host and subdomain, recreate now,
        auto-update, archive flash template.
     3. Review the plan: overrides table (masked values shown as `***`), pinned ports,
        the **dropped settings, highlighted in red**, the template diff and the
        warnings. Then confirm.
   - A multi-select "Import all high-confidence matches with defaults" action. It still
     shows a combined review step before applying.
7. **Credentials** `/credentials`: add/delete/test. Secrets are write-only.
8. **Settings** `/settings`: every `Settings` field, with a "Test NPM connection" button.
9. **Login/Setup.**

## Acceptance
- Route tests check that each page renders with fake data, forms post and redirect, and
  validation errors show up.
- Also include a Playwright smoke test (marked `@pytest.mark.e2e`, skipped by default)
  that logs in, adds a source with fakes, installs an app and sees the badge turn
  "running". Chromium is at `/opt/pw-browsers`.
