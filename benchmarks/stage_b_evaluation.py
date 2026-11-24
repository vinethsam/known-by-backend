"""Compare the local deterministic producer with Stage A scripted replies."""

from __future__ import annotations

import argparse
import json
import tracemalloc
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from time import perf_counter_ns, process_time_ns

from app.research.claims import literal_key
from app.research.deterministic import claim_batches
from app.research.normalisation import normalise_value
from app.schemas import ProfileField
from benchmarks.stage_a_replay import FIXTURES as STAGE_A_FIXTURES
from benchmarks.stage_a_replay import comparison, evaluate

FIXTURES = Path(__file__).with_name("stage_b_cases.json")
FIELD_TIMING = {
    "full_name": ["full_name_ms"],
    "organisation": ["employment_ms"],
    "job_title": ["employment_ms"],
    "university_name": ["education_ms"],
    "degree_type": ["education_ms"],
    "subject": ["education_ms"],
    "profile_link": ["profile_link_ms", "profile_metadata_ms"],
}


def proposals(page, seed, source, content, chunks, timings):
    return claim_batches(page, seed, source, content, chunks, timings=timings)


def _combined_fixtures(recorded_bytes, extension_bytes):
    recorded, extension = json.loads(recorded_bytes), json.loads(extension_bytes)
    if (recorded["version"], recorded["as_of"]) != (extension["version"], extension["as_of"]):
        raise ValueError("Replay fixtures must use the same format and as-of date")
    cases = recorded["cases"] + extension["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Replay case IDs must be unique")
    return json.dumps({**recorded, "cases": cases}).encode()


def _field_flow(cases):
    proposed, rejected, grounded = Counter(), Counter(), Counter()
    by_producer = defaultdict(Counter)
    reasons = Counter()
    for case in cases:
        for batch in case["batches"]:
            claims = batch["response"]["claims"]
            proposed.update(claim["field"] for claim in claims)
            for issue in batch["diagnostics"]:
                if issue["rejected"]:
                    rejected[claims[issue["proposal_index"]]["field"]] += 1
            reasons.update(batch["rejected"])
        grounded.update(claim["field"] for claim in case["claims"])
        for claim in case["claims"]:
            by_producer[claim["extraction_model"]][claim["field"]] += 1
    return {
        "by_field": {
            field: {
                "proposed": proposed[field],
                "proposals_with_rejection": rejected[field],
                "proposals_without_rejection": proposed[field] - rejected[field],
                "unique_grounded_claims": grounded[field],
            }
            for field in ProfileField
        },
        "rejection_reasons": dict(reasons),
        "unique_grounded_by_producer": dict(by_producer),
    }


def _employment_pairs(claims):
    groups = defaultdict(lambda: defaultdict(set))
    for claim in claims:
        field = claim["field"]
        if claim["fact_group"] is None or field not in {"organisation", "job_title"}:
            continue
        status = {None: "unspecified", True: "current", False: "former"}[claim["is_current"]]
        key = claim["source_id"], claim["fact_group"], status, claim["as_of_date"], claim["end_date"]
        groups[key][field].add(claim["normalised_value"])
    # Ambiguous groups stay visible in the claim audit and cannot become scored pairs.
    return sorted(
        {
            (next(iter(fields["organisation"])), next(iter(fields["job_title"])), *key[2:])
            for key, fields in groups.items()
            if len(fields["organisation"]) == len(fields["job_title"]) == 1
        },
        key=lambda pair: tuple(value or "" for value in pair),
    )


def _direct_audit(cases):
    audited = []
    for case in cases:
        expected = case["expected"]
        actual = defaultdict(set)
        for claim in case["claims"]:
            actual[claim["field"]].add(claim["normalised_value"])
        links = sorted(actual["profile_link"])
        employment = _employment_pairs(case["claims"])
        rejected = sorted(
            source["source_id"].split(":", 1)[-1]
            for source in case["sources"]
            if source["processing_status"] == "identity_rejected"
        )
        forbidden = {
            (field, normalise_value(ProfileField(field), value).value)
            for field, values in expected.get("forbidden_claims", {}).items()
            for value in values
        }
        forbidden_fields = {field for field, _ in forbidden}
        proposed_forbidden = sorted(
            {
                (claim["field"], claim["value"])
                for batch in case["batches"]
                for claim in batch["response"]["claims"]
                if claim["field"] in forbidden_fields
                and (claim["field"], normalise_value(ProfileField(claim["field"]), claim["value"]).value)
                in forbidden
            }
        )
        grounded_forbidden = sorted(
            {
                (claim["field"], claim["raw_value"])
                for claim in case["claims"]
                if (claim["field"], claim["normalised_value"]) in forbidden
            }
        )
        expected_employment = expected.get("employment_claim_pairs")
        if expected_employment is not None:
            expected_employment = [
                [
                    normalise_value(ProfileField.organisation, org).value,
                    normalise_value(ProfileField.job_title, title).value,
                    *metadata,
                ]
                for org, title, *metadata in expected_employment
            ]
        field_scores = {}
        if "direct_profile_links" in expected:
            # These extension cases were authored with complete grounded labels.
            # Selected-field labels omit former jobs and representative URLs.
            for field in ProfileField:
                values = expected["fields"][field]
                if field == ProfileField.profile_link:
                    values = expected["direct_profile_links"]
                target = {normalise_value(field, value).value for value in values or []}
                if field == ProfileField.organisation:
                    target.update(org for org, *_ in expected_employment or [])
                if field == ProfileField.job_title:
                    target.update(title for _, title, *_ in expected_employment or [])
                field_scores[field] = comparison(
                    actual[field], sorted(target) if values is not None else None
                )
        audited.append(
            {
                "id": case["id"],
                "direct_profile_links": links,
                "profile_link_score": comparison(links, expected.get("direct_profile_links")),
                "grounded_field_scores": field_scores,
                "employment_claim_pairs": employment,
                "employment_claim_pair_score": comparison(employment, expected_employment),
                "identity_rejected_sources": rejected,
                "identity_rejection_score": comparison(rejected, expected.get("identity_rejected_sources")),
                "forbidden_claims_proposed": proposed_forbidden,
                "forbidden_claims_grounded": grounded_forbidden,
            }
        )
    return audited


def _compared(recorded, current):
    by_id = {case["id"]: case for case in current["cases"]}
    counts = Counter({"both": 0, "deterministic_only": 0, "recorded_only": 0})
    changes = []
    for prior in recorded["cases"]:
        case = by_id[prior["id"]]
        for field in ProfileField:
            expected = prior["expected"]["fields"][field]
            old = {literal_key(value) for value in prior["actual_fields"][field]}
            new = {literal_key(value) for value in case["actual_fields"][field]}
            if expected is not None:
                labels = {literal_key(value) for value in expected}
                counts["both"] += len(labels & old & new)
                counts["deterministic_only"] += len(labels & (new - old))
                counts["recorded_only"] += len(labels & (old - new))
            if old != new:
                changes.append(
                    {
                        "id": case["id"],
                        "field": field,
                        "label_verified": expected is not None,
                        "scripted": prior["actual_fields"][field],
                        "deterministic": case["actual_fields"][field],
                    }
                )
    return {"correct_value_counts": dict(counts), "changed_field_values": changes}


def _aggregate(scores):
    scores = [score for score in scores if score is not None]
    tp, fp, fn = (sum(score[metric] for score in scores) for metric in ("tp", "fp", "fn"))
    return {
        "scored_cases": len(scores),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def run(recorded_bytes, extension_bytes, *, repeats=0):
    dataset = _combined_fixtures(recorded_bytes, extension_bytes)
    recorded = evaluate(recorded_bytes)
    current = evaluate(dataset, produce=proposals)
    matched = evaluate(recorded_bytes, produce=proposals)
    field_scores = [case["scores"][field] for case in current["cases"] for field in ProfileField]
    reviewed_positive = [score for score in field_scores if score is not None and score["tp"] + score["fn"]]
    audit = _direct_audit(current["cases"])
    report = {
        "dataset": current,
        "direct_claim_audit": audit,
        "direct_claim_scores": {
            key: _aggregate(case[key] for case in audit)
            for key in ("profile_link_score", "employment_claim_pair_score", "identity_rejection_score")
        },
        "grounded_field_scores": {
            field: _aggregate(case["grounded_field_scores"].get(field) for case in audit)
            for field in ProfileField
        },
        "field_flow": _field_flow(current["cases"]),
        "reviewed_positive_field_slots": len(reviewed_positive),
        "correctly_supported_field_slots": sum(bool(score["tp"]) for score in reviewed_positive),
        "matched_stage_a": {
            "scripted_scores": recorded["scores"],
            "deterministic_scores": matched["scores"],
            "scripted_pipeline_supported_slots": recorded["pipeline_supported_slots"],
            "deterministic_pipeline_supported_slots": matched["pipeline_supported_slots"],
            "scripted_responses_replaced": sum(
                batch["model"] == "fixture:recorded-reply"
                for case in recorded["cases"]
                for batch in case["batches"]
            ),
            **_compared(recorded, matched),
        },
    }
    if repeats:
        report["timing"] = _benchmark(dataset, repeats)
    return report


def _benchmark(dataset, repeats):
    evaluate(dataset, produce=proposals)
    samples = defaultdict(list)
    for _ in range(repeats):
        wall, cpu = perf_counter_ns(), process_time_ns()
        result = evaluate(dataset, timing=True, produce=proposals)
        samples["replay_wall_ms"].append((perf_counter_ns() - wall) / 1e6)
        samples["process_cpu_ms"].append((process_time_ns() - cpu) / 1e6)
        stages = Counter()
        for case in result["cases"]:
            stages.update(case["local_ms"])
        for stage, elapsed in stages.items():
            samples[stage].append(elapsed)
    tracemalloc.start()
    try:
        evaluate(dataset, produce=proposals)
        peak_bytes = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {
        "repeats": repeats,
        "median_ms": {name: round(median(values), 3) for name, values in samples.items()},
        "replay_wall_range_ms": [
            round(value, 3) for value in (min(samples["replay_wall_ms"]), max(samples["replay_wall_ms"]))
        ],
        "field_latency_buckets": FIELD_TIMING,
        "peak_traced_python_bytes_one_replay": peak_bytes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=0, help="Include local timing and memory measurements")
    args = parser.parse_args()
    if not 0 <= args.repeats <= 100:
        parser.error("--repeats must be between 0 and 100")
    if args.output and args.output.resolve() in {STAGE_A_FIXTURES.resolve(), FIXTURES.resolve()}:
        parser.error("Output must be separate from the fixture files")
    report = run(STAGE_A_FIXTURES.read_bytes(), FIXTURES.read_bytes(), repeats=args.repeats)
    data = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(data, encoding="utf-8")
    else:
        print(data, end="")


if __name__ == "__main__":
    main()
