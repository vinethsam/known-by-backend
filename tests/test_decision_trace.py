"""Production-readable diagnostics stay compact, safe and separate from research output."""

import json
import logging

import pytest

from app.config import ScoringPolicy, Settings
from app.logging import JsonFormatter
from app.research.decision_trace import log_decision, log_result_decisions
from app.research.orchestrator import ResearchOrchestrator
from app.research.reconciliation import reconcile
from app.retrieval.service import RetrievedPage
from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    IdentityMatch,
    PersonProfile,
    PersonSeed,
    ProfileField,
    ProfileRecord,
    ResearchMetrics,
    ResearchResult,
    SourceRecord,
)

TRACE_LOGGER = "app.research.decision_trace"


def fixture_result():
    fields = {field: FieldDecision() for field in ProfileField}
    for field, value in {
        ProfileField.full_name: "Jane Doe",
        ProfileField.organisation: "Example Foundation",
        ProfileField.job_title: "Director",
        ProfileField.university_name: "Example University",
        ProfileField.degree_type: "Master's Degree",
        ProfileField.subject: "Economics",
        ProfileField.profile_link: "https://example.org/jane",
    }.items():
        fields[field] = FieldDecision(value=value, confidence=90, review_required=False)
    fields[ProfileField.job_title].alternative_claim_ids = ["historical"]
    fields[ProfileField.job_title].scoring_components = {"authority": 0.95, "conflict_penalty": 0}
    record = ProfileRecord(
        record_id="education:fixture",
        fields=fields,
        profile_confidence=90,
        coverage=100,
        review_required=False,
    )
    source = SourceRecord(
        source_id="official",
        person_id="person",
        requested_url="https://example.org/jane",
        final_url="https://example.org/jane",
        canonical_url="https://example.org/jane",
        domain="example.org",
        processing_status="extracted",
        raw_content="private HTML blob",
        compacted_text="private evidence blob",
        identity=IdentityMatch(score=0.95, signals={"matched_seed_anchors": ["organisation"]}),
    )
    alternative = EvidenceClaim(
        claim_id="historical",
        person_id="person",
        source_id="official",
        field=ProfileField.job_title,
        raw_value="Analyst",
        normalised_value="analyst",
        is_current=False,
        evidence_text="private verbatim quotation",
        subject_name="Jane Doe",
        extraction_model="fixture",
    )
    return ResearchResult(
        profile=PersonProfile(
            person_id="person",
            input_name="Jane Doe",
            fields=fields,
            records=[record],
            profile_confidence=90,
            coverage=100,
            review_required=False,
            research_status="clean",
            metrics=ResearchMetrics(sources_accepted=2, stop_reason="DISCOVERY_EXHAUSTED"),
        ),
        sources=[source],
        claims=[alternative],
    )


def trace_records(caplog):
    return [record for record in caplog.records if record.name == TRACE_LOGGER]


def test_info_summary_has_decisions_in_text_and_structured_metadata_without_mutation(caplog):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="Example Foundation",
        known_attributes={
            "alumni_organisation": "Past Institution",
            "LIST_NAME": "Private list metadata",
            "notes": "Private arbitrary note",
        },
    )
    result = fixture_result()
    before = result.model_dump(mode="json")

    log_result_decisions(seed, result, job_id="job", candidate_count=4, rejected_source_count=1)

    records = trace_records(caplog)
    assert len(records) == 1
    record = records[0]
    text_data = json.loads(record.getMessage().split(" ", 1)[1])
    formatted = json.loads(JsonFormatter().format(record))
    assert text_data == formatted["decision"]
    assert formatted["job_id"] == "job" and formatted["person_id"] == "person"
    assert text_data["seed_name"] == "Jane Doe"
    assert text_data["seed_context"] == {
        "alumni_organisation": "Past Institution",
        "organisation": "Example Foundation",
    }
    assert text_data["candidate_count"] == 4
    assert text_data["accepted_source_count"] == 2
    assert text_data["rejected_source_count"] == 1
    assert text_data["extraction_claim_count"] == 1
    assert text_data["selected_relationship"] == {
        "job_title": "Director",
        "organisation": "Example Foundation",
    }
    assert text_data["selected_confidence"]["job_title"] == 90
    assert text_data["credential_count"] == 1
    assert text_data["profile_link"] == "https://example.org/jane"
    assert text_data["review_outcome"] == "clean"
    assert text_data["review_required"] is False
    assert len(record.getMessage()) < 1200
    assert "private" not in record.getMessage().casefold()
    assert result.model_dump(mode="json") == before


def test_debug_field_and_identity_details_explain_decisions_without_evidence_blobs(caplog):
    caplog.set_level(logging.DEBUG, logger=TRACE_LOGGER)
    log_result_decisions(
        PersonSeed(full_name="Jane Doe"),
        fixture_result(),
        job_id="job",
        candidate_count=4,
        rejected_source_count=1,
    )
    records = trace_records(caplog)
    assert sum(record.levelno == logging.INFO for record in records) == 1
    title = next(
        record.decision for record in records if getattr(record, "decision", {}).get("field") == "job_title"
    )
    assert title["alternatives"][0]["value"] == "Analyst"
    assert title["components"] == {"authority": 0.95, "conflict_penalty": 0}
    identity = next(
        record.decision for record in records if record.getMessage().startswith("decision_identity")
    )
    assert identity["signals"]["matched_seed_anchors"] == ["organisation"]
    assert "private" not in " ".join(record.getMessage() for record in records).casefold()


def test_deep_trace_is_deterministic_bounded_and_drops_secret_or_raw_payloads(caplog):
    caplog.set_level(logging.DEBUG, logger=TRACE_LOGGER)
    details = {
        "winner": {"title": "Prime Minister", "score": 0.94, "currentness": True},
        "runner_up": {"title": "Analyst", "score": 0.24},
        "url": "https://user:password@example.org/biography?access_token=secret-token#private-fragment",
        "reason": "CURRENT_PRIMARY_ROLE\n beats historical",
        "evidence_text": "sensitive quotation",
        "prompt": "private prompt",
        "api_key": "private key",
        "raw_content": "HTML" * 100000,
        "components": {"authority": 0.95, "api_secret": "hidden", "model_response": "hidden"},
        "candidates": [{"value": "x" * 3000, "nested": {"evidence": "hidden"}} for _ in range(1000)],
    }
    for _ in range(2):
        log_decision("current_role", person_id="person", **details)

    records = trace_records(caplog)
    assert records[0].getMessage() == records[1].getMessage()
    assert len(records[0].getMessage()) < 6000
    data = records[0].decision
    assert data["url"] == "https://example.org/biography"
    assert data["winner"]["title"] == "Prime Minister"
    assert data["reason"] == "CURRENT_PRIMARY_ROLE beats historical"
    assert data["components"] == {"authority": 0.95}
    assert data["candidates"][-1] == "+992 omitted"
    for forbidden in ("secret-token", "private", "sensitive quotation", "hidden", "HTML", "password"):
        assert forbidden not in records[0].getMessage()


def test_debug_details_are_not_emitted_at_info(caplog):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    log_decision("education", person_id="person", reason="compatible partial evidence attached")
    assert not trace_records(caplog)


def test_large_seed_context_does_not_hide_required_plaintext_summary_fields(caplog):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="O" * 200,
        job_title="T" * 200,
        university_name="U" * 200,
        country="C" * 100,
        location="L" * 200,
        subject="S" * 200,
        known_attributes={f"employer context {index} " + "x" * 60: "V" * 200 for index in range(10)},
    )
    result = fixture_result()
    log_result_decisions(seed, result, job_id="job", candidate_count=4, rejected_source_count=1)

    record = trace_records(caplog)[0]
    summary = json.loads(record.getMessage().split(" ", 1)[1])
    assert {
        "seed_name",
        "seed_context",
        "candidate_count",
        "accepted_source_count",
        "rejected_source_count",
        "selection_eligible_source_count",
        "selection_rejected_source_count",
        "extraction_claim_count",
        "selected_relationship",
        "selected_confidence",
        "credential_count",
        "profile_link",
        "review_required",
        "review_outcome",
        "review_reasons",
        "stop_reason",
    } <= summary.keys()
    assert summary["seed_name"] == "Jane Doe"
    assert summary["selected_relationship"] == {
        "job_title": "Director",
        "organisation": "Example Foundation",
    }
    assert summary["selected_confidence"]["job_title"] == 90
    assert len(summary["seed_context"]) <= 8
    assert all(len(value) <= 64 for value in summary["seed_context"].values())
    assert len(record.getMessage()) < 4000


def test_final_identity_and_source_policy_exclusions_are_observable_without_mutation(caplog):
    caplog.set_level(logging.DEBUG, logger=TRACE_LOGGER)
    seed = PersonSeed(full_name="Jane Doe")
    policy = ScoringPolicy()
    result = fixture_result()
    sources, claims = [], []
    for key, field, value in [
        ("business", ProfileField.organisation, "Alpha Corporation"),
        ("academic", ProfileField.university_name, "Beta University"),
    ]:
        source = result.sources[0].model_copy(
            deep=True,
            update={
                "source_id": key,
                "identity": IdentityMatch(score=0.55, ambiguous=True),
                "authority_score": 0.9,
                "requested_url": f"https://{key}.example/profile",
                "final_url": f"https://{key}.example/profile",
                "canonical_url": f"https://{key}.example/profile",
            },
        )
        sources.append(source)
        claims.append(
            result.claims[0].model_copy(
                deep=True,
                update={
                    "claim_id": key,
                    "source_id": key,
                    "field": field,
                    "raw_value": value,
                    "normalised_value": value.lower(),
                    "identity_relevance": 0.55,
                },
            )
        )
    result.profile = reconcile("person", seed, claims, sources, policy)
    result.profile.metrics.sources_accepted = 2
    result.sources, result.claims = sources, claims
    blocked = sources[0].model_copy(
        deep=True,
        update={
            "source_id": "blocked",
            "requested_url": "https://linkedin.com/in/jane",
            "final_url": "https://linkedin.com/in/jane",
            "canonical_url": "https://linkedin.com/in/jane",
        },
    )
    before = result.model_dump(mode="json")
    log_result_decisions(
        seed,
        result,
        job_id="job",
        candidate_count=3,
        rejected_source_count=1,
        source_records=[*sources, blocked],
        policy=policy,
    )

    records = trace_records(caplog)
    summary = json.loads(
        next(record.getMessage() for record in records if record.levelno == logging.INFO).split(" ", 1)[1]
    )
    assert summary["accepted_source_count"] == 2  # Retrieval identity gate precedes reconciliation.
    assert summary["selection_eligible_source_count"] == 0
    assert summary["selection_rejected_source_count"] == 3
    identities = {
        record.decision["source_id"]: record.decision
        for record in records
        if record.getMessage().startswith("decision_identity")
    }
    assert all(
        decision["rejected"] and not decision["selection_eligible"] for decision in identities.values()
    )
    assert "IDENTITY_CONTEXT_NOT_CONNECTED" in identities["business"]["reasons"]
    assert "IDENTITY_CONTEXT_NOT_CONNECTED" in identities["academic"]["reasons"]
    assert "SOURCE_POLICY_BLOCKED" in identities["blocked"]["reasons"]
    assert result.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_pipeline_emits_one_final_summary_after_checkpoints_and_counts_rejected_sources(caplog):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    settings = Settings(_env_file=None, APP_ENV="test", MAX_SOURCES_PER_PERSON=1)
    calls, checkpoints = [], []

    class Model:
        async def complete(self, schema, role, prompt, payload, context, prompt_version, **kwargs):
            calls.append(payload["task"])
            assert payload["task"] == "validate_candidates"
            return schema.model_validate(
                {
                    "decisions": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "source_type": "employer",
                            "relevance": "likely",
                        }
                        for candidate in payload["candidates"]
                    ]
                }
            ), []

    class Search:
        async def search(self, *args, **kwargs):
            raise AssertionError("Tracing must not trigger search")

    class Retrieval:
        async def retrieve(self, url, **kwargs):
            return RetrievedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                html="<h1>Another Person</h1><p>Different biography and identity.</p>",
            )

    async def checkpoint(result):
        checkpoints.append(result)

    result = await ResearchOrchestrator(settings, Search(), Model(), Retrieval()).research(
        "job",
        "person",
        PersonSeed(
            full_name="Jane Doe",
            preferred_urls=["https://linkedin.com/in/jane", "https://example.org/biography"],
        ),
        checkpoint,
    )

    summaries = [
        record for record in trace_records(caplog) if record.getMessage().startswith("decision_summary")
    ]
    assert len(summaries) == 1 and len(checkpoints) > 1
    summary = summaries[0].decision
    assert summary["candidate_count"] == 2
    assert summary["accepted_source_count"] == 0
    assert summary["rejected_source_count"] == 2
    assert summary["extraction_claim_count"] == 0
    assert summary["credential_count"] == 0
    assert summary["profile_link"] is None
    assert summary["selected_relationship"] == {"organisation": None, "job_title": None}
    assert calls == ["validate_candidates"]
    assert not result.claims
