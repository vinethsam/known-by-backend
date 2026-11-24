"""Opt-in deterministic claim producer for prepared KnownBy evidence."""

from collections.abc import Iterable, Iterator, MutableMapping
from time import perf_counter_ns

from app.processing import PreparedContent
from app.retrieval.contracts import RetrievedPage
from app.retrieval.urls import source_record_allowed
from app.schemas import ExtractedClaim, ExtractionResponse, PersonSeed, SourceRecord

from .context import evidence_spans
from .education import extract_education
from .employment import extract_employment
from .identity_fields import extract_full_name, extract_profile_links, profile_metadata_evidence

VERSION = "deterministic:rules-v1"
_RESPONSE_LIMIT = 60  # ExtractionResponse schema limit


def _response(claims: list[ExtractedClaim]) -> ExtractionResponse:
    groups = {}
    for claim in claims:
        key = claim.fact_group or (claim.field, claim.value, claim.evidence)
        items = groups.setdefault(key, {})
        items.setdefault(
            (claim.field, claim.value, claim.as_of_date, claim.end_date, claim.is_current), claim
        )
    bounded = []
    for items in groups.values():
        if len(bounded) + len(items) <= _RESPONSE_LIMIT:
            bounded.extend(items.values())
    return ExtractionResponse(claims=bounded)


def extract_chunk(
    chunk: str,
    seed: PersonSeed,
    source: SourceRecord,
    *,
    timings: MutableMapping[str, float] | None = None,
) -> ExtractionResponse:
    """Return literal proposals to pass to validate_claims with the same chunk."""
    if source.identity.rejected or not source_record_allowed(source):
        return ExtractionResponse()

    def measure(key, operation, *args):
        if timings is None:
            return operation(*args)
        started = perf_counter_ns()
        result = operation(*args)
        timings[key] = timings.get(key, 0) + (perf_counter_ns() - started) / 1e6
        return result

    spans = measure("context_ms", evidence_spans, chunk, seed, source)
    names = measure("full_name_ms", extract_full_name, spans, seed, source)
    links = measure("profile_link_ms", extract_profile_links, chunk, spans, seed)
    employment = measure(
        "employment_ms", lambda: [claim for span in spans for claim in extract_employment(span, seed)]
    )
    education = measure(
        "education_ms", lambda: [claim for span in spans for claim in extract_education(span, seed)]
    )
    return measure("response_ms", _response, [*names, *links, *employment, *education])


def claim_batches(
    page: RetrievedPage,
    seed: PersonSeed,
    source: SourceRecord,
    content: PreparedContent,
    chunks: Iterable[str],
    *,
    timings: MutableMapping[str, float] | None = None,
) -> Iterator[tuple[str, ExtractionResponse, str]]:
    """Produce metadata and residual claim batches for the shared grounding boundary."""
    start = perf_counter_ns() if timings is not None else 0
    metadata = profile_metadata_evidence(page, seed, source, prepared_text=content.text)
    if timings is not None:
        elapsed = (perf_counter_ns() - start) / 1e6
        timings["profile_metadata_ms"] = timings.get("profile_metadata_ms", 0) + elapsed
        timings["extraction_ms"] = timings.get("extraction_ms", 0) + elapsed
    for evidence, response in metadata:
        yield evidence, response, VERSION
    for evidence in chunks:
        start = perf_counter_ns() if timings is not None else 0
        response = extract_chunk(evidence, seed, source, timings=timings)
        if timings is not None:
            timings["extraction_ms"] = timings.get("extraction_ms", 0) + (perf_counter_ns() - start) / 1e6
        yield evidence, response, VERSION


__all__ = ["VERSION", "claim_batches", "extract_chunk", "profile_metadata_evidence"]
