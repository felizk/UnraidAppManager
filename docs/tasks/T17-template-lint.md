# T17 — `uam-lint`: template and app-repo linter

**Model:** Sonnet. **Phase:** 2. **Depends on:** T01 (codec). The ExtraParams parser from
T08 is optional: if T08 isn't merged yet, keep a copy of the supported-flags list and
switch over later.

## Goal
A CLI that checks an app repository against the rules in
[`docs/app-repo-guide.md`](../app-repo-guide.md) §8, plus a composite GitHub Action for
app repos. The guide's rule table is the spec. Rule IDs, levels and meanings must match
it exactly.

## Owns
`uam/lint/**`, `action/lint-template/action.yml`, `tests/unit/lint/**`.
The console script `uam-lint = "uam.lint.cli:main"` is added to `pyproject.toml` by T00;
if it is missing, ask the orchestrator to add it.

## Requirements
- `uam-lint [PATH=.] [--base GIT_REF] [--format text|json|github] [--strict]`
  - Discovery is the same as T03: `*.xml` with a `<Container>` root in `unraid/`,
    `.unraid/`, `templates/` or the repo root (max depth 2). UAM007 is reported for
    `<Container>` XML found anywhere else in the repo.
  - `--base`: read the base versions with `git show <ref>:<path>` and
    `git ls-tree -r --name-only <ref>` (subprocess; no network). Apply UAM101–104.
  - `--format github` prints `::error file=…,line=…::UAM00X message` annotations.
    Get line numbers from a line-tracking parse, or by locating the element text.
  - Exit codes: 0 when clean, 1 on errors, 2 on usage errors. `--strict` also fails on
    warnings.
- Rules live in `uam/lint/rules.py` as small functions registered in a table:
  `(id, level, func(template, ctx) -> list[Finding])`. Only depend on the standard
  library and the codec, so `uvx` installs stay light.
- `action/lint-template/action.yml`: a composite action with the inputs `path`,
  `base`, `strict`. It sets up uv and runs `uvx --from ${{ github.action_path }}/../.. uam-lint`.

## Acceptance
- One test per rule ID, with a positive and a negative case.
- `--base` tests use a temp git repo (moved file, renamed target, required-without-default
  added).
- **`docs/app-repo-kit/unraid/myapp.xml` lints with zero errors and zero warnings.** This
  keeps the guide and the kit honest.
- A golden-output test for each output format.
