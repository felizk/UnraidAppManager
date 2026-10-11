# T11 — Template export to flash

**Model:** Sonnet. **Phase:** 2. **Depends on:** T00 (use the fakes for the codec, spec resolver and repositories).

## Goal
Write Unraid user templates to the flash drive, but only when the user triggers it, with
a preview step first.

## Owns
`uam/services/template_export.py`, `tests/unit/services/test_template_export.py`

## API
```python
@dataclass(frozen=True) class ExportPlan: app_id: int; path: str; new_xml: str; old_xml: str | None; diff: str; changed: bool
async def plan(app_ids: list[int] | None) -> list[ExportPlan]      # None = all installed apps
async def apply(plans: list[ExportPlan], include_secrets: bool | None = None) -> list[str]  # returns written paths
```

## Requirements
- File name: `my-<template.name>.xml` in `Settings.template_export_dir`. Strip path
  separators and other unsafe characters from the name.
- Content: the template fetched at `installed_template_commit`, with every config `value`
  set to the resolved value (the actual host port for Ports, the override value for
  others) and `Name` set to `app.name`. Masked configs get their value only if
  `include_secrets` is set. Unknown elements are preserved through the codec.
- `plan` produces a unified diff (`difflib`) against the existing file, if there is one.
- `apply` re-reads the file and, if it changed since the plan was made, raises
  `ConflictError` (stale plan). It backs up the old file to
  `/data/template-backups/<UTC timestamp>/` and writes atomically: a temporary file in
  the same directory, `fsync`, then `os.replace`. The flash drive is FAT, so don't rely
  on chmod.
- If the export directory is missing or read-only, raise `ValidationError` with a hint
  about the volume mapping.

## Acceptance
- Tests with `tmp_path`: first write, an unchanged file (`changed=False`, not written),
  the diff content, secrets excluded or included, a stale-plan conflict, a backup
  created, and an unsafe name sanitized.
