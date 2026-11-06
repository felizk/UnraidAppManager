from __future__ import annotations

from dataclasses import replace

import pytest

from tests.fakes import FakeBundle, make_app, make_source
from uam.domain import Source
from uam.errors import ConflictError, NotFound, ValidationError
from uam.services.source_move import SourceMover

XML = "<Container><Name>notes</Name></Container>"
FORGE = "https://git.example.com"


def mover(fakes: FakeBundle) -> SourceMover:
    return SourceMover(fakes.sources, fakes.apps, fakes.github, fakes.vault)


async def setup(fakes: FakeBundle, installed: str = "c1") -> tuple[Source, Source]:
    source = await fakes.sources.add(make_source())
    await fakes.apps.add(make_app(source_id=source.id, installed_template_commit=installed))
    # FakeGitHub ignores forge_url, so the "mirror" lives under another owner.
    target = replace(source, owner="mirror", forge_url=FORGE)
    return source, target


async def test_mirror_with_same_commit_moves(fakes: FakeBundle) -> None:
    source, target = await setup(fakes)
    fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c1")

    plan = await mover(fakes).move(source.id, target)

    assert plan.ok
    assert "same as installed" in plan.checks[0].detail
    assert plan.checks[0].app_names == ("notes",)
    moved = await fakes.sources.get(source.id)
    assert (moved.owner, moved.forge_url) == ("mirror", FORGE)
    assert (await fakes.apps.list())[0].source_id == source.id


async def test_installed_commit_in_history_is_ok(fakes: FakeBundle) -> None:
    source, target = await setup(fakes)
    fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c2")
    fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, ref="c1", commit_sha="c1")

    plan = await mover(fakes).plan(source.id, target)

    assert plan.ok and not plan.checks[0].warning
    assert "in its history" in plan.checks[0].detail


async def test_unrelated_history_warns_but_allows(fakes: FakeBundle) -> None:
    source, target = await setup(fakes)
    fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c2")

    plan = await mover(fakes).plan(source.id, target)

    assert plan.ok and plan.checks[0].warning
    assert "offered as an update" in plan.checks[0].detail


async def test_missing_template_blocks_move(fakes: FakeBundle) -> None:
    source, target = await setup(fakes)

    plan = await mover(fakes).plan(source.id, target)
    assert not plan.ok
    with pytest.raises(ConflictError):
        await mover(fakes).move(source.id, target)
    assert (await fakes.sources.get(source.id)).forge_url == ""


async def test_source_without_apps_checks_discovery(fakes: FakeBundle) -> None:
    source = await fakes.sources.add(make_source())
    fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML)

    plan = await mover(fakes).plan(source.id, replace(source, owner="mirror", forge_url=FORGE))

    assert plan.ok
    assert "1 template(s) found" in plan.checks[0].detail


async def test_private_target_uses_new_credential(fakes: FakeBundle) -> None:
    source, target = await setup(fakes)
    fakes.github.put_file("mirror", "notes", "unraid/notes.xml", XML, commit_sha="c1")
    fakes.github.make_private("mirror", "notes", "fj-token")
    from uam.domain import CredentialKind

    cred = await fakes.vault.store("forgejo", CredentialKind.FORGEJO_TOKEN, "me", "fj-token", FORGE)

    assert not (await mover(fakes).plan(source.id, target)).ok
    assert (await mover(fakes).plan(source.id, replace(target, credential_id=cred.id))).ok


async def test_same_location_and_unknown_credential_rejected(fakes: FakeBundle) -> None:
    source, target = await setup(fakes)
    with pytest.raises(ValidationError):
        await mover(fakes).plan(source.id, source)
    with pytest.raises(NotFound):
        await mover(fakes).plan(source.id, replace(target, credential_id=99))
