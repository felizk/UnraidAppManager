"""One positive and one negative case per rule id (excluding the --base rules)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.lint.helpers import PATH_CFG, PORT, cfg, ids, lint_one, template, write
from uam.lint.engine import lint_repo
from uam.lint.extraparams import unsupported_flags

KIT = Path(__file__).resolve().parents[3] / "docs" / "app-repo-kit"


def test_clean_template_has_no_findings(tmp_path: Path) -> None:
    assert lint_one(tmp_path, template()) == []


def test_kit_template_is_clean() -> None:
    findings, checked = lint_repo(KIT)
    assert checked == 1
    assert [f for f in findings if f.level != "info"] == []


@pytest.mark.parametrize("xml", ["<Container><Name>x</Name>", "<Other/>"])
def test_uam001_positive(tmp_path: Path, xml: str) -> None:
    assert ids(lint_one(tmp_path, xml)) == ["UAM001"]


def test_uam001_codec_error_and_negative(tmp_path: Path) -> None:
    bad = template(configs=(cfg("Bogus", "1"),))
    assert "UAM001" in ids(lint_one(tmp_path, bad))


def test_uam001_skips_non_container_xml_at_root(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", "<project/>")
    assert lint_repo(tmp_path) == ([], 0)


@pytest.mark.parametrize("name", [None, "", "-bad", "has space"])
def test_uam002_positive(tmp_path: Path, name: str | None) -> None:
    found = lint_one(tmp_path, template(name=name))
    assert ids(found) == ["UAM002"]


def test_uam002_missing_name_not_reported_as_uam001(tmp_path: Path) -> None:
    assert "UAM001" not in ids(lint_one(tmp_path, template(name=None, network="host")))
    # other rules still run despite the missing name
    assert "UAM205" in ids(lint_one(tmp_path, template(name=None, network="host")))


@pytest.mark.parametrize("repo", [None, "", "Bad Name", "ghcr.io/o/a:", "o/a@sha256:xyz"])
def test_uam003_positive(tmp_path: Path, repo: str | None) -> None:
    assert ids(lint_one(tmp_path, template(repository=repo))) == ["UAM003"]


@pytest.mark.parametrize(
    "repo", ["nginx", "o/a:1", "ghcr.io/o/a:latest", "localhost:5000/a/b", "ghcr.io/OWNER/a"]
)
def test_uam003_negative(tmp_path: Path, repo: str) -> None:
    assert "UAM003" not in ids(lint_one(tmp_path, template(repository=repo)))


def test_uam004(tmp_path: Path) -> None:
    dup = template(configs=(PORT, PATH_CFG, cfg("Variable", "A"), cfg("Variable", "A")))
    found = lint_one(tmp_path, dup)
    assert ids(found) == ["UAM004"]
    assert found[0].line == 13


def test_uam004_negative_same_target_different_type_or_proto(tmp_path: Path) -> None:
    udp = cfg("Port", "8080", default="8080", mode="udp")
    ok = template(configs=(PORT, udp, cfg("Variable", "8080")))
    assert lint_one(tmp_path, ok) == []


@pytest.mark.parametrize(
    ("target", "mode"), [("0", "tcp"), ("70000", "tcp"), ("abc", "tcp"), ("80", "sctp"), ("80", "")]
)
def test_uam005_positive(tmp_path: Path, target: str, mode: str) -> None:
    xml = template(configs=(cfg("Port", target, default="1", mode=mode),), webui="")
    assert "UAM005" in ids(lint_one(tmp_path, xml))


def test_uam005_negative(tmp_path: Path) -> None:
    xml = template(configs=(cfg("Port", "65535", default="1", mode="udp"),), webui="")
    assert "UAM005" not in ids(lint_one(tmp_path, xml))


def test_uam006(tmp_path: Path) -> None:
    found = lint_one(tmp_path, template(webui="http://[IP]:[PORT:9999]/"))
    assert ids(found) == ["UAM006"]
    assert found[0].line is not None


def test_uam006_negative_no_port_placeholder(tmp_path: Path) -> None:
    assert "UAM006" not in ids(lint_one(tmp_path, template(webui="http://example.com/")))


def test_uam007(tmp_path: Path) -> None:
    write(tmp_path, "unraid/ok.xml", template())
    write(tmp_path, "deploy/app.xml", template())
    write(tmp_path, "unraid/deep/app.xml", template())
    write(tmp_path, "docs/other.xml", "<project/>")
    found = lint_repo(tmp_path)[0]
    assert [(f.rule, f.file) for f in found] == [
        ("UAM007", "deploy/app.xml"),
        ("UAM007", "unraid/deep/app.xml"),
    ]


@pytest.mark.parametrize("rel", ["app.xml", "unraid/a.xml", ".unraid/a.xml", "templates/a.xml"])
def test_uam007_negative_discovery_locations(tmp_path: Path, rel: str) -> None:
    assert lint_one(tmp_path, template(), rel) == []


def test_uam201(tmp_path: Path) -> None:
    legacy = "<Networking><Mode>bridge</Mode></Networking>"
    assert "UAM201" in ids(lint_one(tmp_path, template(legacy=legacy)))


def test_uam201_negative(tmp_path: Path) -> None:
    assert "UAM201" not in ids(lint_one(tmp_path, template()))


def test_uam202(tmp_path: Path) -> None:
    found = lint_one(tmp_path, template(extra_params="--restart=always --privileged -m 1g --ulimit nofile=1"))
    assert [f.rule for f in found] == ["UAM202"]
    assert "--privileged" in found[0].message
    assert "--ulimit" in found[0].message
    assert "--restart" not in found[0].message


def test_uam202_negative(tmp_path: Path) -> None:
    ok = "--restart unless-stopped --memory=1g --cpus 2 -e A=B -l x=y --network host --log-opt max-size=1m"
    assert "UAM202" not in ids(lint_one(tmp_path, template(extra_params=ok)))


def test_unsupported_flags_skips_values() -> None:
    assert unsupported_flags("--hostname -weird-value --foo") == ["--foo"]


@pytest.mark.parametrize("target", ["DB_PASSWORD", "API_TOKEN", "CLIENT_SECRET", "SSH_KEY"])
def test_uam203_positive(tmp_path: Path, target: str) -> None:
    xml = template(configs=(PORT, cfg("Variable", target, default="")))
    assert ids(lint_one(tmp_path, xml)) == ["UAM203"]


def test_uam203_negative_masked_or_other_type(tmp_path: Path) -> None:
    xml = template(configs=(PORT, cfg("Variable", "TOKEN", mask="true"), cfg("Path", "/keys", mode="rw")))
    assert "UAM203" not in ids(lint_one(tmp_path, xml))


@pytest.mark.parametrize("ctype", ["Variable", "Path"])
def test_uam204_positive(tmp_path: Path, ctype: str) -> None:
    xml = template(configs=(PORT, cfg(ctype, "/x" if ctype == "Path" else "X", default="", required="true")))
    assert ids(lint_one(tmp_path, xml)) == ["UAM204"]


def test_uam204_negative(tmp_path: Path) -> None:
    xml = template(
        configs=(
            PORT,
            cfg("Variable", "X", default="", required="false"),
            cfg("Variable", "Y", required="true"),
        )
    )
    assert "UAM204" not in ids(lint_one(tmp_path, xml))


def test_uam205(tmp_path: Path) -> None:
    assert ids(lint_one(tmp_path, template(network="host"))) == ["UAM205"]


def test_uam205_negative(tmp_path: Path) -> None:
    assert "UAM205" not in ids(lint_one(tmp_path, template(network="bridge")))


def test_uam206(tmp_path: Path) -> None:
    write(tmp_path, "Dockerfile", "FROM scratch\n")
    assert ids(lint_one(tmp_path, template())) == ["UAM206"]


@pytest.mark.parametrize(
    "dockerfile", ["FROM x\nHEALTHCHECK CMD true\n", "FROM x\n  healthcheck --interval=5s CMD true\n"]
)
def test_uam206_negative(tmp_path: Path, dockerfile: str) -> None:
    write(tmp_path, "Dockerfile", dockerfile)
    assert lint_one(tmp_path, template()) == []


def test_uam206_no_dockerfile_is_silent(tmp_path: Path) -> None:
    assert "UAM206" not in ids(lint_one(tmp_path, template()))


@pytest.mark.parametrize("repo", ["ghcr.io/o/a:1.4.2", "o/a:v2.0.0-alpine", "o/a@sha256:" + "a" * 64])
def test_uam301_positive(tmp_path: Path, repo: str) -> None:
    assert ids(lint_one(tmp_path, template(repository=repo))) == ["UAM301"]


@pytest.mark.parametrize("repo", ["ghcr.io/o/a:latest", "o/a:1", "o/a:1.4", "o/a", "o/a:stable"])
def test_uam301_negative(tmp_path: Path, repo: str) -> None:
    assert "UAM301" not in ids(lint_one(tmp_path, template(repository=repo)))


def test_uam302(tmp_path: Path) -> None:
    assert ids(lint_one(tmp_path, template(repository="lscr.io/linuxserver/x:latest"))) == ["UAM302"]


@pytest.mark.parametrize("repo", ["ghcr.io/o/a", "docker.io/o/a", "o/a", "index.docker.io/o/a"])
def test_uam302_negative(tmp_path: Path, repo: str) -> None:
    assert "UAM302" not in ids(lint_one(tmp_path, template(repository=repo)))


@pytest.mark.parametrize("field", ["icon", "webui", "project"])
def test_uam303_positive(tmp_path: Path, field: str) -> None:
    kwargs: dict[str, str | None] = {field: None}
    found = lint_one(tmp_path, template(**kwargs))  # type: ignore[arg-type]
    assert "UAM303" in ids(found)


def test_uam303_negative(tmp_path: Path) -> None:
    assert "UAM303" not in ids(lint_one(tmp_path, template()))
