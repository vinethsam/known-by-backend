from __future__ import annotations

from app.config import Settings
from app.processing import html_to_markdown, process_content, processed_chunks
from app.retrieval.service import RetrievedPage


def settings(**overrides):
    values = {
        "APP_ENV": "test",
        "PLAYWRIGHT_ENABLED": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_html_to_markdown_preserves_structure_and_resolves_links():
    html = """
    <h1>Profile</h1>
    <p>See <a href="/people/ada">Ada</a></p>
    <ul><li>Mathematician</li></ul>
    <table><tr><th>Field</th></tr><tr><td>Computing</td></tr></table>
    <script>ignored()</script>
    """

    markdown = html_to_markdown(html, base_url="https://example.com/root")

    assert "# Profile" in markdown
    assert "[Ada](https://example.com/people/ada)" in markdown
    assert "- Mathematician" in markdown
    assert "Computing" in markdown
    assert "ignored" not in markdown


def test_processed_chunks_keeps_late_person_relevance():
    cfg = settings(
        MAX_MARKDOWN_CHARS=1200,
        CHUNK_SIZE=400,
        CHUNK_OVERLAP=40,
        MAX_CHUNKS_PER_SOURCE=3,
    )
    text = (
        "Intro without useful identity.\n"
        + ("filler " * 800)
        + "\nAda Lovelace founded an analytical computing program."
    )

    chunks = processed_chunks(text, {"full_name": "Ada Lovelace"}, cfg)

    assert len(chunks) <= 3
    assert any("Ada Lovelace founded" in chunk for chunk in chunks)


def test_processed_chunks_removes_repeated_boilerplate():
    cfg = settings(
        MAX_MARKDOWN_CHARS=2000,
        CHUNK_SIZE=1000,
        CHUNK_OVERLAP=100,
        MAX_CHUNKS_PER_SOURCE=2,
    )
    text = "\n".join(
        ["Cookie policy"] * 8 + ["Ada Lovelace profile", "Ada Lovelace profile", "Ada Lovelace profile"]
    )

    chunks = processed_chunks(text, {"full_name": "Ada Lovelace"}, cfg)

    combined = "\n".join(chunks)
    assert "Cookie policy" not in combined
    assert combined.count("Ada Lovelace profile") == 2


def test_processed_chunks_prioritizes_late_full_name_over_early_clues():
    cfg = settings(
        MAX_MARKDOWN_CHARS=1200,
        CHUNK_SIZE=350,
        CHUNK_OVERLAP=50,
        MAX_CHUNKS_PER_SOURCE=2,
    )
    text = (
        ("Example Labs general directory item.\n" * 120)
        + ("unrelated filler " * 200)
        + "\nAda Lovelace leads the analytical engine research team."
    )

    chunks = processed_chunks(
        text,
        {"full_name": "Ada Lovelace", "organisation": "Example Labs"},
        cfg,
    )

    assert len(chunks) <= 2
    assert any("Ada Lovelace leads" in chunk for chunk in chunks)


def test_processed_chunks_keeps_seed_evidence_with_boilerplate_words():
    cfg = settings(
        MAX_MARKDOWN_CHARS=1200,
        CHUNK_SIZE=400,
        CHUNK_OVERLAP=40,
        MAX_CHUNKS_PER_SOURCE=2,
    )
    text = "\n".join(
        [
            "Cookie policy",
            "Jane Cookie is newsletter editor for the science desk.",
            "Subscribe to our newsletter",
        ]
    )

    chunks = processed_chunks(text, {"full_name": "Jane Cookie"}, cfg)
    combined = "\n".join(chunks)

    assert "Jane Cookie is newsletter editor" in combined
    assert "Cookie policy" not in combined
    assert "Subscribe to our newsletter" not in combined


def test_process_content_uses_json_payloads():
    cfg = settings()
    page = RetrievedPage(
        requested_url="https://example.com/data",
        final_url="https://example.com/data",
        status=200,
        content_type="application/json",
        captured_json={"records": [{"name": "Ada Lovelace"}]},
    )

    chunks = process_content(page, {"full_name": "Ada Lovelace"}, cfg)

    assert "Ada Lovelace" in chunks[0]


def test_ui_chrome_removal_saves_context_and_keeps_late_biography_and_education():
    labels = ["Home", "About us", "Contact us", "Search", "News", "Events", "Accessibility", "Sitemap"]
    menu = (
        "<nav>"
        + "".join(f'<a href="/menu/{index}">{label}</a>' for index, label in enumerate(labels))
        + "</nav>"
    )
    biography = """
    <main><h1>Ada Lovelace</h1>
      <h2>Biography</h2><p>Ada Lovelace is a researcher at Analytical Society.</p>
      <h2>Education</h2><table><tr><th>University</th><th>Subject</th></tr>
        <tr><td>Example University</td><td>Mathematics</td></tr></table>
    </main>
    """
    html = menu + '<div role="navigation">' + menu + "</div>" + biography + "<footer>" + menu + "</footer>"
    url = "https://example.org/ada"
    seed = {"full_name": "Ada Lovelace"}
    cfg = settings()
    before = "\n".join(processed_chunks(html_to_markdown(html, url), seed, cfg))
    after = "\n".join(
        process_content(RetrievedPage(requested_url=url, final_url=url, status=200, html=html), seed, cfg)
    )
    evidence = "\n".join(processed_chunks(html_to_markdown(biography, url), seed, cfg))
    assert after == evidence
    assert "# Ada Lovelace" in after
    assert "## Education" in after
    assert "Example University" in after
    assert "Ada Lovelace is a researcher at Analytical Society." in after
    assert len(before) - len(after) > 400


def test_ui_markup_keeps_person_clues_tables_headings_and_unfamiliar_links():
    html = """
      <nav><a href="/about">About us</a><a href="/ada">Ada Lovelace</a></nav>
      <footer><h2>Education</h2><table><tr><td>Mathematics</td></tr></table></footer>
      <nav><a href="/directory">Faculty directory</a><a href="/professor">Analytical Society</a></nav>
      <div role="navigation"><a href="/news">News</a><a href="/home">Home</a></div>
    """
    markdown = html_to_markdown(
        html, "https://example.org/", seed={"full_name": "Ada Lovelace", "organisation": "Home"}
    )
    assert "Ada Lovelace" in markdown
    assert "## Education" in markdown
    assert "Mathematics" in markdown
    assert "Faculty directory" in markdown
    assert "Analytical Society" in markdown
    assert "[Home](https://example.org/home)" in markdown


def test_standalone_boilerplate_links_removed_without_removing_adjacent_evidence():
    text = "\n".join(
        [
            "[Cookie policy](https://example.org/cookies)",
            "- [Subscribe](https://example.org/newsletter)",
            "[Privacy policy](https://example.org/privacy) [Ada Lovelace](https://example.org/ada)",
            "[Privacy policy](https://example.org/privacy)[Mathematics](https://example.org/subject)",
            "Ada Lovelace wrote the privacy policy for Analytical Society.",
        ]
    )
    combined = "\n".join(processed_chunks(text, {"full_name": "Ada Lovelace"}, settings()))
    assert "Cookie policy" not in combined
    assert "[Subscribe]" not in combined
    assert "[Ada Lovelace]" in combined
    assert "[Mathematics]" in combined
    assert "Ada Lovelace wrote the privacy policy for Analytical Society." in combined
    protected = processed_chunks(
        "[Privacy policy](https://example.org/privacy)",
        {"full_name": "Ada Lovelace", "known_attributes": {"subject": "Privacy policy"}},
        settings(),
    )
    assert protected == ["[Privacy policy](https://example.org/privacy)"]
