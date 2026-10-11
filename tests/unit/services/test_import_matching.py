from __future__ import annotations

from dataclasses import replace

from tests.fakes import make_source
from uam.domain import PortBinding, ProxyHost, UnraidTemplate
from uam.services.import_matching import (
    OCI_SOURCE_LABEL,
    choose_template,
    derive_subdomain,
    ghcr_owner_repo,
    match_proxy_host,
    match_sources,
    normalize_image,
    parse_github_url,
    split_image,
    template_matches_image,
)


def tpl(**kw: object) -> UnraidTemplate:
    base: dict[str, object] = {"name": "notes", "repository": "ghcr.io/felizk/notes:latest"}
    base.update(kw)
    return UnraidTemplate(**base)  # type: ignore[arg-type]


def test_parse_github_url() -> None:
    assert parse_github_url("https://github.com/felizk/notes") == ("felizk", "notes")
    assert parse_github_url("https://github.com/felizk/notes.git") == ("felizk", "notes")
    assert parse_github_url("see https://github.com/felizk/notes/issues") == ("felizk", "notes")
    assert parse_github_url("https://forums.unraid.net/topic/1") is None


def test_image_helpers() -> None:
    assert split_image("ghcr.io/a/b:1.2") == ("ghcr.io/a/b", "1.2")
    assert split_image("ghcr.io/a/b") == ("ghcr.io/a/b", "latest")
    assert split_image("localhost:5000/b") == ("localhost:5000/b", "latest")
    assert split_image("ghcr.io/a/b:1@sha256:abc") == ("ghcr.io/a/b", "1")
    assert normalize_image("docker.io/library/Nginx:1") == "nginx"
    assert ghcr_owner_repo("ghcr.io/Felizk/Notes:1") == ("Felizk", "Notes")
    assert ghcr_owner_repo("docker.io/a/b") is None


def test_label_match_is_high_and_uses_existing_source() -> None:
    src = replace(make_source(owner="felizk", repo="notes"), id=7)
    [m] = match_sources("nginx", {OCI_SOURCE_LABEL: "https://github.com/FelizK/Notes"}, None, [src])
    assert (m.confidence, m.source_id, m.owner, m.repo) == ("high", 7, "felizk", "notes")


def test_label_match_without_source_suggests_new() -> None:
    [m] = match_sources("nginx", {OCI_SOURCE_LABEL: "https://github.com/a/b"}, None, [])
    assert (m.confidence, m.source_id, m.owner, m.repo) == ("high", None, "a", "b")


def test_ghcr_image_existing_source_is_high_case_insensitive() -> None:
    src = replace(make_source(owner="felizk", repo="notes"), id=3)
    [m] = match_sources("ghcr.io/FelizK/Notes:1.2", {}, None, [src])
    assert (m.confidence, m.source_id) == ("high", 3)


def test_ghcr_image_without_source_is_medium_new() -> None:
    [m] = match_sources("ghcr.io/felizk/notes", {}, None, [])
    assert (m.confidence, m.source_id) == ("medium", None)


def test_flash_template_urls_are_medium() -> None:
    for kw in (
        {"project": "https://github.com/a/b"},
        {"support": "https://github.com/a/b/issues"},
        {"unknown_elements": (("TemplateURL", "<TemplateURL>https://github.com/a/b/x.xml</TemplateURL>"),)},
    ):
        [m] = match_sources("nginx", {}, tpl(**kw), [])
        assert (m.confidence, m.owner, m.repo, m.source_id) == ("medium", "a", "b", None)


def test_confidence_order_and_dedupe() -> None:
    src = replace(make_source(owner="a", repo="b"), id=1)
    flash = tpl(project="https://github.com/c/d", support="https://github.com/a/b")
    matches = match_sources("ghcr.io/a/b:1", {OCI_SOURCE_LABEL: "https://github.com/e/f"}, flash, [src])
    assert [(m.confidence, m.owner) for m in matches] == [("high", "e"), ("high", "a"), ("medium", "c")]


def test_choose_template_prefers_repository_then_name() -> None:
    a = ("a.xml", tpl(name="other", repository="ghcr.io/x/notes:latest"))
    b = ("b.xml", tpl(name="notes", repository="ghcr.io/x/different"))
    assert choose_template([b, a], "ghcr.io/X/notes:1.2", "notes") == "a.xml"
    assert choose_template([b], "ghcr.io/x/unknown:1", "NOTES") == "b.xml"
    assert choose_template([a], "ghcr.io/x/unknown", "zzz") is None
    assert template_matches_image(a[1], "ghcr.io/x/notes:9")


def host(hid: int, domain: str, fhost: str, fport: int) -> ProxyHost:
    return ProxyHost(hid, (domain,), "http", fhost, fport)


def test_proxy_match_rules_in_order() -> None:
    ports = [PortBinding(3000, "tcp", 8080)]
    by_port = host(1, "x.example.com", "192.168.1.10", 8080)
    by_name = host(2, "y.example.com", "notes", 3000)
    by_domain = host(3, "notes.example.com", "10.0.0.1", 1)
    hosts = [by_name, by_port, by_domain]
    assert match_proxy_host(hosts, "notes", "example.com", ports, "192.168.1.10") == by_domain
    assert match_proxy_host(hosts, "notes", None, ports, "192.168.1.10") == by_port
    assert match_proxy_host(hosts, "notes", "example.com", [], "192.168.1.10") == by_domain
    assert match_proxy_host([by_name], "notes", None, ports, "192.168.1.10") == by_name
    assert match_proxy_host([by_port], "notes", None, ports, "192.168.1.99") is None


def test_derive_subdomain() -> None:
    assert derive_subdomain(host(1, "wiki.home.example.com", "h", 1), "example.com") == "wiki.home"
    assert derive_subdomain(host(1, "wiki.other.org", "h", 1), "example.com") == "wiki"
    assert derive_subdomain(host(1, "wiki.other.org", "h", 1), None) == "wiki"
