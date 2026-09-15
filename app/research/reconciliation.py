"""Select explainable flat decisions while preserving alternative raw facts."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from app.config import ScoringPolicy
from app.research.confidence import (
    EDUCATION_FIELDS,
    VOLATILE_FIELDS,
    claim_strength,
    confidence_for_group,
    profile_scores,
)
from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    PersonProfile,
    PersonSeed,
    PersonStatus,
    ProfileField,
    SourceRecord,
)


def reconcile(
    person_id: str,
    seed: PersonSeed,
    claims: list[EvidenceClaim],
    source_records: list[SourceRecord],
    policy: ScoringPolicy,
    today: date | None = None,
) -> PersonProfile:
    sources = {s.source_id: s for s in source_records}
    accepted = [
        c
        for c in claims
        if c.source_id in sources
        and not sources[c.source_id].identity.rejected
        and min(c.identity_relevance, sources[c.source_id].identity.score) >= policy.identity_minimum
    ]
    fields = {}
    # Select one explicitly grouped education/employment record, then keep its components
    # together. Unselected records remain alternatives; never splice two degrees into one.
    anchors: dict[str, tuple[str, str]] = {}
    for label, group_fields in (("education", EDUCATION_FIELDS), ("employment", VOLATILE_FIELDS)):
        grouped = [c for c in accepted if c.field in group_fields and c.fact_group]
        if label == "employment":
            grouped = [c for c in grouped if c.is_current is not False and c.end_date is None]
        if grouped:
            anchor = max(
                grouped,
                key=lambda c: (
                    claim_strength(c, sources[c.source_id], policy, today)[0],
                    c.as_of_date or date.min,
                ),
            )
            anchors[label] = (anchor.source_id, anchor.fact_group)
    for field in ProfileField:
        relevant = [c for c in accepted if c.field == field]
        alternatives = []
        if field in VOLATILE_FIELDS:
            alternatives = [c for c in relevant if c.is_current is False or c.end_date is not None]
            relevant = [c for c in relevant if c not in alternatives]
        if not relevant:
            fields[field] = FieldDecision(
                review_reason_codes=["MISSING_FIELD"],
                alternative_claim_ids=[c.claim_id for c in alternatives],
            )
            continue
        groups: dict[str, list[EvidenceClaim]] = defaultdict(list)
        for claim in relevant:
            groups[claim.normalised_value].append(claim)
        label = (
            "education" if field in EDUCATION_FIELDS else "employment" if field in VOLATILE_FIELDS else None
        )
        anchor = anchors.get(label)
        anchor_values = (
            {c.normalised_value for c in relevant if (c.source_id, c.fact_group) == anchor}
            if anchor
            else set()
        )
        selectable = {v: cs for v, cs in groups.items() if not anchor_values or v in anchor_values}
        # A component absent from the selected record stays missing rather than borrowing
        # an unrelated degree/employment component solely for a complete spreadsheet.
        if anchor and not anchor_values:
            fields[field] = FieldDecision(
                review_reason_codes=["MISSING_FIELD", "UNPAIRED_FACT"],
                alternative_claim_ids=[c.claim_id for c in relevant + alternatives],
            )
            continue
        scores = {v: confidence_for_group(cs, [], sources, policy, today)[0] for v, cs in selectable.items()}
        chosen_value = max(scores, key=lambda v: (scores[v], v))
        support = groups[chosen_value]
        other = [c for value, cs in groups.items() if value != chosen_value for c in cs]
        # Distinct degrees/institutions/subjects can coexist. Their alternatives are not
        # evidence that the selected value is false. Volatile roles may also be concurrent
        # when explicitly represented as different facts on the same source.
        if field in EDUCATION_FIELDS or field == ProfileField.profile_link:
            conflicts = []
            alternatives.extend(other)
        else:
            conflicts = []
            for claim in other:
                same_source_other_group = any(
                    c.source_id == claim.source_id
                    and c.fact_group
                    and claim.fact_group
                    and c.fact_group != claim.fact_group
                    for c in support
                )
                if field in VOLATILE_FIELDS and same_source_other_group:
                    alternatives.append(claim)
                else:
                    conflicts.append(claim)
        confidence, components, reasons, selected = confidence_for_group(
            support, conflicts, sources, policy, today
        )
        if alternatives and field in EDUCATION_FIELDS | VOLATILE_FIELDS:
            reasons.append("MULTIPLE_VALUES")
        source_ids = list(dict.fromkeys(c.source_id for c in support))
        display = selected.normalised_value if field == ProfileField.degree_type else selected.raw_value
        fields[field] = FieldDecision(
            value=display,
            confidence=confidence,
            selected_claim_id=selected.claim_id,
            supporting_claim_ids=[c.claim_id for c in support],
            supporting_source_ids=source_ids,
            sources=list(dict.fromkeys(sources[s].final_url for s in source_ids)),
            conflicting_claim_ids=[c.claim_id for c in conflicts],
            alternative_claim_ids=[c.claim_id for c in alternatives],
            review_required=bool(conflicts)
            or confidence < policy.review_threshold
            or (bool(alternatives) and field != ProfileField.profile_link)
            or "IDENTITY_AMBIGUITY" in reasons,
            review_reason_codes=reasons,
            scoring_components=components,
        )
    profile_confidence, coverage = profile_scores(fields, policy)
    review = any(d.review_required for d in fields.values())
    return PersonProfile(
        person_id=person_id,
        input_name=seed.full_name,
        fields=fields,
        profile_confidence=profile_confidence,
        coverage=coverage,
        review_required=review,
        status=PersonStatus.review_required if review else PersonStatus.completed,
        sources_considered=len(source_records),
        sources_used=len({s for d in fields.values() for s in d.supporting_source_ids}),
    )
