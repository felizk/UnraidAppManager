# T06 — Port allocator

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `PortAllocator` (`uam/ports/allocator.py`, class `DbPortAllocator`). It hands
out free host ports from the configured range and keeps each allocation stable for its app.

## Owns
`uam/ports/**`, `tests/unit/ports/**`

## Constructor inputs
A session factory (the `port_allocations` table), `SettingsStore`, `DockerEngine` (for
published ports), and an optional `HostListenerProbe`.

## Requirements
- A port is **in use** if any of these is true:
  (a) it is in the `port_allocations` table (any app);
  (b) `DockerEngine.list_containers()` reports it as published (any container, including
      ones UAM doesn't manage);
  (c) `HostListenerProbe` reports it, by parsing `/host/proc/net/{tcp,tcp6,udp,udp6}`
      for sockets in LISTEN state (TCP state `0A`) and bound UDP sockets, with ports
      decoded from hex. The probe is skipped if the path is missing;
  (d) it is in `Settings.reserved_ports`.
  A port is in use per (port, protocol), but **allocate the same number for TCP and UDP
  when an app asks for both on one container port.**
- `allocate(app_id, requests)`:
  - If an allocation already exists for `(app_id, container_port, protocol)`, return it.
  - Otherwise, if `preferred` is given, inside the range and free, use it. If not, pick
    the lowest free port in the range.
  - Run everything in one DB transaction. The unique constraint protects against races:
    on an `IntegrityError`, retry up to 3 times.
  - If no port is free, raise `PortRangeExhausted`.
  - Remove allocations this app has that are not in `requests`, because the template
    dropped those ports.
- `pin(app_id, bindings, ignore_container_id)` (used by import, T16): store the exact host
  ports, even outside the range. It ignores ports published by `ignore_container_id`
  (the container being imported), and raises `ConflictError` if another app's
  allocation or another container uses one of them. A later `allocate()` returns pinned
  rows unchanged, even when they are outside the range.
- `release(app_id)` deletes the app's rows.
- The `uam/ports/probe.py` helper is unit-tested with sample `/proc/net/tcp` content.

## Acceptance
- Tests: stability across calls, preferred port honoured or skipped, collision with a
  foreign container port, collision with a host listener, TCP+UDP pairing, range
  exhaustion, stale cleanup, pin outside the range and pin conflict, and concurrent allocations (asyncio.gather of 20 apps
  gives no duplicates).
