# T10 — Update checker and scheduler

**Model:** Sonnet. **Phase:** 2. **Depends on:** T00 (fakes). Calls `Orchestrator.update`
through a small Protocol defined in this module (`Updater`), so it does not need T09.

## Goal
Run tiered update checks and apply automatic updates where the app allows it.

## Owns
`uam/services/update_checker.py`, `uam/services/scheduler.py`, `tests/unit/services/test_update_checker.py`, `tests/unit/services/test_scheduler.py`

## UpdateChecker
`check(app) -> UpdateCheckResult`:
1. Template: `latest_commit_for_path(source, app.template_path)`. If the result is not
   `None` and its sha differs from `installed_template_commit`, fetch the template,
   parse it, and compare it with the installed template (fetched at
   `installed_template_commit`, or a cached copy). Compute a `TemplateDiff`: added,
   removed and changed configs, plus image changes. A new required config with no
   default and no override sets `needs_attention`.
2. Image: `RegistryClient.get_digest(resolved image, vault.registry_auth_for(image))`
   compared with `installed_image_digest`.
3. Persist the `available_*` fields, `last_checked_at` and `needs_attention`. If anything
   new was detected, set `last_change_at = now`.
4. Scheduling: the app is hot if `last_change_at` is within `hot_window_hours`. Then
   `next_check_at = now + (hot_interval_minutes if hot else cold_interval_hours) * jitter(0.9..1.1)`.
   If `GitHubClient.rate_limit_remaining_fraction() < 0.1`, double the hot interval.
   On `RateLimited(reset_at)`, set `next_check_at = reset_at + 1 min`.
5. Auto-apply: if `image_changed and app.auto_update_image`, or
   `template_changed and app.auto_update_template and not needs_attention`, call
   `updater.update(...)`. Errors are logged and recorded, and are never re-raised.

`check_now(app_id)` runs one check immediately (used by the UI).

## Scheduler
- APScheduler `AsyncIOScheduler` with one job, `tick`, every 60 s (`max_instances=1`,
  `coalesce=True`).
- `tick`: take the apps from `due_for_check(now)` and check them with a concurrency limit
  of 4 (`asyncio.Semaphore`). Apps without `next_check_at` are due immediately.
- Also run `Orchestrator.sync_state()` every 5 minutes through a second job.
- `start()` and `shutdown()` hook into the FastAPI lifespan (T14 does the wiring).

## Acceptance
- With `frozen_now` plus the fakes: hot vs cold intervals, jitter bounds, rate-limit
  backoff, 304 meaning no template fetch, digest change triggering auto-update, a
  required var blocking auto-apply, and a failing check not stopping the other apps in
  the tick.
