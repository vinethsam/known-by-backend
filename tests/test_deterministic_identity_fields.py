from app.processing import prepare_content
from app.research.claims import validate_claims
from app.research.deterministic import VERSION, extract_chunk, profile_metadata_evidence
from app.retrieval.contracts import RetrievedPage
from app.schemas import IdentityMatch, PersonSeed, SourceRecord


def _source(url="https://northlake.example.edu/people/lina-chen", **identity):
    return SourceRecord(
        source_id="fixture",
        person_id="target",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain="example.edu",
        source_type="university",
        identity=IdentityMatch(score=0.9, **identity),
    )


def _ground(chunk, seed, source):
    response = extract_chunk(chunk, seed, source)
    assert all(claim.evidence in chunk and claim.value in claim.evidence for claim in response.claims)
    claims, reasons = validate_claims(response, seed, source, chunk, VERSION)
    assert reasons == []
    return claims


def test_full_name_uses_visible_heading_and_observed_alias():
    seed = PersonSeed(full_name="Lina Chen")
    chunk = (
        "# Dr. Lina Chen\n\nLina Chen (also known as L. Chen) is a chemist.\n\n"
        "L. Chen earned an MSc in Chemistry from Northlake University."
    )
    claims = _ground(chunk, seed, _source())
    names = [claim for claim in claims if claim.field == "full_name"]
    assert [claim.raw_value for claim in names] == ["Dr. Lina Chen"]
    subject = next(claim for claim in claims if claim.field == "subject")
    assert subject.raw_value == "Chemistry" and subject.subject_name == "Lina Chen"
    assert "known as L. Chen" in subject.evidence_text


def test_middle_name_variant_requires_source_context():
    seed = PersonSeed(full_name="Lina Chen", university_name="Northlake University")
    chunk = "Lina Marie Chen earned an MSc in Chemistry from Northlake University."
    assert not extract_chunk(chunk, seed, _source()).claims
    claims = _ground(chunk, seed, _source(signals={"matched_seed_anchors": ["university_name"]}))
    assert {claim.raw_value for claim in claims if claim.field == "full_name"} == {"Lina Marie Chen"}


def test_namesake_and_pronoun_cannot_inherit_the_target_scope():
    seed = PersonSeed(full_name="Lina Chen")
    chunk = (
        "# Lina Chen\n\nLina Chen is the wife of the Chief Executive Officer of Moss Research. "
        "He earned a PhD in Geology from Moss University.\n\n"
        "## Lina Chen-Watt\n\n- BSc in Physics from Watt University."
    )
    claims = _ground(chunk, seed, _source())
    assert {claim.field for claim in claims} == {"full_name"}
    assert not extract_chunk(chunk, seed, _source(rejected=True)).claims


def test_textual_date_prefix_is_retained_with_the_employer_title_pair():
    seed = PersonSeed(full_name="Lina Chen")
    chunk = "As of September 15, 2024, Lina Chen currently works as Research Director at Moss Research."
    roles = [
        claim for claim in _ground(chunk, seed, _source()) if claim.field in {"organisation", "job_title"}
    ]
    assert {claim.raw_value for claim in roles} == {"Research Director", "Moss Research"}
    assert len({claim.fact_group for claim in roles}) == 1
    assert all(claim.as_of_date.isoformat() == "2024-09-15" for claim in roles)


def test_inline_profile_link_requires_owned_label_and_profile_path():
    seed = PersonSeed(full_name="Lina Chen")
    chunk = (
        "Read [Lina Chen's biography](https://other.example.org/people/lina-chen). "
        "[Lina Chen](https://pay.example.org/checkout/lina-chen) "
        "[Lina Chen](https://www.linkedin.com/in/lina-chen) "
        "[Lina Chen](http://127.0.0.1/people/lina-chen) "
        "[Lina Chen-Watt](https://other.example.org/people/lina-chen)."
    )
    assert [
        claim.raw_value for claim in _ground(chunk, seed, _source()) if claim.field == "profile_link"
    ] == ["https://other.example.org/people/lina-chen"]


def test_canonical_profile_link_retains_original_reference_and_rejects_directory():
    seed = PersonSeed(full_name="Lina Chen")
    source = _source()
    page = RetrievedPage(
        requested_url=source.requested_url,
        final_url=source.final_url,
        status=200,
        content_type="text/html",
        html='<head><link rel="canonical" href="/faculty/lina-chen"></head><h1>Lina Chen</h1>',
    )
    content = prepare_content(page, seed)
    [(evidence, response)] = profile_metadata_evidence(page, seed, source, prepared_text=content.text)
    claims, reasons = validate_claims(response, seed, source, evidence, VERSION)
    assert reasons == [] and claims[0].raw_value == "https://northlake.example.edu/faculty/lina-chen"
    assert "Original URL reference: /faculty/lina-chen" in claims[0].evidence_text
    assert not profile_metadata_evidence(page, seed, source, prepared_text="# Directory\n\n## Lina Chen")
    assert not profile_metadata_evidence(
        page, seed, _source("https://example.org/lina-chen"), prepared_text=content.text
    )


def test_structured_person_url_can_bind_an_opaque_profile_id():
    seed = PersonSeed(full_name="Lina Chen")
    source = _source("https://northlake.example.edu/directory")
    page = RetrievedPage(
        requested_url=source.requested_url,
        final_url=source.final_url,
        status=200,
        content_type="application/json",
        captured_json={
            "@type": "Person",
            "name": "Lina Chen",
            "url": "https://northlake.example.edu/people/314",
        },
    )
    [(evidence, response)] = profile_metadata_evidence(page, seed, source)
    [claim], reasons = validate_claims(response, seed, source, evidence, VERSION)
    assert reasons == [] and claim.field == "profile_link"
    assert claim.raw_value == "https://northlake.example.edu/people/314"
