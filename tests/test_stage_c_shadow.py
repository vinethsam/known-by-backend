import json
import socket

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.processing import prepare_content
from app.research.claims import validate_claims
from app.research.decision_trace import log_decision
from app.research.orchestrator import _process_source
from app.research.reconciliation import reconcile
from app.research.shadow import ExtractionShadow
from app.retrieval.contracts import RetrievedPage
from app.retrieval.urls import domain_key
from app.schemas import (
    ExtractionResponse,
    PersonSeed,
    ResearchResult,
    SourceCandidate,
    SourceRecord,
    UsageRecord,
)
from benchmarks.stage_a_replay import FIXTURES
from benchmarks.stage_c_shadow import ReviewedFacts, compare_shadow, evaluate_retained, write_report


def _manifest():
    # Reuse a test fixture to verify the loader. This does not establish genuine
    # capture provenance or generate a real-world evaluation artifact.
    manifest = json.loads(FIXTURES.read_bytes())
    manifest["evidence_kind"] = "approved_retained_webpage"
    manifest["cases"] = manifest["cases"][:1]
    manifest["cases"][0]["sources"][0]["relevance"] = "likely"
    return manifest


def _offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Shadow comparison attempted to open a live client or socket")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx.Client, "__init__", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", blocked)


def test_retained_webpage_comparison_requires_exact_model_2_evidence(monkeypatch):
    _offline(monkeypatch)
    with pytest.raises(ValidationError, match="evidence_kind"):
        evaluate_retained(FIXTURES.read_bytes())
    manifest = _manifest()
    report = evaluate_retained(json.dumps(manifest).encode(), measure_memory=True)
    [case] = report["cases"]
    assert report["live_model_calls"] == report["live_api_spend"] == 0
    assert report["peak_traced_python_bytes_in_separate_replay"] > 0
    assert report["scores"]["qualifications"]["tp"] == 2
    assert case["comparison"]["status"] == "matched"
    assert case["comparison"]["recorded_replay"]["scores"]["qualifications"]["fn"] == 1
    assert case["fully_correct_supported_slots"] == case["reviewed_positive_slots"]

    manifest["cases"][0]["sources"][0]["responses"][0]["chunk_sha256"] = "0" * 64
    [mismatched] = evaluate_retained(json.dumps(manifest).encode())["cases"]
    assert mismatched["scores"]["qualifications"]["tp"] == 2
    assert mismatched["comparison"]["reason"] == "recorded_chunk_hash_mismatch"


def test_retained_markdown_respects_review_unknowns_and_safe_output(monkeypatch, tmp_path):
    _offline(monkeypatch)
    manifest = _manifest()
    case = manifest["cases"][0]
    source = case["sources"][0]
    page = RetrievedPage(
        requested_url=source["url"], final_url=source["url"], status=200, html=source.pop("html")
    )
    source["markdown"] = prepare_content(page, PersonSeed.model_validate(case["seed"])).text
    source.pop("responses")
    case["expected"]["fields"]["profile_link"] = None
    report = evaluate_retained(json.dumps(manifest).encode())
    assert report["scores"]["profile_link"]["scored_cases"] == 0
    assert report["cases"][0]["comparison"]["status"] == "unavailable"
    assert list(tmp_path.iterdir()) == []
    output = tmp_path / "private.json"
    write_report(output, report)
    assert json.loads(output.read_bytes())["manifest_sha256"] == report["manifest_sha256"]
    with pytest.raises(FileExistsError):
        write_report(output, {})


def _capture():
    [case] = _manifest()["cases"]
    [item] = case["sources"]
    settings = Settings.model_construct()
    seed = PersonSeed.model_validate(case["seed"])
    url = item["url"]
    page = RetrievedPage(
        requested_url=url, final_url=url, status=200, content_type="text/html", html=item["html"]
    )
    candidate = SourceCandidate(url=url, source_type=item["source_type"], relevance="likely")
    source = SourceRecord(
        person_id="person", requested_url=url, final_url=url, canonical_url=url, domain=domain_key(url)
    )
    content = prepare_content(page, seed)
    chunks = _process_source(source, candidate, page, seed, settings, content=content)
    [reply] = item["responses"]
    claims, _ = validate_claims(
        ExtractionResponse.model_validate(reply["response"]), seed, source, chunks[0], "test"
    )
    profile = reconcile(source.person_id, seed, claims, [source], settings.SCORING)
    primary = ResearchResult(
        profile=profile,
        sources=[source.model_copy(update={"processing_status": "extracted"}, deep=True)],
        claims=claims,
        usage=[
            UsageRecord(
                job_id="job",
                person_id=source.person_id,
                role="extraction",
                model="test",
                operation="extract_claims",
                prompt_version="claims-v9",
                cost=0.02,
            )
        ],
    )
    return (
        settings,
        seed,
        source,
        page,
        content,
        chunks,
        primary,
        ReviewedFacts.model_validate(case["expected"]),
    )


def test_same_run_shadow_uses_copies_and_existing_primary_ledger(monkeypatch, caplog):
    _offline(monkeypatch)
    settings, seed, source, page, content, chunks, primary, expected = _capture()
    shadow = ExtractionShadow()

    def blocked(*args, **kwargs):
        pytest.fail("Prepared shadow input was reprocessed")

    monkeypatch.setattr("benchmarks.stage_a_replay.prepare_content", blocked)
    before = primary.model_dump_json()
    shadow.observe(seed, source, page, content, chunks)
    source.identity.rejected = True
    page.html = ""
    chunks.clear()
    with caplog.at_level("DEBUG", logger="app.research.decision_trace"):
        report = compare_shadow(shadow, settings=settings, primary=primary, expected=expected)
        assert not caplog.records
        log_decision("normal_research", person_id="person")
        assert len(caplog.records) == 1
    assert report["comparison"]["status"] == "matched"
    assert report["candidate"]["scores"]["qualifications"]["tp"] == 2
    assert report["comparison"]["scores"]["qualifications"]["fn"] == 1
    accounting = report["comparison"]["provider_accounting"]
    assert accounting["actual_api_cost"] == 0.02
    assert accounting["operations"]["extract_claims"]["model_calls"] == 1
    assert report["comparison"]["actual_api_cost_per_fully_correct_supported_field"] > 0
    assert primary.model_dump_json() == before

    primary.usage[0].cost = None
    assert (
        compare_shadow(shadow, settings=settings, primary=primary, expected=expected)["comparison"][
            "actual_api_cost_per_fully_correct_supported_field"
        ]
        is None
    )

    shadow.note_omission("capture-error-after-another-source")
    assert (
        compare_shadow(shadow, settings=settings, primary=primary, expected=expected)["comparison"]["reason"]
        == "incomplete_or_unmatched_primary_evidence"
    )


def test_capped_capture_and_missing_primary_source_do_not_claim_comparison():
    settings, seed, source, page, content, chunks, primary, expected = _capture()
    shadow = ExtractionShadow(max_bytes=1)
    shadow.observe(seed, source, page, content, chunks)
    assert shadow.snapshot().omitted_source_ids == (source.source_id,)
    assert (
        compare_shadow(shadow, settings=settings, primary=primary, expected=expected)["comparison"]["status"]
        == "unavailable"
    )

    shadow = ExtractionShadow()
    shadow.observe(seed, source, page, content, chunks)
    primary.sources.append(primary.sources[0].model_copy(update={"source_id": "missing"}))
    assert (
        compare_shadow(shadow, settings=settings, primary=primary, expected=expected)["comparison"]["status"]
        == "unavailable"
    )
