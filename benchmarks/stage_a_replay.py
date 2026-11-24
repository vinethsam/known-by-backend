"""Replay local evidence and recorded claim proposals without a worker or client."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, time, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns
from unittest.mock import patch

from app.config import Settings
from app.processing import PreparedContent, prepare_content
from app.research.claims import deduplicate_claims, literal_key, validate_claims
from app.research.extraction import DETERMINISTIC_EXTRACTION_VERSION
from app.research.orchestrator import _process_source
from app.research.reconciliation import reconcile
from app.research.telemetry import summarize_person
from app.retrieval.contracts import RetrievedPage
from app.retrieval.urls import canonicalise_url, domain_key, source_policy_allows
from app.schemas import (
    ExtractionResponse,
    PersonSeed,
    ProfileField,
    ResearchResult,
    SourceCandidate,
    SourceRecord,
)

FIXTURES = Path(__file__).with_name("stage_a_cases.json")
EDUCATION = ("university_name", "degree_type", "subject")
EMPLOYMENT = ("organisation", "job_title")


class RecordedChunkMismatch(ValueError):
    """A saved reply cannot be attributed to this exact input chunk."""


def comparison(actual, expected):
    if expected is None:
        return None

    def key(value):
        if isinstance(value, (tuple, list)):
            return tuple(literal_key(item) if item is not None else "" for item in value)
        return literal_key(value)

    found = Counter(map(key, actual))
    target = Counter(map(key, expected))
    return {
        "tp": (found & target).total(),
        "fp": (found - target).total(),
        "fn": (target - found).total(),
        "unexpected": sorted((found - target).elements()),
        "missing": sorted((target - found).elements()),
    }


def replay_case(case, as_of, settings, *, timing=False, produce=None, prepared_sources=None):
    person_id = case["id"]
    seed = PersonSeed.model_validate(case["seed"])
    timings = Counter(processing_ms=0, grounding_ms=0, reconciliation_ms=0)
    sources, claims, batches, residual = [], [], [], {}
    seen_urls, seen_hashes = set(), set()

    def ground(response, source, evidence, model):
        rejected, adjusted = Counter(), Counter()
        start = perf_counter_ns()
        grounded, _ = validate_claims(
            response, seed, source, evidence, model, rejection_counts=rejected, adjustment_counts=adjusted
        )
        timings["grounding_ms"] += (perf_counter_ns() - start) / 1e6
        for claim in grounded:
            claim.claim_id = f"{person_id}:claim:{len(claims) + 1:03}"
            claims.append(claim)
        diagnostics = []
        if rejected or adjusted:
            for index, proposal in enumerate(response.claims):
                rejection, adjustment = Counter(), Counter()
                validate_claims(
                    ExtractionResponse(claims=[proposal]),
                    seed,
                    source,
                    evidence,
                    model,
                    rejection_counts=rejection,
                    adjustment_counts=adjustment,
                )
                if rejection or adjustment:
                    diagnostics.append(
                        {"proposal_index": index, "rejected": rejection, "adjusted": adjustment}
                    )
        batches.append(
            {
                "source_id": source.source_id,
                "model": model,
                "evidence": evidence,
                "response": response.model_dump(mode="json", exclude_unset=True),
                "rejected": rejected,
                "adjusted": adjusted,
                "diagnostics": diagnostics,
            }
        )
        return bool(grounded) and not rejected

    for fixture in case["sources"]:
        prepared = prepared_sources.get(fixture["id"]) if prepared_sources is not None else None
        if prepared is None:
            url = fixture["url"]
            final_url = fixture.get("final_url", url)
            candidate = SourceCandidate(
                url=url, source_type=fixture["source_type"], relevance=fixture.get("relevance", "likely")
            )
            canonical = canonicalise_url(final_url)
            if (
                not source_policy_allows(url, final_url, source_type=candidate.source_type)
                or canonical in seen_urls
            ):
                raise ValueError(f"Fixture source is blocked or repeated: {fixture['id']}")
            seen_urls.add(canonical)
            markdown = fixture.get("markdown")
            page = RetrievedPage(
                requested_url=url,
                final_url=final_url,
                status=200,
                html=fixture.get("html", markdown or ""),
                captured_json=fixture.get("captured_json"),
                content_type=fixture.get(
                    "content_type", "text/plain" if markdown is not None else "text/html"
                ),
                retrieval_method=fixture.get("retrieval_method", "static"),
            )
            source = SourceRecord(
                source_id=f"{person_id}:{fixture['id']}",
                person_id=person_id,
                requested_url=url,
                final_url=final_url,
                canonical_url=canonical,
                domain=domain_key(final_url),
                source_type=candidate.source_type,
                fetch_status=page.status,
                content_type=page.content_type,
                retrieval_method=page.retrieval_method,
                title=fixture.get("title", ""),
                preferred_source=fixture.get("preferred_source", False),
            )
            start = perf_counter_ns()
            content = prepare_content(page, seed) if markdown is None else PreparedContent(markdown, [])
            chunks = _process_source(source, candidate, page, seed, settings, content=content)
            timings["processing_ms"] += (perf_counter_ns() - start) / 1e6
        else:
            # The runtime observer receives admitted residual chunks after the
            # shared structured pass. Preserve those bytes and source identity.
            source = prepared.source.model_copy(deep=True)
            page, content, chunks = prepared.page, prepared.content, prepared.chunks
        sources.append(source)
        if source.content_hash in seen_hashes:
            source.processing_status = "duplicate"
            continue
        seen_hashes.add(source.content_hash)
        if source.identity.rejected or source.identity.score < settings.SCORING.identity_minimum:
            source.processing_status = "identity_rejected"
            continue

        accepted = [
            evidence
            for evidence, response in content.structured
            if response.claims
            and ground(response, source, evidence, f"deterministic:{DETERMINISTIC_EXTRACTION_VERSION}")
        ]
        if accepted and prepared is None:
            start = perf_counter_ns()
            chunks = content.chunks(seed, settings, accepted_evidence=accepted)
            timings["processing_ms"] += (perf_counter_ns() - start) / 1e6
        if produce is not None:
            for evidence, response, model in produce(page, seed, source, content, chunks, timings):
                if response.claims:
                    ground(response, source, evidence, model)
            residual[source.source_id] = {}
            source.processing_status = "extracted"
            continue
        remaining = dict(enumerate(chunks))
        for recorded in sorted(fixture.get("responses", []), key=lambda reply: reply["chunk"]):
            evidence = remaining.pop(recorded["chunk"], None)
            if evidence is None or sha256(evidence.encode()).hexdigest() != recorded["chunk_sha256"]:
                raise RecordedChunkMismatch(
                    f"Recorded reply no longer matches its chunk: {fixture['id']}:{recorded['chunk']}"
                )
            ground(
                ExtractionResponse.model_validate(recorded["response"]),
                source,
                evidence,
                "fixture:recorded-reply",
            )
        residual[source.source_id] = remaining
        source.processing_status = "extracted" if not remaining else "replay_partial"

    claims = deduplicate_claims(claims)
    start = perf_counter_ns()
    profile = reconcile(person_id, seed, claims, sources, settings.SCORING, today=as_of)
    timings["reconciliation_ms"] = (perf_counter_ns() - start) / 1e6
    result = ResearchResult(profile=profile, sources=sources, claims=claims)
    support = summarize_person(result)["independently_supported_fields"]
    records = profile.records or [profile]

    def relationship(record, fields):
        return tuple(record.fields[field].researched_value for field in fields)

    actual_fields = {
        field: sorted({record.fields[field].researched_value for record in records} - {None})
        for field in ProfileField
    }
    education = [row for row in (relationship(record, EDUCATION) for record in records) if any(row)]
    employment = relationship(profile, EMPLOYMENT)
    expected = case["expected"]
    scores = {field: comparison(actual_fields[field], expected["fields"][field]) for field in ProfileField}
    scores["qualifications"] = comparison(education, expected["qualifications"])
    scores["employment"] = comparison([employment] if any(employment) else [], expected["employment"])

    by_claim = {claim.claim_id: claim for claim in claims}
    by_source = {source.source_id: source for source in sources}
    supporting = set()
    provenance_total = provenance_complete = 0
    for record in records:
        for field, decision in record.fields.items():
            if decision.researched_value is None:
                continue
            ids = set(decision.supporting_claim_ids)
            if field == ProfileField.full_name:
                ids.update(decision.scoring_components.get("seed_corroborating_claim_ids", []))
            if decision.selected_claim_id:
                ids.add(decision.selected_claim_id)
            supporting.update(ids)
            provenance_total += 1
            provenance_complete += bool(ids) and all(
                item in by_claim
                and by_claim[item].evidence_text
                and by_claim[item].source_id in by_source
                and by_source[by_claim[item].source_id].canonical_url
                for item in ids
            )

    return {
        "id": person_id,
        "categories": case["categories"],
        "label_basis": case["label_basis"],
        "expected": expected,
        "actual_fields": actual_fields,
        "qualification_rows": education,
        "employment_row": employment,
        "scores": scores,
        "pipeline_supported_fields": support,
        "provenance": {"complete": provenance_complete, "researched_decisions": provenance_total},
        "batches": batches,
        "unrecorded_chunks": residual,
        "grounded_without_selected_support": sorted(by_claim.keys() - supporting),
        "claims": [claim.model_dump(mode="json", exclude={"created_at"}) for claim in claims],
        "sources": [
            source.model_dump(mode="json", exclude={"retrieved_at", "raw_content"}) for source in sources
        ],
        "profile": profile.model_dump(mode="json", exclude={"started_at", "completed_at", "metrics"}),
        **({"local_ms": {key: round(value, 3) for key, value in timings.items()}} if timing else {}),
    }


def evaluate(fixture_bytes, *, timing=False, produce=None):
    dataset = json.loads(fixture_bytes)
    if dataset["version"] != 1 or not dataset["cases"]:
        raise ValueError("Expected a nonempty Stage A v1 fixture set")
    as_of = date.fromisoformat(dataset["as_of"])
    # Use code defaults, bypassing environment variables and .env credentials.
    settings = Settings.model_construct()
    with patch("app.research.claims.utcnow", return_value=datetime.combine(as_of, time(), timezone.utc)):
        cases = [
            replay_case(case, as_of, settings, timing=timing, produce=produce) for case in dataset["cases"]
        ]
    totals = {}
    for field in (*ProfileField, "qualifications", "employment"):
        scores = [case["scores"][field] for case in cases if case["scores"][field] is not None]
        tp, fp, fn = (sum(score[key] for score in scores) for key in ("tp", "fp", "fn"))
        totals[field] = {
            "evaluated_cases": len(scores),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        }
    return {
        "dataset_version": dataset["version"],
        "fixture_sha256": sha256(
            json.dumps(dataset, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "as_of": as_of.isoformat(),
        "kind": "synthetic_downstream_replay" if produce is None else "synthetic_candidate_evaluation",
        "live_model_calls": 0,
        "pipeline_supported_slots": sum(len(case["pipeline_supported_fields"]) for case in cases),
        "possible_field_slots": len(cases) * len(ProfileField),
        "scores": totals,
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timing", action="store_true")
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.fixtures.resolve():
        parser.error("Output must be separate from the fixture file")
    report = (
        json.dumps(evaluate(args.fixtures.read_bytes(), timing=args.timing), indent=2, sort_keys=True) + "\n"
    )
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
