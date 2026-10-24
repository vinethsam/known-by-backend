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
