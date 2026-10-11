# ruff: noqa: E501
"""--base rules (UAM101-104) with a real temp git repo, plus CLI behaviour and golden output."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.unit.lint.helpers import PATH_CFG, PORT, cfg, ids, template, write
from uam.lint.cli import main
from uam.lint.engine import lint_repo


def git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "unraid/app.xml", template())
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def lint_base(root: Path) -> list[str]:
    return ids(lint_repo(root, "HEAD")[0])


def test_no_changes_is_clean(repo: Path) -> None:
    assert lint_repo(repo, "HEAD")[0] == []


def test_uam101_moved(repo: Path) -> None:
    (repo / "unraid/app.xml").rename(repo / "unraid/renamed.xml")
    found = lint_repo(repo, "HEAD")[0]
    assert ids(found) == ["UAM101"]
    assert found[0].file == "unraid/app.xml"


def test_uam101_deleted(repo: Path) -> None:
    (repo / "unraid/app.xml").unlink()
    assert lint_base(repo) == ["UAM101"]


def test_uam101_negative_edit_in_place(repo: Path) -> None:
    write(repo, "unraid/app.xml", template(project="https://example.com"))
    assert "UAM101" not in lint_base(repo)


def test_uam102_target_renamed(repo: Path) -> None:
    renamed = PATH_CFG.replace('Target="/config"', 'Target="/data"')
    write(repo, "unraid/app.xml", template(configs=(PORT, renamed)))
    found = lint_repo(repo, "HEAD")[0]
    assert ids(found) == ["UAM102"]
    assert "Path:/config" in found[0].message


def test_uam102_negative_added_config(repo: Path) -> None:
    write(repo, "unraid/app.xml", template(configs=(PORT, PATH_CFG, cfg("Variable", "NEW"))))
    assert lint_base(repo) == []


def test_uam103_required_without_default(repo: Path) -> None:
    new = cfg("Variable", "LICENSE", default="", required="true")
    write(repo, "unraid/app.xml", template(configs=(PORT, PATH_CFG, new)))
    assert lint_base(repo) == ["UAM103", "UAM204"]


def test_uam103_negative_with_default_or_existing(repo: Path) -> None:
    new = cfg("Variable", "LICENSE", default="abc", required="true")
    write(repo, "unraid/app.xml", template(configs=(PORT, PATH_CFG, new)))
    assert lint_base(repo) == []


def test_uam103_negative_already_existed_in_base(tmp_path: Path) -> None:
    old = cfg("Variable", "LICENSE", default="", required="true")
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "app.xml", template(configs=(PORT, old)))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    assert "UAM103" not in ids(lint_repo(tmp_path, "HEAD")[0])


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({"name": "other"}, ["UAM104"]), ({"repository": "ghcr.io/owner/other:latest"}, ["UAM104"])],
)
def test_uam104_positive(repo: Path, kwargs: dict[str, str], expected: list[str]) -> None:
    write(repo, "unraid/app.xml", template(**kwargs))  # type: ignore[arg-type]
    assert lint_base(repo) == expected


def test_uam104_negative_tag_change(repo: Path) -> None:
    write(repo, "unraid/app.xml", template(repository="ghcr.io/owner/myapp:stable"))
    assert lint_base(repo) == []


def test_new_template_not_in_base_is_fine(repo: Path) -> None:
    write(repo, "unraid/worker.xml", template(name="worker"))
    assert lint_base(repo) == []


def test_cli_bad_base_is_usage_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(repo), "--base", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_cli_missing_path_is_usage_error(tmp_path: Path) -> None:
    assert main([str(tmp_path / "missing")]) == 2


def test_cli_bad_option_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--format", "xml"])
    assert exc.value.code == 2


def test_cli_exit_codes_and_strict(tmp_path: Path) -> None:
    write(tmp_path, "unraid/app.xml", template(network="host"))
    assert main([str(tmp_path)]) == 0
    assert main([str(tmp_path), "--strict"]) == 1
    write(tmp_path, "unraid/app.xml", template(name=None))
    assert main([str(tmp_path)]) == 1


def test_cli_subdirectory_base_paths(repo: Path) -> None:
    (repo / "unraid/app.xml").unlink()
    assert main([str(repo), "--base", "HEAD"]) == 1


BROKEN = template(
    network="host",
    webui="http://[IP]:[PORT:9]/",
    configs=(PORT, cfg("Variable", "API_TOKEN", default="")),
)


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    write(tmp_path, "unraid/app.xml", BROKEN)
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    main(list(argv))
    return capsys.readouterr().out


def test_golden_text(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(broken)
    assert run(capsys, ".") == (
        "unraid/app.xml:6: error UAM006 WebUI refers to [PORT:9] but no Port config has Target 9\n"
        "unraid/app.xml:8: warning UAM205 Network is host; ports won't be mapped or proxied\n"
        'unraid/app.xml:11: warning UAM203 Variable API_TOKEN looks like a secret; set Mask="true"\n'
        "1 error(s), 2 warning(s), 0 info(s)\n"
    )


def test_golden_github(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(broken)
    assert run(capsys, ".", "--format", "github") == (
        "::error file=unraid/app.xml,line=6::UAM006 WebUI refers to [PORT:9] but no Port config has Target 9\n"
        "::warning file=unraid/app.xml,line=8::UAM205 Network is host; ports won't be mapped or proxied\n"
        '::warning file=unraid/app.xml,line=11::UAM203 Variable API_TOKEN looks like a secret; set Mask="true"\n'
    )


def test_golden_json(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(broken)
    out = json.loads(run(capsys, ".", "--format", "json"))
    assert out == {
        "findings": [
            {
                "rule": "UAM006",
                "level": "error",
                "file": "unraid/app.xml",
                "line": 6,
                "message": "WebUI refers to [PORT:9] but no Port config has Target 9",
            },
            {
                "rule": "UAM205",
                "level": "warning",
                "file": "unraid/app.xml",
                "line": 8,
                "message": "Network is host; ports won't be mapped or proxied",
            },
            {
                "rule": "UAM203",
                "level": "warning",
                "file": "unraid/app.xml",
                "line": 11,
                "message": 'Variable API_TOKEN looks like a secret; set Mask="true"',
            },
        ],
        "summary": {"errors": 1, "warnings": 2, "infos": 0},
    }


def test_github_format_prefixes_path_argument(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(broken.parent)
    out = run(capsys, broken.name, "--format", "github")
    assert out.startswith(f"::error file={broken.name}/unraid/app.xml,line=6::")
