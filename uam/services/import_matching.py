"""Pure helpers that match a running container to a GitHub source, a repo template and an NPM host."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Literal

from uam.domain import PortBinding, ProxyHost, Source, SourceMatch, UnraidTemplate

OCI_SOURCE_LABEL = "org.opencontainers.image.source"

_GITHUB_RE = re.compile(
    r"https?://(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
)
_RANK = {"high": 0, "medium": 1, "low": 2}
_TEMPLATE_URL_TAGS = ("Project", "Support", "TemplateURL")

Confidence = Literal["high", "medium", "low"]


def parse_github_url(text: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from the first github.com URL in `text`, if any."""
    m = _GITHUB_RE.search(text)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    if not owner or not repo:
        return None
    return owner, repo


def split_image(ref: str) -> tuple[str, str]:
    """Split an image reference into `(repository, tag)`; the tag defaults to `latest`."""
    ref = ref.split("@", 1)[0]
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :] or "latest"
    return ref, "latest"


def normalize_image(ref: str) -> str:
    """Lower-cased repository part of an image reference, without tag, `docker.io/` or `library/`."""
    repo = split_image(ref)[0].lower()
    repo = repo.removeprefix("docker.io/").removeprefix("index.docker.io/")
    return repo.removeprefix("library/")


def ghcr_owner_repo(ref: str) -> tuple[str, str] | None:
    """`(owner, repo)` of a `ghcr.io/<owner>/<repo>[:tag]` image, else None."""
    parts = split_image(ref)[0].split("/")
    if len(parts) >= 3 and parts[0].lower() == "ghcr.io" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def find_source(sources: Iterable[Source], owner: str, repo: str) -> Source | None:
    """The existing source for `owner/repo`, compared case-insensitively."""
    key = (owner.lower(), repo.lower())
    return next((s for s in sources if (s.owner.lower(), s.repo.lower()) == key), None)


def _match(
    sources: Sequence[Source], owner: str, repo: str, confidence: Confidence, reason: str
) -> SourceMatch:
    existing = find_source(sources, owner, repo)
    return SourceMatch(
        source_id=existing.id if existing else None,
        owner=existing.owner if existing else owner,
        repo=existing.repo if existing else repo,
        template_path=None,
        confidence=confidence,
        reason=reason,
    )


def template_github_repo(template: UnraidTemplate) -> tuple[str, str, str] | None:
    """`(owner, repo, where)` for the first github.com URL in Project, Support or TemplateURL."""
    candidates: list[tuple[str, str]] = [("Project", template.project), ("Support", template.support)]
    candidates += [(tag, raw) for tag, raw in template.unknown_elements if tag in _TEMPLATE_URL_TAGS]
    for where, text in candidates:
        found = parse_github_url(text)
        if found:
            return found[0], found[1], where
    return None


def dedupe_matches(matches: Iterable[SourceMatch]) -> list[SourceMatch]:
    """Sort by confidence (stable) and keep the best match per repository."""
    ordered = sorted(matches, key=lambda m: _RANK[m.confidence])
    seen: set[tuple[str, str]] = set()
    result: list[SourceMatch] = []
    for m in ordered:
        key = (m.owner.lower(), m.repo.lower())
        if key not in seen:
            seen.add(key)
            result.append(m)
    return result


def match_sources(
    image: str,
    image_labels: dict[str, str],
    flash_template: UnraidTemplate | None,
    sources: Sequence[Source],
) -> list[SourceMatch]:
    """High and medium matches, best first, one per repository (case-insensitive).

    Template paths are left empty; the caller fills them in via discovery.
    """
    found: list[SourceMatch] = []
    label = parse_github_url(image_labels.get(OCI_SOURCE_LABEL, ""))
    if label:
        reason = f"image label {OCI_SOURCE_LABEL} points to {label[0]}/{label[1]}"
        found.append(_match(sources, label[0], label[1], "high", reason))
    ghcr = ghcr_owner_repo(image)
    if ghcr:
        if find_source(sources, *ghcr):
            found.append(_match(sources, *ghcr, "high", f"image {image} matches an existing source"))
        else:
            found.append(_match(sources, *ghcr, "medium", f"image {image} is on ghcr.io; add a new source"))
    if flash_template is not None:
        url = template_github_repo(flash_template)
        if url:
            reason = f"flash template {url[2]} is a GitHub URL"
            found.append(_match(sources, url[0], url[1], "medium", reason))
    return dedupe_matches(found)


def choose_template(templates: Sequence[tuple[str, UnraidTemplate]], image: str, name: str) -> str | None:
    """Path of the template whose repository (ignoring the tag) equals `image`, else the one named `name`."""
    wanted = normalize_image(image)
    for path, tpl in templates:
        if normalize_image(tpl.repository) == wanted:
            return path
    for path, tpl in templates:
        if tpl.name.lower() == name.lower():
            return path
    return None


def template_matches_image(template: UnraidTemplate, image: str) -> bool:
    """True when the template's `Repository` (without tag) equals the image."""
    return normalize_image(template.repository) == normalize_image(image)


def match_proxy_host(
    hosts: Sequence[ProxyHost],
    container_name: str,
    base_domain: str | None,
    published_ports: Sequence[PortBinding],
    host_ip: str,
) -> ProxyHost | None:
    """First NPM host matching by domain, then by host IP plus published port, then by container name."""
    if base_domain:
        wanted = f"{container_name}.{base_domain}".lower()
        for h in hosts:
            if any(d.lower() == wanted for d in h.domain_names):
                return h
    host_ports = {b.host_port for b in published_ports}
    for h in hosts:
        if h.forward_host == host_ip and h.forward_port in host_ports:
            return h
    for h in hosts:
        if h.forward_host == container_name:
            return h
    return None


def derive_subdomain(host: ProxyHost, base_domain: str | None) -> str | None:
    """Subdomain part of the host's first domain; the first label when it is outside `base_domain`."""
    if not host.domain_names:
        return None
    domain = host.domain_names[0]
    if base_domain and domain.lower().endswith("." + base_domain.lower()):
        return domain[: -(len(base_domain) + 1)]
    return domain.split(".", 1)[0]
