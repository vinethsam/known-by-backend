"""Offline rich-seed planning, privacy, identity, and provider-work regressions."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.config import ScoringPolicy
from app.processing import _secondary_terms
from app.providers.source_advisor import QueryPlan, SourceAdvisor, SourceDecisionSet
from app.research.discovery import build_context_queries, build_fallback_queries, build_query, query_key
from app.research.extraction import extract_chunks
from app.research.identity import assess_identity, research_seed_payload
from app.schemas import ExtractionResponse, PersonSeed, ProfileField, SourceCandidate, SourceType
from tests.test_pipeline_api import configured, fake_services, migrated_store


@pytest.mark.parametrize(
    ("attributes", "expected", "absent"),
    [
        ({}, '"Jane Doe"', "education"),
        ({"organisation": "Example Foundation"}, "Example Foundation", "education"),
        ({"university_name": "Example University"}, "Example University", "career"),
        ({"job_title": "Programme Director"}, "Programme Director", "education"),
        (
            {"university_name": "Example University", "subject": "Economics"},
            "Example University Economics",
            "career",
        ),
        (
            {"historical_employer": "Former Foundation", "historical_job_title": "Research Fellow"},
            "Former Foundation Research Fellow career",
            "current",
        ),
    ],
)
def test_first_query_uses_relevant_available_relationship(attributes, expected, absent):
    query = build_query(PersonSeed(full_name="Jane Doe", **attributes))
    assert query.startswith('"Jane Doe"')
    assert expected in query
    assert absent not in query


def test_objective_queries_separate_education_and_employment_and_follow_missing_fields():
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="Example Foundation",
        job_title="Programme Director",
        university_name="Example University",
        degree_type="BSc",
        subject="Economics",
        historical_employer="Former Foundation",
        historical_job_title="Research Fellow",
        industry="Public Policy",
        country="Ghana",
    )
    queries = build_context_queries(seed)
    assert queries[0] == '"Jane Doe" Example Foundation Programme Director'
    assert '"Jane Doe" Example University BSc' in queries
    assert '"Jane Doe" Former Foundation Research Fellow career' in queries
    assert all(not ("Example Foundation" in query and "Example University" in query) for query in queries)
    assert len({query_key(query) for query in queries}) == len(queries)
    assert len(queries) <= 4

    assert build_context_queries(seed, ["subject"]) == ['"Jane Doe" Example University Economics']
    assert build_context_queries(seed, []) == []
    assert build_context_queries(
        seed,
        ["subject"],
        evidence_context={"university_name": ["Newly Verified University"]},
    ) == ['"Jane Doe" Newly Verified University Economics']


def test_alias_duplicates_private_and_unknown_values_never_expand_searches_or_processing():
    seed = PersonSeed(
        full_name="Jane Doe",
        university_name="Example University",
        known_attributes={
            "university": "Example University",
            "previous employer": "Former Foundation",
            "LIST_NAME": "Private Prospect Batch",
            "record_id": "private-record-8754",
            "email": "jane.private@example.com",
            "address": "123 Confidential Street",
            "arbitrary_notes": "Private Family Information",
        },
    )
    material = json.dumps(
        {
            "queries": [build_query(seed), *build_fallback_queries(seed)],
            "payload": research_seed_payload(seed),
            "processing_terms": _secondary_terms(seed),
        }
    )
    assert "Example University" in material
    assert "Former Foundation" in material
    for value in list(seed.known_attributes.values())[2:]:
        assert value not in material
    queries = [build_query(seed), *build_fallback_queries(seed)]
    assert len({query_key(query) for query in queries}) == len(queries)
    assert seed.input_metadata["email"] == "jane.private@example.com"


class CapturingModel:
    def __init__(self):
        self.payloads = []

    def can_parallel_complete(self, *args):
        return False

    async def complete(self, response_type, role, prompt, payload, context, version, **kwargs):
        self.payloads.append(payload)
        if response_type is QueryPlan:
            return QueryPlan(queries=[]), []
        if response_type is SourceDecisionSet:
            return SourceDecisionSet(decisions=[]), []
        return ExtractionResponse(claims=[]), []


@pytest.mark.asyncio
async def test_all_provider_stages_receive_only_safe_context_without_input_metadata():
    model = CapturingModel()
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="Example Foundation",
        university_name="Example University",
        historical_employer="Former Foundation",
        subject="Economics",
        known_attributes={
            "LIST_NAME": "Private Prospect Batch",
            "personal_email": "jane.private@example.com",
            "record_id": "private-record-8754",
            "unknown": "Private Family Information",
        },
    )
    advisor = SourceAdvisor(model)
    await advisor.plan(seed, [], {"unresolved_fields": ["university_name", "degree_type", "subject"]})
    await advisor.validate(seed, [SourceCandidate(url="https://example.edu/jane")], {})
    async for _ in extract_chunks(
        model,
        seed,
        ["Jane Doe studied Economics at Example University."],
        "https://example.edu/jane",
        {},
        asyncio.Semaphore(1),
        1,
    ):
        pass

    assert len(model.payloads) == 3
    for payload in model.payloads:
        serialized = json.dumps(payload)
        assert "Example University" in serialized
        assert "input_metadata" not in serialized
        for value in seed.known_attributes.values():
            assert value not in serialized
    assert "historical_employer" not in model.payloads[0]["seed"]


@pytest.mark.parametrize("context", ["organisation", "university_name", "historical_employer"])
def test_institutional_context_supports_full_name_but_not_partial_namesakes(context):
    seed = PersonSeed(full_name="Jane Doe", **{context: "Example Institution"})
    policy = ScoringPolicy()
    matched = assess_identity(
        seed,
        "Jane Doe is profiled by Example Institution.",
        policy,
        source_type=SourceType.university,
    )
    namesake = assess_identity(
        seed,
        "Jane Smith is profiled by Unrelated Institution. Doe is a common name.",
        policy,
        source_type=SourceType.university,
    )
    assert matched.score > namesake.score
    assert context in matched.signals["matched_seed_anchors"]
    assert namesake.rejected or namesake.score < policy.identity_minimum


async def run_fixture(tmp_path, monkeypatch, seed, **fixture_options):
    tmp_path.mkdir()
    settings = configured(tmp_path, MAX_SEARCH_QUERIES_PER_PERSON=3, MAX_SOURCES_PER_PERSON=3)
    store = migrated_store(settings)
    pipeline, calls = fake_services(settings, store, monkeypatch, **fixture_options)
    result = await pipeline.research(str(uuid4()), str(uuid4()), seed)
    return result, calls


@pytest.mark.asyncio
async def test_rich_seed_preserves_provider_work_for_already_complete_fixture(tmp_path, monkeypatch):
    baseline, baseline_calls = await run_fixture(
        tmp_path / "baseline",
        monkeypatch,
        PersonSeed(full_name="Jane Doe", organisation="Example Foundation"),
    )
    rich, rich_calls = await run_fixture(
        tmp_path / "rich",
        monkeypatch,
        PersonSeed(
            full_name="Jane Doe",
            organisation="Example Foundation",
            job_title="Programme Director",
            university_name="Example University",
            degree_type="BSc",
            subject="Economics",
            industry="Public Policy",
            known_attributes={"record_id": "private-id", "LIST_NAME": "private-batch"},
        ),
    )
    assert rich.profile.coverage == baseline.profile.coverage
    for field in ProfileField:
        assert rich.profile.fields[field].value == baseline.profile.fields[field].value
    assert rich_calls["plans"] == baseline_calls["plans"] == 0
    for key in ("queries", "fetched", "validated", "models"):
        assert len(rich_calls[key]) == len(baseline_calls[key]), key
    assert rich.profile.metrics.queries_performed == baseline.profile.metrics.queries_performed == 1
    assert rich.profile.metrics.llm_calls == baseline.profile.metrics.llm_calls
    assert rich.profile.metrics.tokens_used == baseline.profile.metrics.tokens_used


@pytest.mark.asyncio
async def test_supplied_university_recovers_education_with_no_required_planner_call(tmp_path, monkeypatch):
    employer = "https://employer.example.org/jane"
    university = "https://university.example.edu/jane"

    def sources(query, call):
        return [university] if "Example University" in query else [employer]

    def claims(payload):
        if payload["source_url"] == university:
            return {"university_name": "Example University", "degree_type": "BSc", "subject": "Economics"}
        return {"organisation": "Example Foundation", "job_title": "Programme Director"}

    baseline, baseline_calls = await run_fixture(
        tmp_path / "baseline",
        monkeypatch,
        PersonSeed(full_name="Jane Doe", organisation="Example Foundation"),
        search_urls=sources,
        extraction_mapping=claims,
        current_claim_fields={"organisation", "job_title"},
    )
    rich, rich_calls = await run_fixture(
        tmp_path / "rich",
        monkeypatch,
        PersonSeed(
            full_name="Jane Doe",
            organisation="Example Foundation",
            university_name="Example University",
            subject="Economics",
        ),
        search_urls=sources,
        extraction_mapping=claims,
        current_claim_fields={"organisation", "job_title"},
    )
    assert baseline.profile.fields[ProfileField.university_name].value is None
    assert rich.profile.fields[ProfileField.university_name].value == "Example University"
    assert rich.profile.fields[ProfileField.degree_type].value == "Bachelor's Degree"
    assert rich.profile.coverage > baseline.profile.coverage
    assert rich_calls["plans"] == 0
    assert baseline_calls["plans"] >= 1
    assert len(rich_calls["queries"]) <= len(baseline_calls["queries"]) <= 3
    assert len(rich_calls["fetched"]) == len(set(rich_calls["fetched"])) == 2
    assert any(
        "Example University" in query and "Example Foundation" not in query for query in rich_calls["queries"]
    )
    assert rich.profile.metrics.web_search_calls_reserved <= 3


def test_second_supplied_university_can_anchor_education_without_matching_employer():
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="Example Foundation",
        known_attributes={"University Name": "First University", "College Name": "Second College"},
    )
    identity = assess_identity(seed, "Jane Doe graduated from Second College.", ScoringPolicy())
    assert identity.signals["matched_seed_anchors"] == ["university_name:2"]
    assert "SEED_IDENTITY_UNCONFIRMED" not in identity.reason_codes
    assert not assess_identity(
        seed, "Jane Doe not only attended Second College but graduated with honors.", ScoringPolicy()
    ).signals["contradicted_clues"]


def test_missing_education_is_a_deterministic_objective_without_supplied_school():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation", job_title="Programme Director")
    queries = build_context_queries(seed, ["university_name", "degree_type", "subject"])
    assert queries == ['"Jane Doe" Example Foundation education university degree']
    assert "Programme Director" not in queries[0]
    assert build_context_queries(PersonSeed(full_name="Jane Doe"), ["degree_type"]) == [
        '"Jane Doe" education university degree'
    ]
    assert build_context_queries(seed, []) == []


@pytest.mark.asyncio
async def test_known_domain_metadata_avoids_validation_without_changing_evidence(monkeypatch):
    from pathlib import Path

    import app.research.orchestrator as execution

    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    settings = configured(
        Path("."), MAX_SEARCH_QUERIES_PER_PERSON=1, MAX_SOURCES_PER_PERSON=2, SOURCES_PER_ROUND=2
    )
    settings.SCORING.domain_overrides = {
        "employer.example.org": SourceType.employer,
        "university.example.edu": SourceType.employer,
    }
    admission = execution.candidate_has_seed_identity_context
    monkeypatch.setattr(execution, "candidate_has_seed_identity_context", lambda *args: False)
    baseline, before = fake_services(
        settings, None, monkeypatch, current_claim_fields={"organisation", "job_title"}
    )
    original = await baseline.research("fixture-job", "fixture-person", seed)
    monkeypatch.setattr(execution, "candidate_has_seed_identity_context", admission)
    optimized, after = fake_services(
        settings, None, monkeypatch, current_claim_fields={"organisation", "job_title"}
    )
    result = await optimized.research("fixture-job", "fixture-person", seed)
    assert before["advisor_requests"] == 1 and after["advisor_requests"] == 0
    assert len(before["models"]) == 4 and len(after["models"]) == 3
    assert len(before["fetched"]) == len(after["fetched"]) == 2
    for field in ProfileField:
        assert result.profile.fields[field].value == original.profile.fields[field].value
        assert (
            result.profile.fields[field].research_confidence
            == original.profile.fields[field].research_confidence
        )
    assert result.profile.metrics.accounting["operations"].get("validate_candidates") is None
    assert all(source.identity.signals["matched_seed_anchors"] for source in result.sources)


@pytest.mark.asyncio
async def test_unclassified_candidates_keep_model_assisted_validation(monkeypatch):
    from pathlib import Path

    settings = configured(
        Path("."), MAX_SEARCH_QUERIES_PER_PERSON=1, MAX_SOURCES_PER_PERSON=2, SOURCES_PER_ROUND=2
    )
    pipeline, calls = fake_services(settings, None, monkeypatch)
    await pipeline.research("fixture-job", "fixture-person", PersonSeed(full_name="Jane Doe"))
    assert calls["advisor_requests"] == 1
    assert calls["validated"]


@pytest.mark.asyncio
async def test_missing_education_researches_another_source_before_paid_planning(monkeypatch):
    from pathlib import Path

    settings = configured(Path("."), MAX_SEARCH_QUERIES_PER_PERSON=2, MAX_SOURCES_PER_PERSON=3)
    employer = "https://employer.example.org/jane"
    university = "https://university.example.edu/jane"

    def sources(query_payload, call):
        query = json.loads(query_payload)["query"]
        return [university] if "education" in query else [employer]

    def claims(payload):
        if payload["source_url"] == university:
            return {
                "full_name": "Jane Doe",
                "university_name": "Example University",
                "degree_type": "BSc",
                "subject": "Economics",
            }
        return {
            "full_name": "Jane Doe",
            "organisation": "Example Foundation",
            "job_title": "Programme Director",
        }

    pipeline, calls = fake_services(
        settings,
        None,
        monkeypatch,
        search_urls=sources,
        extraction_mapping=claims,
        current_claim_fields={"organisation", "job_title"},
    )
    result = await pipeline.research(
        "fixture-job", "fixture-person", PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    )
    assert calls["plans"] == 0 and len(calls["queries"]) == 2
    assert result.profile.fields[ProfileField.university_name].value == "Example University"
    assert result.profile.fields[ProfileField.degree_type].value == "Bachelor's Degree"
    assert result.profile.fields[ProfileField.subject].value == "Economics"
    assert calls["fetched"] == [employer, university]


@pytest.mark.asyncio
async def test_auth_failure_stops_shared_provider_fallbacks(monkeypatch):
    from pathlib import Path

    settings = configured(Path("."), OPENROUTER_MAX_RETRIES=0)
    pipeline, calls = fake_services(settings, None, monkeypatch, search_status=403)
    result = await pipeline.research("fixture-job", "fixture-person", PersonSeed(full_name="Jane Doe"))
    assert len(calls["queries"]) == len(calls["models"]) == 1
    assert calls["plans"] == calls["advisor_requests"] == 0
    assert result.profile.metrics.stop_reason == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_supplied_profile_link_is_retrieved_before_search(monkeypatch):
    from pathlib import Path

    settings = configured(Path("."), MAX_SEARCH_QUERIES_PER_PERSON=1, MAX_SOURCES_PER_PERSON=2)
    url = "https://employer.example.org/jane"
    pipeline, calls = fake_services(settings, None, monkeypatch)
    result = await pipeline.research(
        "fixture-job",
        "fixture-person",
        PersonSeed(
            full_name="Jane Doe",
            organisation="Example Foundation",
            profile_link=url,
            preferred_urls=[url],
        ),
    )
    assert calls["fetched"][0] == url and calls["fetched"].count(url) == 1
    assert result.sources[0].preferred_source
    assert not result.sources[0].identity.rejected


@pytest.mark.asyncio
async def test_completed_redirect_alias_reuses_exact_processed_evidence(monkeypatch):
    from pathlib import Path

    settings = configured(
        Path("."),
        MAX_SEARCH_QUERIES_PER_PERSON=1,
        MAX_SOURCES_PER_PERSON=2,
        SOURCES_PER_ROUND=2,
        MAX_CONCURRENT_FETCHES=2,
    )
    pipeline, calls = fake_services(settings, None, monkeypatch)
    retrieve = pipeline.retrieval.retrieve

    async def alias(url, **kwargs):
        page = await retrieve(url, **kwargs)
        # Navigation changes raw HTML; the independently checked extraction text,
        # publication metadata and final source URL remain exactly the same.
        text = (
            "Jane Doe works for Example Foundation in Ghana as Programme Director. "
            "Jane Doe earned BSc in Economics at Example University."
        )
        return page.model_copy(
            update={
                "final_url": "https://employer.example.org/canonical-jane",
                "html": f"<title>Jane Doe</title><nav data-source='{url}'>Home</nav><h1>Jane Doe</h1><p>{text}</p>",
            }
        )

    monkeypatch.setattr(pipeline.retrieval, "retrieve", alias)
    result = await pipeline.research(
        "fixture-job", "fixture-person", PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    )
    assert len(calls["fetched"]) == 2
    assert [source.processing_status for source in result.sources] == ["extracted", "duplicate"]
    assert result.profile.metrics.extraction_cache_hits == 1
    assert sum(record.operation == "extract_claims" for record in result.usage) == 1
    assert {claim.source_id for claim in result.claims} == {result.sources[0].source_id}
