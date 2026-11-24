"""Review explicitly supplied local evidence without fetching or calling a model."""

from __future__ import annotations

import argparse
import json
import os
import tracemalloc
from datetime import date
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns, process_time_ns
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config import Settings
from app.research.decision_trace import suppress_decision_logging
from app.research.shadow import ExtractionShadow
from app.research.telemetry import summarize_person
from app.schemas import ExtractionResponse, PersonSeed, ProfileField, ResearchResult, SourceType
from benchmarks.stage_a_replay import EDUCATION, EMPLOYMENT, RecordedChunkMismatch, comparison, replay_case
from benchmarks.stage_b_evaluation import _aggregate, _direct_audit, proposals

_MAX_MANIFEST_BYTES = 40_000_000
_RELEVANCE = Literal["likely", "ambiguous", "unrelated"]
_ROLE_STATUS = Literal["current", "former", "unspecified"]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ReviewedFacts(_Input):
    fields: dict[ProfileField, list[str] | None]
    qualifications: list[tuple[str | None, str | None, str | None]] | None
    employment: list[tuple[str | None, str | None]] | None
    employment_claim_pairs: list[tuple[str, str, _ROLE_STATUS, date | None, date | None]] | None = None
    identity_rejected_sources: list[str] | None = None
    direct_profile_links: list[str] | None = None
    forbidden_claims: dict[ProfileField, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_explicit_unknowns(self):
        if self.fields.keys() != set(ProfileField):
            raise ValueError("All seven reviewed fields are required; use null for an unknown field")
        return self


class _RecordedReply(_Input):
    chunk: int = Field(ge=0, lt=10)
    chunk_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response: ExtractionResponse


class _RetainedSource(_Input):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    url: str
    final_url: str | None = None
    source_type: SourceType
    relevance: _RELEVANCE = "ambiguous"
    title: str = ""
    preferred_source: bool = False
    content_type: str | None = None
    retrieval_method: str = "static"
    html: str | None = None
    markdown: str | None = None
    captured_json: dict | list | None = None
    responses: list[_RecordedReply] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def require_evidence(self):
        if self.markdown is not None:
            if not self.markdown.strip() or self.html is not None or self.captured_json is not None:
                raise ValueError("Prepared Markdown is a separate, nonempty evidence format")
            if self.responses:
                raise ValueError("Model 2 replay requires the original page, not prepared Markdown")
        elif not (self.html or "").strip() and self.captured_json is None:
            raise ValueError("A source requires retained page evidence")
        if len({reply.chunk for reply in self.responses}) != len(self.responses):
            raise ValueError("Recorded chunks must be unique within a source")
        return self


class _Case(_Input):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    categories: list[str] = Field(default_factory=list)
    seed: PersonSeed
    sources: list[_RetainedSource] = Field(min_length=1, max_length=30)
    expected: ReviewedFacts
    label_basis: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_sources(self):
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique within a case")
        return self


class _Manifest(_Input):
    version: Literal[1]
    evidence_kind: Literal["approved_retained_webpage"]
    as_of: date
    cases: list[_Case] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_people(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        return self


def _review_dict(expected: ReviewedFacts) -> dict:
    # Preserve explicit nulls for the selected fields. Optional direct-claim
    # annotations appear only when actually supplied by the reviewer.
    return expected.model_dump(mode="json", exclude_unset=True)


def _audit(case: dict) -> dict:
    counts = {field: score for field in ProfileField if (score := case["scores"][field]) is not None}
    supported = set(case["pipeline_supported_fields"])
    return {
        "reviewed_positive_slots": sum(bool(score["tp"] + score["fn"]) for score in counts.values()),
        "fully_correct_supported_slots": sum(
            field in supported and bool(score["tp"]) and not score["fp"] and not score["fn"]
            for field, score in counts.items()
        ),
        "grounded_claim_audit": _direct_audit([case])[0],
    }


def _timed_replay(case, as_of, settings, *, prepared_sources=None):
    wall, cpu = perf_counter_ns(), process_time_ns()
    with suppress_decision_logging():
        replay = replay_case(
            case, as_of, settings, timing=True, produce=proposals, prepared_sources=prepared_sources
        )
    return {
        **replay,
        **_audit(replay),
        "local_replay_wall_ms": round((perf_counter_ns() - wall) / 1e6, 3),
        "local_replay_cpu_ms": round((process_time_ns() - cpu) / 1e6, 3),
    }


def _recorded_comparison(case, as_of, settings) -> dict:
    saved_replies = sum(len(source.get("responses", [])) for source in case["sources"])
    if not saved_replies:
        return {"status": "unavailable", "reason": "no_recorded_model_2_replies"}
    if any("markdown" in source for source in case["sources"]):
        return {"status": "unavailable", "reason": "original_page_missing_for_one_or_more_sources"}
    try:
        with suppress_decision_logging():
            baseline = replay_case(case, as_of, settings)
    except RecordedChunkMismatch:
        return {"status": "unavailable", "reason": "recorded_chunk_hash_mismatch"}
    replayed_replies = sum(batch["model"] == "fixture:recorded-reply" for batch in baseline["batches"])
    if any(baseline["unrecorded_chunks"].values()) or replayed_replies != saved_replies:
        return {"status": "unavailable", "reason": "incomplete_matched_model_2_replies"}
    return {"status": "matched", "recorded_replay": baseline | _audit(baseline)}


def evaluate_retained(manifest_bytes: bytes, *, measure_memory: bool = False) -> dict:
    """Score operator-attested local webpage captures and optional saved replies."""
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise ValueError("Retained evidence exceeds the local manifest size limit")
    manifest = _Manifest.model_validate_json(manifest_bytes)
    settings = Settings.model_construct()
    cases = []
    for reviewed in manifest.cases:
        case = reviewed.model_dump(mode="json", exclude_none=True)
        case["expected"] = _review_dict(reviewed.expected)
        candidate = _timed_replay(case, manifest.as_of, settings)
        candidate["comparison"] = _recorded_comparison(case, manifest.as_of, settings)
        cases.append(candidate)
    memory = None
    if measure_memory and not tracemalloc.is_tracing():
        tracemalloc.start()
        try:
            evaluate_retained(manifest_bytes)
            memory = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    return {
        "kind": "operator_attested_retained_webpage",
        "as_of": manifest.as_of.isoformat(),
        "manifest_sha256": sha256(manifest_bytes).hexdigest(),
        "live_model_calls": 0,
        "live_api_spend": 0,
        "peak_traced_python_bytes_in_separate_replay": memory,
        "scores": {
            field: _aggregate(case["scores"][field] for case in cases)
            for field in (*ProfileField, "qualifications", "employment")
        },
        "cases": cases,
    }


def compare_shadow(
    shadow: ExtractionShadow,
    *,
    settings: Settings,
    primary: ResearchResult | None = None,
    expected: ReviewedFacts | None = None,
    as_of: date | None = None,
) -> dict:
    """Evaluate frozen residual chunks after the primary run, entirely in memory."""
    snapshot = shadow.snapshot()
    if expected is None:
        expected = ReviewedFacts(fields=dict.fromkeys(ProfileField), qualifications=None, employment=None)
    prepared = {item.source.source_id: item for item in snapshot.sources}
    case = {
        "id": snapshot.person_id,
        "categories": ["captured_processed_evidence"],
        "label_basis": "Caller-supplied reviewed facts. Unreviewed fields are excluded.",
        "seed": snapshot.seed.model_dump(mode="json"),
        "expected": _review_dict(expected),
        "sources": [{"id": source_id} for source_id in prepared],
    }
    review_date = as_of or (primary.profile.completed_at.date() if primary else date.today())
    candidate = _timed_replay(case, review_date, settings, prepared_sources=prepared)
    comparison_report = {"status": "unavailable", "reason": "no_primary_result_supplied"}
    if primary is not None:
        active = {
            item.source_id
            for item in primary.sources
            if item.processing_status in {"extracted", "processed", "extraction_failed"}
        } | {claim.source_id for claim in primary.claims}
        by_id = {item.source_id: item for item in primary.sources}
        if (
            primary.profile.person_id == snapshot.person_id
            and active == prepared.keys()
            and not snapshot.omitted_source_ids
            and all(
                source_id in by_id
                and by_id[source_id].content_hash == item.source.content_hash
                and by_id[source_id].canonical_url == item.source.canonical_url
                for source_id, item in prepared.items()
            )
        ):
            comparison_report = _matched_primary(primary, expected)
        else:
            comparison_report = {
                "status": "unavailable",
                "reason": "incomplete_or_unmatched_primary_evidence",
            }
    return {
        "kind": "same_run_shadow",
        "as_of": review_date.isoformat(),
        "live_model_calls": 0,
        "live_api_spend": 0,
        "omitted_source_ids": snapshot.omitted_source_ids,
        "candidate": candidate,
        "comparison": comparison_report,
    }


def _matched_primary(primary: ResearchResult, expected: ReviewedFacts) -> dict:
    records = primary.profile.records or [primary.profile]

    def values(record, fields):
        return tuple(record.fields[field].researched_value for field in fields)

    selected = {
        field: sorted({record.fields[field].researched_value for record in records} - {None})
        for field in ProfileField
    }
    employment = values(primary.profile, EMPLOYMENT)
    education = [row for row in (values(record, EDUCATION) for record in records) if any(row)]
    scores = {field: comparison(selected[field], expected.fields[field]) for field in ProfileField}
    accounting = summarize_person(primary)
    supported = set(accounting["independently_supported_fields"])
    correct_slots = sum(
        field in supported and score is not None and bool(score["tp"]) and not score["fp"] and not score["fn"]
        for field, score in scores.items()
    )
    actual_cost = accounting["actual_api_cost"]
    return {
        "status": "matched",
        "actual_fields": selected,
        "scores": {
            **scores,
            "qualifications": comparison(education, expected.qualifications),
            "employment": comparison([employment] if any(employment) else [], expected.employment),
        },
        "provider_accounting": accounting,
        "actual_api_cost_per_fully_correct_supported_field": (
            round(actual_cost / correct_slots, 8) if actual_cost is not None and correct_slots else None
        ),
        "primary_profile": primary.profile.model_dump(mode="json", exclude={"metrics"}),
        "primary_claims": [claim.model_dump(mode="json") for claim in primary.claims],
    }


def write_report(path: Path, report: dict) -> None:
    """Only an explicit caller can create a new local report file."""
    encoded = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
        target.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path, help="Explicitly approved local manifest")
    parser.add_argument("--output", required=True, type=Path, help="New file in a private local directory")
    parser.add_argument("--measure-memory", action="store_true")
    args = parser.parse_args()
    try:
        with args.evidence.open("rb") as source:
            data = source.read(_MAX_MANIFEST_BYTES + 1)
        report = evaluate_retained(data, measure_memory=args.measure_memory)
        write_report(args.output, report)
    except FileExistsError:
        parser.error("The output file already exists. Choose a new local file.")
    except (ValidationError, ValueError, OSError):
        parser.error("The approved evidence manifest or private output path is invalid.")


if __name__ == "__main__":
    main()
