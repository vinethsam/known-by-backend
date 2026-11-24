"""Person-name and profile-link proposals from observed names and URLs."""

from __future__ import annotations

import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from app.research.extraction import _structured_nodes
from app.research.identity import claim_is_eligible_for_source
from app.research.normalisation import name_key
from app.retrieval.contracts import RetrievedPage
from app.retrieval.urls import (
    _reject_blocked_hostname,
    _validate_public_ip,
    canonicalise_url,
    source_policy_allows,
    source_record_allowed,
)
from app.schemas import ExtractedClaim, ExtractionResponse, PersonSeed, ProfileField, SourceRecord

from .context import EvidenceSpan, evidence_spans

_LINK = re.compile(r"(?<![!\\])\[([^\[\]\r\n]+)\]\((https?://[^\s()<>]+)\)", re.I)
_PROFILE_PATH = {
    "bio",
    "bios",
    "directory",
    "experts",
    "faculty",
    "leadership",
    "members",
    "people",
    "person",
    "profile",
    "profiles",
    "staff",
    "team",
}
_PROFILE_LABEL = re.compile(r"(?:['’]s)?\s+(?:bio(?:graphy)?|profile)\s*$", re.I)
_STRUCTURED_HINT = re.compile(r"application/ld\+json|itemtype\s*=", re.I)


def extract_full_name(
    spans: list[EvidenceSpan], seed: PersonSeed, source: SourceRecord
) -> list[ExtractedClaim]:
    """Retain the first admissible literal observation per distinct form of the name."""
    claims = {}
    for span in spans:
        name = span.subject_name
        if span.explicit and claim_is_eligible_for_source(
            seed, source, ProfileField.full_name, name, span.text
        ):
            claims.setdefault(name_key(name), span)
    return [
        span.claim(ProfileField.full_name, span.subject_name, source_claim_type="literal_person_name")
        for span in claims.values()
    ]


def _profile_key(url: str, target: str, *, named_metadata: bool = False, inline: bool = False) -> str | None:
    if (
        not 1 <= len(url) <= 500
        or not url.lower().startswith(("https://", "http://"))
        or not source_policy_allows(url)
    ):
        return None
    key = canonicalise_url(url)
    parts = urlsplit(key)
    host = parts.hostname or ""
    try:
        _reject_blocked_hostname(host)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                return None
        else:
            _validate_public_ip(address)
    except ValueError:
        return None
    path = [unquote(segment) for segment in parts.path.strip("/").split("/") if segment]
    if not path or (inline and not _PROFILE_PATH.intersection(segment.casefold() for segment in path[:-1])):
        return None
    slug = re.sub(r"\.(?:html?|aspx?)$", "", path[-1], flags=re.I)
    if name_key(slug) == target or (
        named_metadata and len(path) >= 2 and path[-2].casefold() in _PROFILE_PATH and slug.isdecimal()
    ):
        return key
    return None


def extract_profile_links(chunk: str, spans: list[EvidenceSpan], seed: PersonSeed) -> list[ExtractedClaim]:
    """Match only a target-labelled link to a target-specific profile path."""
    claims = {}
    target_name = name_key(seed.full_name)
    for span in spans:
        target = name_key(span.subject_name)
        for label, url in _LINK.findall(span.text):
            name = _PROFILE_LABEL.sub("", label.strip("*_ "))
            if name_key(name) not in {target, target_name}:
                continue
            key = _profile_key(url, target, inline=True)
            if key and key not in claims:
                claims[key] = span.claim(
                    ProfileField.profile_link, url, source_claim_type="named_profile_link"
                )
    for link in _LINK.finditer(chunk):
        label, url = link.groups()
        name = _PROFILE_LABEL.sub("", label.strip("*_ "))
        if name_key(name) != target_name or len(link.group()) > 800 or not 2 <= len(name) <= 200:
            continue
        key = _profile_key(url, target_name, inline=True)
        if key and key not in claims:
            claims[key] = ExtractedClaim(
                field=ProfileField.profile_link,
                value=url,
                evidence=link.group(),
                subject_name=name,
                evidence_location=f"chunk:{link.start()}-{link.end()}",
                source_claim_type="named_profile_link",
            )
    return list(claims.values())


class _PageURLMetadata(HTMLParser):
    """Read canonical and Open Graph URL tags from the bounded HTML head."""

    def __init__(self):
        super().__init__()
        self.urls: list[tuple[str, str]] = []
        self.in_head = True

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.in_head = False
        if not self.in_head or tag not in {"link", "meta"}:
            return
        values = {name: value or "" for name, value in attrs}
        if tag == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.urls.append(("canonical", values.get("href", "")))
        if tag == "meta" and values.get("property", "").casefold() == "og:url":
            self.urls.append(("og:url", values.get("content", "")))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def profile_metadata_evidence(
    page: RetrievedPage,
    seed: PersonSeed,
    source: SourceRecord,
    *,
    prepared_text: str = "",
) -> list[tuple[str, ExtractionResponse]]:
    """Produce explicit local metadata projections without fetching their links."""
    if (
        source.identity.rejected
        or not source_record_allowed(source)
        or not source_policy_allows(page.requested_url, page.final_url)
        or canonicalise_url(page.final_url) != canonicalise_url(source.final_url)
    ):
        return []
    target = name_key(seed.full_name)
    # A later card in a directory cannot establish ownership of the page itself.
    first_heading = re.search(r"^#{1,2}[ \t]+\S.*$", prepared_text[:16_384], re.M)
    heading = evidence_spans(first_heading.group(), seed, source) if first_heading else []
    title_name = re.split(r"\s+[|–—-]\s+", source.title, maxsplit=1)[0]
    ownership = (heading[0].subject_name, heading[0].text) if heading else None
    if not first_heading and name_key(title_name) == target:
        ownership = (title_name, source.title)

    choices = {}
    base_host = urlsplit(canonicalise_url(page.final_url)).hostname

    def add(kind: str, observed: str, name: str, quote: str, *, metadata: bool = False):
        if not observed or len(observed) > 500:
            return
        try:
            url = urljoin(page.final_url, observed)
        except ValueError:
            return
        key = _profile_key(url, name_key(name), named_metadata=metadata)
        if not key or urlsplit(key).hostname != base_host:
            return
        # The existing validator expects a literal absolute URL. Preserve the
        # original metadata as well when resolving an observed relative value.
        evidence = f"{quote}\n{kind}: {url}"
        if url != observed:
            evidence += f"\nOriginal URL reference: {observed}"
        if len(evidence) <= 800:
            choices.setdefault(key, (kind, url, name, evidence))

    if ownership:
        name, quote = ownership
        metadata = _PageURLMetadata()
        try:
            metadata.feed(page.html[:65_536])
        except (ValueError, AssertionError):
            metadata.urls.clear()
        for kind, url in metadata.urls:
            add(kind, url, name, quote, metadata=True)
        if len(choices) > 1:
            return []
        if not choices:
            add("retrieved URL", page.final_url, name, quote)

    # Structured pages without an owned Markdown heading can still explicitly
    # bind a Person to its own `url`. Reuse the existing scoped node reader.
    if not choices and (
        page.captured_json is not None
        or "json" in (page.content_type or "").casefold()
        or _STRUCTURED_HINT.search(page.html)
    ):
        for location, node, _ in _structured_nodes(page):
            name, url = node.get("name"), node.get("url")
            if isinstance(name, str) and name_key(name) == target and isinstance(url, str):
                quote = json.dumps({"@type": "Person", "name": name, "url": url}, ensure_ascii=False)
                add(f"{location}/url", url, name, quote, metadata=True)

    if len(choices) != 1:
        return []
    kind, value, name, evidence = next(iter(choices.values()))
    return [
        (
            evidence,
            ExtractionResponse(
                claims=[
                    ExtractedClaim(
                        field=ProfileField.profile_link,
                        value=value,
                        evidence=evidence,
                        subject_name=name,
                        evidence_location=f"page-metadata:{kind}"[:200],
                        source_claim_type="profile_url_metadata",
                    )
                ]
            ),
        )
    ]
