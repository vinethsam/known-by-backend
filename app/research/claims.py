"""Ground model claims in supplied text before they can affect a profile."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import MutableMapping
from datetime import date

from app.prompts.extraction import EXTRACTION_PROMPT_VERSION
from app.research.identity import claim_is_eligible_for_source
from app.research.normalisation import (
    comparison_key,
    name_key,
    normalise_country_name,
    normalise_degree_candidates,
    normalise_value,
)
from app.retrieval.urls import source_policy_allows, source_record_allowed
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


def origin_is_explicit(value: str, evidence: str, subject_name: str) -> bool:
    """Require the named person's own origin statement in the quoted evidence."""

    country = re.escape(comparison_key(value))
    subject = re.escape(name_key(subject_name))
    quote = comparison_key(evidence)
    relation = (
        r"(?:country of origin(?: is)?|(?:is|was) (?:originally )?from|"
        r"(?:is|was) (?:a )?native of|(?:originally )?hails? from|"
        r"(?:originally )?comes? from|originates? from|originally from|(?:was )?born and raised in)"
    )
    direct = re.search(rf"\b{subject}(?:\s+s)?\s+{relation}\s+(?:the\s+)?{country}\b", quote)
    reverse = re.search(
        rf"\b{country}\s+(?:is|was)\s+{subject}\s+s\s+country of origin\b",
        quote,
    )
    if not direct and not reverse:
        return False
    if re.search(
        rf"\b(?:not|never)\s+(?:(?:originally\s+)?from|originates?\s+from|"
        rf"hails?\s+from|comes?\s+from)\s+{country}\b",
        quote,
    ):
        return False
    return True


def validate_claims(
    response: ExtractionResponse,
    seed: PersonSeed,
    source: SourceRecord,
    chunk: str,
    model: str,
    *,
    rejection_counts: MutableMapping[str, int] | None = None,
    adjustment_counts: MutableMapping[str, int] | None = None,
) -> tuple[list[EvidenceClaim], list[str]]:
    def rejected(reason: str) -> None:
        reasons.append(reason)
        if rejection_counts is not None:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    def adjusted(reason: str) -> None:
        reasons.append(reason)
        if adjustment_counts is not None:
            adjustment_counts[reason] = adjustment_counts.get(reason, 0) + 1

    if not source_record_allowed(source):
        if rejection_counts is not None:
            rejection_counts["SOURCE_POLICY_BLOCKED"] = len(response.claims)
        return [], ["SOURCE_POLICY_BLOCKED"]
    claims, reasons = [], []
    content = literal_key(chunk)
    for claim in response.claims:
        if name_key(claim.subject_name) != name_key(seed.full_name):
            rejected("CLAIM_SUBJECT_MISMATCH")
            continue
        if literal_key(claim.evidence) not in content:
            rejected("UNGROUNDED_EVIDENCE")
            continue
        # Extraction values should be verbatim; normalisation is our responsibility.
        # A URL is additionally required to be an observed link, never model invention.
        if literal_key(claim.value) not in literal_key(claim.evidence):
            rejected("UNGROUNDED_VALUE")
            continue
        if claim.field == ProfileField.country_of_origin:
            if normalise_country_name(claim.value) is None:
                rejected("UNRECOGNIZED_COUNTRY")
                continue
            if not origin_is_explicit(claim.value, claim.evidence, seed.full_name):
                rejected("ORIGIN_CONTEXT_UNSUPPORTED")
                continue
        if not claim_is_eligible_for_source(seed, source, claim.field, claim.value, claim.evidence):
            rejected("DISPUTED_CLAIM_IDENTITY_CONTEXT")
            continue
        if claim.field == ProfileField.profile_link:
            if not source_policy_allows(claim.value):
                rejected("SOURCE_POLICY_BLOCKED")
                continue
            from app.retrieval.urls import canonicalise_url

            try:
                known_links = {
                    canonicalise_url(u.rstrip(").,;")) for u in re.findall(r"https?://[^\s<>\[\]]+", chunk)
                }
                if canonicalise_url(claim.value) not in known_links:
                    raise ValueError("Unobserved link")
            except ValueError:
                rejected("UNOBSERVED_PROFILE_LINK")
                continue
        # A model-supplied date is metadata on an otherwise literal claim. Strip
        # only unsupported dates; do not discard a grounded role or credential.
        as_of_date = claim.as_of_date
        end_date = claim.end_date
        unsupported_date = False
        if as_of_date and not date_is_grounded(as_of_date, claim.evidence):
            as_of_date = None
            unsupported_date = True
        if end_date and not date_is_grounded(end_date, claim.evidence):
            end_date = None
            unsupported_date = True
        if unsupported_date:
            adjusted("UNGROUNDED_CLAIM_DATE")
        normalisations = (
            normalise_degree_candidates(claim.value)
            if claim.field == ProfileField.degree_type
            else [normalise_value(claim.field, claim.value)]
        )
        compound_degree = claim.field == ProfileField.degree_type and len(normalisations) > 1
        for normalisation in normalisations:
            if not normalisation.value:
                rejected("EMPTY_NORMALISED_VALUE")
                continue
            claims.append(
                EvidenceClaim(
                    person_id=source.person_id,
                    source_id=source.source_id,
                    field=claim.field,
                    raw_value=claim.value,
                    normalised_value=normalisation.value,
                    normalisation_certainty=normalisation.certainty,
                    evidence_text=claim.evidence,
                    evidence_location=claim.evidence_location,
                    temporal_context=claim.temporal_context,
                    as_of_date=as_of_date,
                    end_date=end_date,
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
            if claim.field == ProfileField.degree_type and normalisation.subject:
                subject = normalise_value(ProfileField.subject, normalisation.subject)
                claims.append(
                    EvidenceClaim(
                        person_id=source.person_id,
                        source_id=source.source_id,
                        field=ProfileField.subject,
                        raw_value=claim.value,
                        normalised_value=subject.value,
                        normalisation_certainty=min(normalisation.certainty, subject.certainty),
                        evidence_text=claim.evidence,
                        evidence_location=claim.evidence_location,
                        temporal_context=claim.temporal_context,
                        as_of_date=as_of_date,
                        end_date=end_date,
                        is_current=claim.is_current,
                        directness=claim.directness,
                        source_claim_type=(
                            f"derived_degree_subject:{comparison_key(normalisation.value)}"
                            if compound_degree
                            else "derived_degree_subject"
                        ),
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
            claim.normalised_value,
            claim.as_of_date,
            claim.end_date,
            claim.is_current,
            claim.fact_group,
        )
        if key not in seen:
            seen.add(key)
            result.append(claim)
    return result
