from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import load_template
from tests.fakes import FakeBundle, make_app, make_source
from uam.domain import App, AppState
from uam.errors import ConflictError, ValidationError
from uam.services.template_export import TemplateExporter, safe_file_name

COMMIT = "abc123"
SECRET_XML = load_template("simple-web.xml").replace(
    '  <Config Name="TZ"',
    '  <Config Name="Token" Target="TOKEN" Default="" Mode="" Type="Variable" Mask="true">s3cret</Config>\n'
    '  <Config Name="TZ"',
)


def read(path: str | Path) -> str:
    return Path(path).read_text()


async def setup(
    fakes: FakeBundle, tmp_path: Path, name: str = "notes"
) -> tuple[TemplateExporter, App, Path, Path]:
    export, backups = tmp_path / "export", tmp_path / "backups"
    export.mkdir(exist_ok=True)
    s = await fakes.settings.get()
    s.template_export_dir = str(export)
    await fakes.settings.save(s)
    src = await fakes.sources.add(make_source())
    fakes.github.put_file("felizk", "notes", "unraid/notes.xml", SECRET_XML, ref=COMMIT, commit_sha=COMMIT)
    app = await fakes.apps.add(
        make_app(
            source_id=src.id,
            name=name,
            state=AppState.RUNNING,
            installed_template_commit=COMMIT,
            overrides={"Variable:TZ": "UTC"},
        )
    )
    svc = TemplateExporter(
        fakes.apps,
        fakes.sources,
        fakes.github,
        fakes.codec,
        fakes.spec_resolver,
        fakes.vault,
        fakes.settings,
        backup_dir=backups,
        clock=lambda: datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    )
    return svc, app, export, backups


async def test_first_write(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, _, export, _ = await setup(fakes, tmp_path)
    [plan] = await svc.plan(None)
    assert plan.changed and plan.old_xml is None
    assert plan.path == str(export / "my-notes.xml")
    assert "<Name>notes</Name>" in plan.new_xml
    assert ">UTC<" in plan.new_xml
    assert ">30000<" in plan.new_xml  # actual allocated host port
    assert await svc.apply([plan]) == [plan.path]
    assert read(plan.path) == plan.new_xml


async def test_unchanged_not_written(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, app, _, backups = await setup(fakes, tmp_path)
    await svc.apply(await svc.plan(None))
    [plan] = await svc.plan([app.id])
    assert not plan.changed and plan.diff == ""
    assert await svc.apply([plan]) == []
    assert not backups.exists()


async def test_diff_and_backup(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, app, export, backups = await setup(fakes, tmp_path)
    (export / "my-notes.xml").write_text("old\n")
    [plan] = await svc.plan([app.id])
    assert plan.changed and plan.old_xml == "old\n"
    assert "-old" in plan.diff and "+<?xml" in plan.diff
    await svc.apply([plan])
    assert (backups / "20260115T120000Z" / "my-notes.xml").read_text() == "old\n"
    assert [p.name for p in export.iterdir()] == ["my-notes.xml"]


@pytest.mark.parametrize("flag", [True, False])
async def test_secrets_setting(fakes: FakeBundle, tmp_path: Path, flag: bool) -> None:
    svc, *_ = await setup(fakes, tmp_path)
    s = await fakes.settings.get()
    s.template_export_include_secrets = flag
    await fakes.settings.save(s)
    [plan] = await svc.plan(None)
    assert ("s3cret" in plan.new_xml) is flag


async def test_apply_include_secrets_overrides(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, _, export, _ = await setup(fakes, tmp_path)
    s = await fakes.settings.get()
    s.template_export_include_secrets = True
    await fakes.settings.save(s)
    [plan] = await svc.plan(None)
    assert "s3cret" in plan.new_xml
    await svc.apply([plan], include_secrets=False)
    assert "s3cret" not in (export / "my-notes.xml").read_text()


async def test_stale_plan(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, _, export, _ = await setup(fakes, tmp_path)
    [plan] = await svc.plan(None)
    (export / "my-notes.xml").write_text("someone else\n")
    with pytest.raises(ConflictError):
        await svc.apply([plan])
    assert (export / "my-notes.xml").read_text() == "someone else\n"


async def test_unsafe_name_sanitized(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, _, export, _ = await setup(fakes, tmp_path, name="../evil/na me")
    [plan] = await svc.plan(None)
    assert Path(plan.path).parent == export
    assert Path(plan.path).name == "my-evil_na_me.xml"
    assert safe_file_name("a/b\\c") == "my-a_b_c.xml"
    assert safe_file_name("...") == "my-app.xml"


async def test_missing_dir(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, _, export, _ = await setup(fakes, tmp_path)
    export.rmdir()
    with pytest.raises(ValidationError, match="mapped"):
        await svc.plan(None)


async def test_all_skips_uninstalled(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, *_ = await setup(fakes, tmp_path)
    other = await fakes.apps.add(make_app(name="other"))
    assert len(await svc.plan(None)) == 1
    with pytest.raises(ValidationError):
        await svc.plan([other.id])


async def test_plan_include_secrets_overrides_setting(fakes: FakeBundle, tmp_path: Path) -> None:
    svc, *_ = await setup(fakes, tmp_path)
    s = await fakes.settings.get()
    s.template_export_include_secrets = True
    await fakes.settings.save(s)
    [plan] = await svc.plan(None, include_secrets=False)
    assert "s3cret" not in plan.new_xml
