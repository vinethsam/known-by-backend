"""Conservative, source-independent page processing."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify

_TECHNICAL_ELEMENTS = ("script", "style", "noscript", "template")
_EXCESS_BLANK_LINES = re.compile(r"\n(?:[ \t]*\n){2,}")
_BOILERPLATE_LINE_RE = re.compile(
    r"^(accept cookies|cookie policy|cookie preferences|cookie settings|privacy policy|"
    r"terms of (use|service)|subscribe|subscribe to (our|the) newsletter|"
    r"newsletter signup|sign up for (our|the) newsletter|share this( article| page)?|"
    r"all rights reserved|skip to content|main navigation|advertisement)$",
    re.IGNORECASE,
)
_UI_LABELS = {
    "home",
    "about",
    "about us",
    "contact",
    "contact us",
    "search",
    "menu",
    "news",
    "events",
    "sign in",
    "log in",
    "login",
    "register",
    "accessibility",
    "sitemap",
    "back to top",
    "skip to main content",
    "accept all",
    "reject all",
    "manage cookies",
    "close",
}
_STANDALONE_LINK_RE = re.compile(r"^(?:[-*+]\s+)?\[([^\]\n]+)\]\([^\s\[\]]+\)$")
_PRESERVED_STRUCTURES = ("h1", "h2", "h3", "h4", "h5", "h6", "table", "dl", "article")


def _identity_terms(seed: object) -> list[str]:
    values = [
        _seed_value(seed, key)
        for key in (
            "full_name",
            "organisation",
            "country",
            "location",
            "university_name",
            "job_title",
            "subject",
            "program_year",
        )
    ]
    attributes = _seed_value(seed, "known_attributes")
    if isinstance(attributes, Mapping):
        values.extend(attributes.values())
    return [" ".join(str(value).casefold().split()) for value in values if value]


def _remove_ui_chrome(soup: BeautifulSoup, seed: object) -> None:
    identity_terms = _identity_terms(seed)
    elements = soup.find_all(
        lambda tag: tag.name in {"nav", "footer"} or tag.get("role") in {"navigation", "menu", "contentinfo"}
    )
    for element in reversed(elements):
        if element.find(_PRESERVED_STRUCTURES):
            continue
        labels = [" ".join(text.casefold().split()).strip(" |·•") for text in element.stripped_strings]
        text = " ".join(labels)
        if any(term in text for term in identity_terms):
            continue
        # Only whole, explicit UI labels qualify. Unknown directory/profile links
        # and any prose survive, even inside misleading navigation/footer markup.
        if labels and all(
            not label or label in _UI_LABELS or _BOILERPLATE_LINE_RE.fullmatch(label) for label in labels
        ):
            element.decompose()


def _resolve_relative_links(soup: BeautifulSoup, base_url: str) -> None:
    for link in soup.find_all("a", href=True):
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        link["href"] = urljoin(base_url, href.strip())


def _normalise_markdown(markdown: str) -> str:
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
    markdown = _EXCESS_BLANK_LINES.sub("\n\n", markdown)
    return markdown.strip()


def html_to_markdown(html: str, base_url: str | None = None, *, seed: object | None = None) -> str:
    """Convert raw HTML to structured Markdown without interpreting content."""

    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for element in reversed(soup.find_all(_TECHNICAL_ELEMENTS)):
        element.decompose()
    if seed is not None:
        _remove_ui_chrome(soup, seed)
    if base_url:
        _resolve_relative_links(soup, base_url)

    markdown = markdownify(str(soup), heading_style="ATX", bullets="-")
    return _normalise_markdown(markdown)


def document_metadata(html: str) -> tuple[str, date | None]:
    """Read explicit page title/publication metadata without inventing freshness."""
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True)[:300] if title_tag else ""
    for meta in soup.find_all("meta", content=True):
        label = str(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").casefold()
        if label not in {"article:published_time", "datepublished", "date_published"}:
            continue
        value = str(meta.get("content", ""))
        try:
            published = date.fromisoformat(value[:10])
        except ValueError:
            continue
        if published <= datetime.now(timezone.utc).date():
            return title, published
    return title, None


def _seed_value(seed: object, key: str) -> Any:
    if isinstance(seed, Mapping):
        return seed.get(key)
    return getattr(seed, key, None)


def _primary_terms(seed: object) -> list[str]:
    full_name = str(_seed_value(seed, "full_name") or "").strip()
    return [full_name] if full_name else []


def _secondary_terms(seed: object) -> list[str]:
    values = [
        _seed_value(seed, "organisation"),
        _seed_value(seed, "university_name"),
        _seed_value(seed, "job_title"),
        _seed_value(seed, "subject"),
    ]
    terms: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        if text:
            terms.append(text)
    full_name = str(_seed_value(seed, "full_name") or "")
    parts = [part for part in re.split(r"\W+", full_name) if len(part) > 2]
    terms.extend(parts)
    return list(dict.fromkeys(terms))


def _line_mentions_seed(line: str, seed: object) -> bool:
    line_lower = line.lower()
    return any(term.lower() in line_lower for term in _primary_terms(seed))


def _compact_boilerplate(text: str, seed: object) -> str:
    lines = [_normalise_markdown(line) for line in text.splitlines()]
    kept: list[str] = []
    seen_counts: dict[str, int] = {}
    identity_terms = _identity_terms(seed)
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if kept and kept[-1]:
                kept.append("")
            continue
        link = _STANDALONE_LINK_RE.fullmatch(stripped)
        label = link.group(1) if link else stripped
        protected_link = link is not None and any(term in label.casefold() for term in identity_terms)
        if (
            _BOILERPLATE_LINE_RE.fullmatch(label)
            and not _line_mentions_seed(label, seed)
            and not protected_link
        ):
            continue
        key = stripped.lower()
        seen_counts[key] = seen_counts.get(key, 0) + 1
        if seen_counts[key] > 2:
            continue
        kept.append(stripped)
    return _normalise_markdown("\n".join(kept))


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    intervals.sort()
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _interval_length(intervals: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _add_priority_intervals(
    selected: list[tuple[int, int]],
    candidates: list[tuple[int, int]],
    max_total: int,
) -> list[tuple[int, int]]:
    for start, end in sorted(candidates):
        if start >= end:
            continue
        remaining = max_total - _interval_length(selected)
        if remaining <= 0:
            break
        candidate = (start, min(end, start + remaining))
        selected.append(candidate)
        selected = _merge_intervals(selected)
    return selected


def _limit_intervals(intervals: list[tuple[int, int]], max_total: int) -> list[tuple[int, int]]:
    selected: list[tuple[int, int]] = []
    used = 0
    for start, end in _merge_intervals(intervals):
        remaining = max_total - used
        if remaining <= 0:
            break
        if end - start > remaining:
            end = start + remaining
        selected.append((start, end))
        used += end - start
    return selected


def _term_windows(text: str, terms: list[str], chunk_size: int) -> list[tuple[int, int]]:
    lower_text = text.lower()
    intervals: list[tuple[int, int]] = []
    for term in terms:
        lower_term = term.lower()
        start = 0
        while lower_term:
            hit = lower_text.find(lower_term, start)
            if hit == -1:
                break
            window_start = max(0, hit - chunk_size // 2)
            window_end = min(len(text), window_start + chunk_size)
            intervals.append((window_start, window_end))
            start = hit + max(1, len(lower_term))
    return intervals


def _select_relevant_text(text: str, seed: object, settings: object) -> str:
    max_chars = int(getattr(settings, "MAX_MARKDOWN_CHARS", 24_000))
    chunk_size = int(getattr(settings, "CHUNK_SIZE", 6000))
    overlap = int(getattr(settings, "CHUNK_OVERLAP", 400))
    max_chunks = int(getattr(settings, "MAX_CHUNKS_PER_SOURCE", 3))
    chunk_budget = max(1, chunk_size * max_chunks - overlap * max(0, max_chunks - 1))
    max_selected = min(max_chars, chunk_budget)
    if len(text) <= max_selected:
        return text

    window_size = min(chunk_size, max_selected)
    head_chars = min(window_size, max_selected // 3)
    primary_windows = _term_windows(text, _primary_terms(seed), window_size)
    secondary_windows = _term_windows(text, _secondary_terms(seed), window_size)

    intervals: list[tuple[int, int]] = []
    intervals = _add_priority_intervals(intervals, primary_windows, max_selected)
    intervals = _add_priority_intervals(intervals, [(0, head_chars)], max_selected)
    intervals = _add_priority_intervals(intervals, secondary_windows, max_selected)
    intervals = _limit_intervals(intervals, max_selected)
    return "\n\n".join(text[start:end].strip() for start, end in intervals if text[start:end].strip())


def _chunk_text(text: str, settings: object) -> list[str]:
    chunk_size = int(getattr(settings, "CHUNK_SIZE", 6000))
    overlap = int(getattr(settings, "CHUNK_OVERLAP", 400))
    max_chunks = int(getattr(settings, "MAX_CHUNKS_PER_SOURCE", 3))
    if not text:
        return []

    chunks: list[str] = []
    seen_hashes: set[str] = set()
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(len(text), start + chunk_size)
        chunk = text[start:end].strip()
        digest = sha256(chunk.encode("utf-8")).hexdigest()
        if chunk and digest not in seen_hashes:
            chunks.append(chunk)
            seen_hashes.add(digest)
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def processed_chunks(text: str, seed: object, settings: object) -> list[str]:
    """Return compact, bounded chunks with late person mentions preserved."""

    compacted = _compact_boilerplate(_normalise_markdown(text or ""), seed)
    selected = _select_relevant_text(compacted, seed, settings)
    return _chunk_text(selected, settings)


def process_content(page: object, seed: object, settings: object) -> list[str]:
    """Convert a retrieved page to downstream chunks for LLM extraction."""

    captured_json = getattr(page, "captured_json", None)
    content_type = str(getattr(page, "content_type", "") or "").lower()
    if captured_json is not None:
        text = json.dumps(captured_json, ensure_ascii=False, sort_keys=True)
    elif "json" in content_type:
        text = str(getattr(page, "html", "") or "")
    else:
        text = html_to_markdown(
            str(getattr(page, "html", "") or ""),
            base_url=getattr(page, "final_url", None),
            seed=seed,
        )
    return processed_chunks(text, seed, settings)


__all__ = ["html_to_markdown", "process_content", "processed_chunks"]
