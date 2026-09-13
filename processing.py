"""Conservative, source-independent HTML-to-Markdown processing."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify


_TECHNICAL_ELEMENTS = ("script", "style", "noscript", "template")
_EXCESS_BLANK_LINES = re.compile(r"\n(?:[ \t]*\n){2,}")


def _resolve_relative_links(soup: BeautifulSoup, base_url: str) -> None:
    for link in soup.find_all("a", href=True):
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            continue

        resolved_href = urljoin(base_url, href.strip())
        if resolved_href != href:
            link["href"] = resolved_href


def _normalise_markdown(markdown: str) -> str:
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
    markdown = _EXCESS_BLANK_LINES.sub("\n\n", markdown)
    return markdown.strip()


def html_to_markdown(html: str, base_url: str | None = None) -> str:
    """Convert raw HTML to structured Markdown without interpreting content."""

    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for element in reversed(soup.find_all(_TECHNICAL_ELEMENTS)):
        element.decompose()

    if base_url:
        _resolve_relative_links(soup, base_url)

    markdown = markdownify(
        str(soup),
        heading_style="ATX",
        bullets="-",
    )
    return _normalise_markdown(markdown)
