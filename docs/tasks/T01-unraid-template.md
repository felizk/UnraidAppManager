# T01 — Unraid template parse/render

**Model:** Sonnet. **Phase:** 1. **Depends on:** T00.

## Goal
Implement `TemplateCodec` (`uam/unraid_template/codec.py`, class `XmlTemplateCodec`).

## Owns
`uam/unraid_template/**`, `tests/unit/unraid_template/**`

## Requirements
- Parse with `defusedxml.ElementTree`. Raise `TemplateParseError` if the root is not `<Container>`.
- Map the known elements to `UnraidTemplate` fields: `Name, Repository, Registry, Network,
  Privileged, Shell, Support, Project, Overview, Category, WebUI, Icon, ExtraParams,
  PostArgs, CPUset`. Handle the `<Config>` attributes (`Name, Target, Default, Mode,
  Description, Type, Display, Required, Mask`) and the element text, which is the value.
- Booleans: accept `true/false/True/1/0`.
- **Legacy format:** convert `<Networking><Mode>`, `<Networking><Publish><Port><HostPort>
  <ContainerPort><Protocol>`, `<Data><Volume><HostDir><ContainerDir><Mode>` and
  `<Environment><Variable><Name><Value>` into `TemplateConfig` entries. Do not add a
  config when an equivalent `<Config>` already exists with the same target and type.
- Keep any element that isn't recognised in `unknown_elements` (tag and raw XML), and
  re-emit it on render in its original order after the known elements.
- `render()` produces the canonical Unraid layout: `<?xml version="1.0"?>`, a
  `<Container version="2">` root, 2-space indentation and the same element order Unraid
  uses. Text that contains `<` or `&` is escaped (not CDATA).
- Round trip: `parse(render(parse(x))) == parse(x)` for every fixture.
- Helper `webui_port(template) -> int | None`: extract `N` from `[PORT:N]` in `WebUI`.

## Acceptance
- Tests for every fixture, both formats, malformed XML, missing root, the round trip,
  and a property test (hypothesis is optional) for escaping.
