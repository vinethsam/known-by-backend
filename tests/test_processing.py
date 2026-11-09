from __future__ import annotations

import json

from app.config import Settings
from app.processing import html_to_markdown, process_content, processed_chunks
from app.research.claims import validate_claims
from app.research.extraction import deterministic_evidence
from app.research.identity import assess_identity
from app.retrieval.service import RetrievedPage
from app.schemas import PersonSeed, ProfileField, SourceRecord


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
    assert len(before) > len(after)


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


def test_jsonld_person_graph_is_grounded_before_optional_llm_extraction():
    cfg = settings()
    seed = PersonSeed(full_name="Ada Lovelace", organisation="Analytical Society")
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {"@id": "#employer", "@type": "Organization", "name": "Analytical Society"},
            {"@type": "ProfilePage", "mainEntity": {"@id": "#ada"}},
            {
                "@id": "#ada",
                "@type": "Person",
                "name": "Ada Lovelace",
                "jobTitle": "Research Director",
                "worksFor": {"@id": "#employer"},
            },
        ],
    }
    # CMS templates can publish the same Person node more than once.
    data["@graph"].append(dict(data["@graph"][-1]))
    url = "https://example.org/ada"
    page = RetrievedPage(
        requested_url=url,
        final_url=url,
        status=200,
        html=f'<script type="application/ld+json">{json.dumps(data)}</script>',
    )
    chunks = process_content(page, seed, cfg)
    text = "\n\n".join(chunks)
    source = SourceRecord(
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain="example.org",
        identity=assess_identity(seed, text, cfg.SCORING),
    )
    accepted = []
    claims = []
    for evidence, response in deterministic_evidence(page, seed):
        grounded, reasons = validate_claims(response, seed, source, evidence, "deterministic:structured-v1")
        assert not reasons
        assert evidence in text
        claims.extend(grounded)
        accepted.append(evidence)
    assert source.identity.score >= cfg.SCORING.identity_review_threshold
    assert len(claims) == 3
    assert {claim.field for claim in claims} == {
        ProfileField.full_name,
        ProfileField.organisation,
        ProfileField.job_title,
    }
    assert all(claim.evidence_location.startswith("jsonld[0]") for claim in claims)
    assert all(claim.extraction_model == "deterministic:structured-v1" for claim in claims)
    assert process_content(page, seed, cfg, accepted_evidence=accepted) == []

    description = "Ada Lovelace also held a visiting appointment during her earlier career."
    data["@graph"][-1]["description"] = description
    rich_page = page.model_copy(
        update={"html": f'<script type="application/ld+json">{json.dumps(data)}</script>'}
    )
    remainder = "\n\n".join(process_content(rich_page, seed, cfg, accepted_evidence=accepted))
    assert description in remainder
    assert "Research Director" not in remainder and "worksFor" not in remainder

    data["@graph"][0]["description"] = "Historical appointment from 2018 to 2021."
    dated_page = page.model_copy(
        update={"html": f'<script type="application/ld+json">{json.dumps(data)}</script>'}
    )
    remainder = "\n\n".join(process_content(dated_page, seed, cfg, accepted_evidence=accepted))
    assert "Historical appointment from 2018 to 2021." in remainder and "Research Director" in remainder


def test_structured_ambiguity_and_credential_recognizers_remain_for_the_model():
    data = {
        "@type": "Person",
        "name": "Ada Lovelace",
        "jobTitle": ["Director", "Professor"],
        "worksFor": [{"name": "Analytical Society"}, {"name": "Example College"}],
        "alumniOf": {"@type": "CollegeOrUniversity", "name": "Example University"},
        "hasCredential": [
            {
                "@type": "Credential",
                "name": "PhD in Mathematics",
                "recognizedBy": {"@type": "Organization", "name": "Academic Accreditors"},
                "issuer": {"@type": "CollegeOrUniversity", "name": "Doctoral Institute"},
                "description": "This qualification was completed in 2011.",
                "dateCreated": "2011-06-01",
            },
            {"@type": "Credential", "name": "Honorary Doctor of Science"},
        ],
        "description": "Ada Lovelace's qualifications and career need their dates and institutions interpreted.",
    }
    url = "https://example.org/ada"
    page = RetrievedPage(
        requested_url=url,
        final_url=url,
        status=200,
        html=f'<script type="application/ld+json">{json.dumps(data)}</script>',
    )
    batches = deterministic_evidence(page, {"full_name": "Ada Lovelace"})
    claims = [claim for _, response in batches for claim in response.claims]
    assert not any(claim.field in {ProfileField.organisation, ProfileField.job_title} for claim in claims)
    university = next(claim for claim in claims if claim.field == ProfileField.university_name)
    degree = next(claim for claim in claims if claim.field == ProfileField.degree_type)
    assert university.value == "Example University"
    assert degree.fact_group != university.fact_group
    assert not any("Honorary" in claim.value or "Accreditors" in claim.value for claim in claims)
    remaining = "\n\n".join(
        process_content(
            page, {"full_name": "Ada Lovelace"}, settings(), accepted_evidence=[text for text, _ in batches]
        )
    )
    assert "Professor" in remaining and "Example College" in remaining
    assert "Academic Accreditors" in remaining and "Honorary Doctor" in remaining
    assert (
        "Doctoral Institute" in remaining and "2011-06-01" in remaining and "completed in 2011" in remaining
    )
    assert "qualifications and career" in remaining


def test_microdata_identity_does_not_inherit_a_neighbouring_persons_job():
    html = """
    <div itemscope itemtype="https://schema.org/Person">
      <h1 itemprop="name">Ada Lovelace</h1><span itemprop="jobTitle">Research Director</span>
      <span itemprop="worksFor" itemscope itemtype="https://schema.org/Organization">
        <span itemprop="name">Analytical Society</span></span>
    </div>
    <div itemscope itemtype="https://schema.org/Person">
      <h2 itemprop="name">Another Person</h2><span itemprop="jobTitle">Chief Executive</span>
    </div>"""
    page = RetrievedPage(
        requested_url="https://example.org/ada", final_url="https://example.org/ada", status=200, html=html
    )
    claims = [
        claim
        for _, response in deterministic_evidence(page, {"full_name": "Ada Lovelace"})
        for claim in response.claims
    ]
    assert {claim.value for claim in claims} == {"Ada Lovelace", "Research Director", "Analytical Society"}
    assert all(claim.evidence_location.startswith("microdata") for claim in claims)


def test_each_explicit_credential_issuer_keeps_its_degree_and_subject_association():
    data = {
        "@type": "Person",
        "name": "Ada Lovelace",
        "hasCredential": [
            {
                "@type": "Credential",
                "name": "BSc in Mathematics",
                "issuer": {"@type": "CollegeOrUniversity", "name": "Example University"},
            },
            {
                "@type": "Credential",
                "name": "MSc in Computing",
                "about": {"name": "Computing"},
                "issuedBy": {"@type": "EducationalOrganization", "name": "Analytical Institute"},
            },
            {
                "@type": "Credential",
                "name": "PhD in Mathematics",
                "recognizedBy": {"@type": "CollegeOrUniversity", "name": "Accrediting University"},
            },
        ],
    }
    page = RetrievedPage(
        requested_url="https://example.org/ada",
        final_url="https://example.org/ada",
        status=200,
        html=f'<script type="application/ld+json">{json.dumps(data)}</script>',
    )
    groups = [
        response.claims
        for _, response in deterministic_evidence(page, {"full_name": "Ada Lovelace"})
        if any(claim.field == ProfileField.degree_type for claim in response.claims)
    ]
    assert [{claim.value for claim in group} for group in groups] == [
        {"BSc in Mathematics", "Example University"},
        {"MSc in Computing", "Computing", "Analytical Institute"},
        {"PhD in Mathematics"},
    ]
    assert all(len({claim.fact_group for claim in group}) == 1 for group in groups)
    assert len({group[0].fact_group for group in groups}) == 3


def test_exact_repeated_evidence_blocks_save_chunks_without_dropping_unique_education():
    block = "Ada Lovelace researches analytical computing at Analytical Society. " * 8
    education = "## Education\nAda Lovelace earned a BSc and an MSc in Mathematics at Example University."
    cfg = settings(CHUNK_SIZE=800, CHUNK_OVERLAP=100, MAX_CHUNKS_PER_SOURCE=3)
    stats = {}
    chunks = processed_chunks(
        "\n\n".join([block, block, block, education]),
        {"full_name": "Ada Lovelace"},
        cfg,
        processing_stats=stats,
    )
    assert len(chunks) == 1
    assert stats["duplicate_evidence_blocks"] == 2
    assert "BSc and an MSc" in chunks[0] and "Example University" in chunks[0]


def test_repeated_paragraphs_under_different_education_contexts_are_preserved():
    paragraph = (
        "Ada Lovelace completed an advanced qualification covering the theory and practice of computing."
    )
    text = f"## University A\n\n{paragraph}\n\n## University B\n\n{paragraph}"
    stats = {}
    combined = "\n\n".join(
        processed_chunks(text, {"full_name": "Ada Lovelace"}, settings(), processing_stats=stats)
    )
    assert combined.count(paragraph) == 2
    assert not stats


def test_same_name_structured_records_with_conflicting_context_require_interpretation():
    data = [
        {
            "@type": "Person",
            "@id": "#one",
            "name": "Ada Lovelace",
            "jobTitle": "Research Director",
            "worksFor": {"name": "Analytical Society"},
        },
        {
            "@type": "Person",
            "@id": "#two",
            "name": "Ada Lovelace",
            "jobTitle": "Chief Executive",
            "worksFor": {"name": "Other Laboratories"},
        },
    ]
    page = RetrievedPage(
        requested_url="https://example.org/people",
        final_url="https://example.org/people",
        status=200,
        html=f'<script type="application/ld+json">{json.dumps(data)}</script>',
    )
    batches = deterministic_evidence(page, {"full_name": "Ada Lovelace"})
    assert {claim.field for _, response in batches for claim in response.claims} == {ProfileField.full_name}
    remaining = "\n\n".join(
        process_content(
            page, {"full_name": "Ada Lovelace"}, settings(), accepted_evidence=[text for text, _ in batches]
        )
    )
    assert "Analytical Society" in remaining and "Other Laboratories" in remaining


def test_nonliteral_structured_names_remain_context_without_inventing_name_claims():
    seed = {"full_name": "Ada Lovelace"}
    for name in (["Ada Lovelace", "A. Lovelace"], {"@value": "Ada Lovelace"}):
        data = {
            "@type": "Person",
            "name": name,
            "givenName": "Ada",
            "familyName": "Lovelace",
            "jobTitle": "Research Director",
            "worksFor": {"name": "Analytical Society"},
        }
        page = RetrievedPage(
            requested_url="https://example.org/ada",
            final_url="https://example.org/ada",
            status=200,
            html=f'<h1>Ada Lovelace</h1><script type="application/ld+json">{json.dumps(data)}</script>',
        )
        assert deterministic_evidence(page, seed) == []
        remaining = "\n\n".join(process_content(page, seed, settings()))
        assert "Research Director" in remaining and "Analytical Society" in remaining
        assert "givenName" in remaining and "familyName" in remaining


def test_named_profile_education_section_survives_repeated_early_name_windows():
    text = (
        "# Ada Lovelace\n\n"
        + "\n\n".join(
            f"Ada Lovelace profile event {number}. " + "Unrelated event schedule details. " * 18
            for number in range(20)
        )
        + "\n\n## Education\n\nShe earned a BSc and MSc in Mathematics at Example University."
    )
    cfg = settings(MAX_MARKDOWN_CHARS=1600, CHUNK_SIZE=600, CHUNK_OVERLAP=60, MAX_CHUNKS_PER_SOURCE=3)
    combined = "\n\n".join(processed_chunks(text, {"full_name": "Ada Lovelace"}, cfg))
    assert "Ada Lovelace" in combined and "BSc and MSc in Mathematics at Example University" in combined
    # A separate person's education section is not a shortcut to target evidence.
    unrelated = text.replace("## Education", "# Another Person\n\n## Education")
    combined = "\n\n".join(processed_chunks(unrelated, {"full_name": "Ada Lovelace"}, cfg))
    assert "Example University" not in combined
