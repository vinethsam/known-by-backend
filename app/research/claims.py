"""Ground model claims in supplied text before they can affect a profile."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from app.prompts.extraction import EXTRACTION_PROMPT_VERSION
from app.research.normalisation import comparison_key, name_key, normalise
from app.schemas import EvidenceClaim, ExtractionResponse, PersonSeed, ProfileField, SourceRecord, utcnow


def literal_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def date_is_grounded(value: date, evidence: str) -> bool:
    if value > utcnow().date():
        return False
    forms = [value.isoformat()]
    for month in (value.strftime("%B"), value.strftime("%b")):
        forms.extend([f"{month} {value.day}, {value.year}", f"{value.day} {month} {value.year}"])
    return any(literal_key(form) in literal_key(evidence) for form in forms)


def validate_claims(
    response: ExtractionResponse, seed: PersonSeed, source: SourceRecord, chunk: str, model: str
) -> tuple[list[EvidenceClaim], list[str]]:
    claims, reasons = [], []
    content = literal_key(chunk)
    for claim in response.claims:
        if name_key(claim.subject_name) != name_key(seed.full_name):
            reasons.append("CLAIM_SUBJECT_MISMATCH")
            continue
        if literal_key(claim.evidence) not in content:
            reasons.append("UNGROUNDED_EVIDENCE")
            continue
        # Extraction values should be verbatim; normalisation is our responsibility.
        # A URL is additionally required to be an observed link, never model invention.
        if literal_key(claim.value) not in literal_key(claim.evidence):
            reasons.append("UNGROUNDED_VALUE")
            continue
        if claim.field == ProfileField.profile_link:
            from app.retrieval.urls import canonicalise_url

            try:
                known_links = {
                    canonicalise_url(u.rstrip(").,;")) for u in re.findall(r"https?://[^\s<>\[\]]+", chunk)
                }
                if canonicalise_url(claim.value) not in known_links:
                    raise ValueError("Unobserved link")
            except ValueError:
                reasons.append("UNOBSERVED_PROFILE_LINK")
                continue
        if any(
            value and not date_is_grounded(value, claim.evidence)
            for value in (claim.as_of_date, claim.end_date)
        ):
            reasons.append("UNGROUNDED_CLAIM_DATE")
            continue
        normalised, certainty = normalise(claim.field, claim.value)
        if not normalised:
            reasons.append("EMPTY_NORMALISED_VALUE")
            continue
        claims.append(
            EvidenceClaim(
                person_id=source.person_id,
                source_id=source.source_id,
                field=claim.field,
                raw_value=claim.value,
                normalised_value=normalised,
                normalisation_certainty=certainty,
                evidence_text=claim.evidence,
                evidence_location=claim.evidence_location,
                temporal_context=claim.temporal_context,
                as_of_date=claim.as_of_date,
                end_date=claim.end_date,
                is_current=claim.is_current,
                directness=claim.directness,
                source_claim_type=claim.source_claim_type,
                fact_group=claim.fact_group,
                subject_name=claim.subject_name,
                identity_relevance=source.identity.score,
                extraction_model=model,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
        )
    return deduplicate_claims(claims), sorted(set(reasons))


def deduplicate_claims(claims: list[EvidenceClaim]) -> list[EvidenceClaim]:
    seen = set()
    result = []
    for claim in claims:
        key = (
            claim.source_id,
            claim.field,
            comparison_key(claim.raw_value),
            claim.as_of_date,
            claim.end_date,
            claim.is_current,
            claim.fact_group,
        )
        if key not in seen:
            seen.add(key)
            result.append(claim)
    return result
