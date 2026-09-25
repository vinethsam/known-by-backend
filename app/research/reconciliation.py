"""Reconcile grounded evidence into deterministic, education-scoped records."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.config import ScoringPolicy
from app.research.confidence import (
    EDUCATION_FIELDS,
    VOLATILE_FIELDS,
    claim_strength,
    claim_tie_key,
    confidence_for_group,
    profile_scores,
)
from app.research.identity import effective_identity_scores
from app.research.normalisation import (
    BACHELORS_DEGREE,
    DOCTORAL_DEGREE,
    MASTERS_DEGREE,
    RELATIONSHIP_PRIORITY,
    EducationKind,
    RelationshipType,
    classify_education,
    classify_relationship,
    comparison_key,
    is_non_organisation_place,
    name_key,
    normalise_degree_candidates,
    normalise_display,
)
from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    PersonProfile,
    PersonSeed,
    PersonStatus,
    ProfileField,
    ProfileRecord,
    SourceRecord,
    SourceType,
)


@dataclass
class _FactBundle:
    source_id: str
    fact_group: str
    claims: list[EvidenceClaim] = field(default_factory=list)
    explicitly_grouped: bool = False


@dataclass
class _EvidenceCluster:
    bundles: list[_FactBundle] = field(default_factory=list)
    alternatives: list[EvidenceClaim] = field(default_factory=list)
    ambiguous: bool = False

    @property
    def claims(self) -> list[EvidenceClaim]:
        return [claim for bundle in self.bundles for claim in bundle.claims]


def _value_key(field_name: ProfileField, value: str) -> str:
    """Use conservative equivalence for grouping while preserving every raw value."""

    key = comparison_key(value)
    if field_name != ProfileField.university_name:
        return key
    tokens = [token for token in key.split() if token not in {"the", "of"}]
    institution_markers = {"university", "college", "institute", "academy", "school"}
    if institution_markers.intersection(tokens):
        return " ".join(sorted(tokens))
    return " ".join(tokens)


def _build_bundles(claims: list[EvidenceClaim], fields: set[ProfileField]) -> list[_FactBundle]:
    bundles: dict[tuple[str, str], _FactBundle] = {}
    for claim in claims:
        if claim.field not in fields:
            continue
        if claim.fact_group:
            group = claim.fact_group
        else:
            material = "|".join(
                (
                    claim.field.value,
                    claim.normalised_value,
                    claim.raw_value,
                    claim.as_of_date.isoformat() if claim.as_of_date else "",
                    claim.end_date.isoformat() if claim.end_date else "",
                    "" if claim.is_current is None else str(claim.is_current),
                    claim.evidence_location or "",
                    claim.evidence_text,
                )
            )
            group = f"__ungrouped__:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"
        key = (claim.source_id, group)
        bundle = bundles.setdefault(
            key,
            _FactBundle(
                source_id=claim.source_id,
                fact_group=group,
                explicitly_grouped=bool(claim.fact_group),
            ),
        )
        bundle.claims.append(claim)
    return list(bundles.values())


def _split_compound_degree_bundles(bundles: list[_FactBundle]) -> list[_FactBundle]:
    """Split one safely expanded raw degree claim while retaining its relationship context."""

    result = []
    for bundle in bundles:
        degree_claims = [claim for claim in bundle.claims if claim.field == ProfileField.degree_type]
        degree_values = {
            _value_key(ProfileField.degree_type, claim.normalised_value) for claim in degree_claims
        }
        raw_values = {comparison_key(claim.raw_value) for claim in degree_claims}
        if len(degree_values) <= 1 or len(raw_values) != 1:
            result.append(bundle)
            continue
        common = [claim for claim in bundle.claims if claim.field != ProfileField.degree_type]
        for degree_value in sorted(degree_values):
            matching = [
                claim
                for claim in degree_claims
                if _value_key(ProfileField.degree_type, claim.normalised_value) == degree_value
            ]
            result.append(
                _FactBundle(
                    source_id=bundle.source_id,
                    fact_group=f"{bundle.fact_group}:degree:{degree_value}",
                    claims=[*common, *matching],
                    explicitly_grouped=True,
                )
            )
    return result


def _values(claims: list[EvidenceClaim]) -> dict[ProfileField, set[str]]:
    values: dict[ProfileField, set[str]] = defaultdict(set)
    for claim in claims:
        values[claim.field].add(_value_key(claim.field, claim.normalised_value))
    return values


def _overlap_and_conflict(left: list[EvidenceClaim], right: list[EvidenceClaim]) -> tuple[int, int]:
    left_values, right_values = _values(left), _values(right)
    shared_fields = left_values.keys() & right_values.keys()
    overlap = sum(bool(left_values[field_name] & right_values[field_name]) for field_name in shared_fields)
    conflict = sum(
        bool(left_values[field_name].isdisjoint(right_values[field_name])) for field_name in shared_fields
    )
    return overlap, conflict


def _bundle_strength(
    bundle: _FactBundle,
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    today: date | None,
    identities: dict[str, float],
) -> float:
    return max(
        (
            claim_strength(
                claim,
                sources[claim.source_id],
                policy,
                today,
                effective_identities=identities,
            )[0]
            for claim in bundle.claims
        ),
        default=0,
    )


def _cluster_signature(cluster: _EvidenceCluster) -> str:
    parts = sorted(
        f"{claim.field.value}:{_value_key(claim.field, claim.normalised_value)}" for claim in cluster.claims
    )
    return "|".join(parts)


def _definitely_distinct_education(bundle: _FactBundle, cluster: _EvidenceCluster) -> bool:
    bundle_values, cluster_values = _values(bundle.claims), _values(cluster.claims)
    bundle_degrees = bundle_values.get(ProfileField.degree_type, set())
    cluster_degrees = cluster_values.get(ProfileField.degree_type, set())
    if bundle_degrees and cluster_degrees and bundle_degrees.isdisjoint(cluster_degrees):
        return True
    if bundle_degrees and bundle_degrees == cluster_degrees:
        contextual_conflicts = sum(
            bool(bundle_values.get(field_name) and cluster_values.get(field_name))
            and bundle_values[field_name].isdisjoint(cluster_values[field_name])
            for field_name in {ProfileField.university_name, ProfileField.subject}
        )
        # Two independent contextual disagreements distinguish same-level degrees;
        # one disagreement alone remains a reviewable source conflict.
        if contextual_conflicts >= 2:
            return True
    # A single source explicitly separating two fact groups is direct evidence of
    # distinct credentials, even when both happen to have the same degree level.
    for existing in cluster.bundles:
        if (
            bundle.source_id == existing.source_id
            and bundle.explicitly_grouped
            and existing.explicitly_grouped
            and bundle.fact_group != existing.fact_group
            and _overlap_and_conflict(bundle.claims, existing.claims)[1]
        ):
            return True
    return False


def _education_clusters(
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    today: date | None,
    identities: dict[str, float],
) -> tuple[list[_EvidenceCluster], list[EvidenceClaim]]:
    bundles = _split_compound_degree_bundles(_build_bundles(claims, EDUCATION_FIELDS))
    excluded: list[EvidenceClaim] = []
    regular = []
    non_degree_kinds = {
        EducationKind.certification,
        EducationKind.executive_education,
        EducationKind.honorary_degree,
        EducationKind.postdoctoral,
        EducationKind.ongoing_study,
        EducationKind.training,
    }
    for bundle in bundles:
        degree_claims = [claim for claim in bundle.claims if claim.field == ProfileField.degree_type]
        excluded_degrees = [
            claim for claim in degree_claims if classify_education(claim.raw_value) in non_degree_kinds
        ]
        if not excluded_degrees:
            regular.append(bundle)
            continue
        excluded.extend(excluded_degrees)
        retained_degrees = [claim for claim in degree_claims if claim not in excluded_degrees]
        if retained_degrees:
            regular.append(
                _FactBundle(
                    source_id=bundle.source_id,
                    fact_group=bundle.fact_group,
                    claims=[
                        claim
                        for claim in bundle.claims
                        if claim.field != ProfileField.degree_type or claim in retained_degrees
                    ],
                    explicitly_grouped=bundle.explicitly_grouped,
                )
            )
        else:
            excluded.extend(claim for claim in bundle.claims if claim.field != ProfileField.degree_type)
    bundles = regular
    bundles.sort(
        key=lambda bundle: (
            -_bundle_strength(bundle, sources, policy, today, identities),
            sources[bundle.source_id].canonical_url
            or sources[bundle.source_id].final_url
            or sources[bundle.source_id].requested_url,
            bundle.fact_group,
        )
    )
    clusters: list[_EvidenceCluster] = []
    for bundle in bundles:
        compatible: list[tuple[int, _EvidenceCluster]] = []
        for cluster in clusters:
            overlap, conflict = _overlap_and_conflict(bundle.claims, cluster.claims)
            if overlap and not conflict:
                compatible.append((overlap, cluster))
        if compatible:
            compatible.sort(key=lambda item: (-item[0], _cluster_signature(item[1])))
            compatible[0][1].bundles.append(bundle)
            continue
        if not clusters or all(_definitely_distinct_education(bundle, cluster) for cluster in clusters):
            clusters.append(_EvidenceCluster(bundles=[bundle]))
            continue

        candidates = [cluster for cluster in clusters if not _definitely_distinct_education(bundle, cluster)]
        candidates.sort(
            key=lambda cluster: (
                -_overlap_and_conflict(bundle.claims, cluster.claims)[0],
                _overlap_and_conflict(bundle.claims, cluster.claims)[1],
                _cluster_signature(cluster),
            )
        )
        target = candidates[0]
        overlap, _ = _overlap_and_conflict(bundle.claims, target.claims)
        target.ambiguous = True
        if overlap:
            # Same degree with incompatible details is one ambiguous record until
            # relationship evidence proves two separate qualifications.
            target.bundles.append(bundle)
        else:
            # Never splice unrelated, ungrouped education components together.
            target.alternatives.extend(bundle.claims)

    def cluster_order(cluster: _EvidenceCluster) -> tuple[float, str]:
        strength = max(
            (_bundle_strength(bundle, sources, policy, today, identities) for bundle in cluster.bundles),
            default=0,
        )
        return -strength, _cluster_signature(cluster)

    return sorted(clusters, key=cluster_order), _unique_claims(excluded)


def _employment_clusters(claims: list[EvidenceClaim]) -> list[_EvidenceCluster]:
    active = [
        claim
        for claim in claims
        if claim.field in VOLATILE_FIELDS and claim.is_current is not False and claim.end_date is None
    ]
    clusters: list[_EvidenceCluster] = []
    for bundle in _build_bundles(active, VOLATILE_FIELDS):
        compatible: list[tuple[int, _EvidenceCluster]] = []
        for cluster in clusters:
            overlap, conflict = _overlap_and_conflict(bundle.claims, cluster.claims)
            if overlap and not conflict:
                compatible.append((overlap, cluster))
        if compatible:
            compatible.sort(key=lambda item: (-item[0], _cluster_signature(item[1])))
            compatible[0][1].bundles.append(bundle)
        else:
            clusters.append(_EvidenceCluster(bundles=[bundle]))
    return clusters


def _relation_fields_by_claim(claims: list[EvidenceClaim]) -> dict[str, set[ProfileField]]:
    result: dict[str, set[ProfileField]] = {}
    for fields in (EDUCATION_FIELDS, VOLATILE_FIELDS):
        for bundle in _build_bundles(claims, fields):
            related = {claim.field for claim in bundle.claims}
            for claim in bundle.claims:
                result[claim.claim_id] = related
    return result


def _quality_for_claim(
    claim: EvidenceClaim,
    seed: PersonSeed,
    relation_fields: dict[str, set[ProfileField]],
) -> tuple[float, str | None]:
    related = relation_fields.get(claim.claim_id, {claim.field})
    if claim.field == ProfileField.full_name:
        return (1, None) if name_key(claim.raw_value) == name_key(seed.full_name) else (0.65, "NAME_VARIANT")
    if claim.field == ProfileField.job_title and ProfileField.organisation not in related:
        return 0.65, "UNPAIRED_FACT"
    if claim.field == ProfileField.subject and not related.intersection(
        {ProfileField.university_name, ProfileField.degree_type}
    ):
        return 0.65, "UNPAIRED_FACT"
    if claim.field == ProfileField.degree_type and claim.normalised_value not in {
        BACHELORS_DEGREE,
        MASTERS_DEGREE,
        DOCTORAL_DEGREE,
    }:
        return 0.7, "AMBIGUOUS_EDUCATION"
    if claim.field == ProfileField.university_name and len(related & EDUCATION_FIELDS) == 1:
        return 0.9, None
    if claim.field == ProfileField.degree_type and len(related & EDUCATION_FIELDS) == 1:
        return 0.85, None
    return 1, None


def _unique_claims(claims: list[EvidenceClaim]) -> list[EvidenceClaim]:
    return list({claim.claim_id: claim for claim in claims}.values())


def _decision(
    field_name: ProfileField,
    relevant: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    today: date | None,
    identities: dict[str, float],
    qualities: dict[str, float],
    quality_reasons: dict[str, str],
    *,
    conflicts: list[EvidenceClaim] | None = None,
    alternatives: list[EvidenceClaim] | None = None,
    reason_codes: list[str] | None = None,
) -> FieldDecision:
    conflicts = list(conflicts or [])
    alternatives = _unique_claims(list(alternatives or []))
    reason_codes = list(reason_codes or [])
    if not relevant:
        reasons = ["MISSING_FIELD", *reason_codes]
        return FieldDecision(
            conflicting_claim_ids=[claim.claim_id for claim in _unique_claims(conflicts)],
            alternative_claim_ids=[claim.claim_id for claim in alternatives],
            review_reason_codes=list(dict.fromkeys(reasons)),
            review_required=False,
        )

    groups: dict[str, list[EvidenceClaim]] = defaultdict(list)
    for claim in relevant:
        groups[_value_key(field_name, claim.normalised_value)].append(claim)
    group_scores = {
        value: confidence_for_group(
            support,
            [],
            sources,
            policy,
            today,
            effective_identities=identities,
            quality_by_claim=qualities,
        )[0]
        for value, support in groups.items()
    }
    chosen_value = max(group_scores, key=lambda value: (group_scores[value], value))
    support = list(groups[chosen_value])
    internal_conflicts = [
        claim for value, group in groups.items() if value != chosen_value for claim in group
    ]
    # A concurrent role may disagree about the title while corroborating its organisation.
    # Preserve that agreement without using the other role's mismatched components.
    matching_external = [
        claim for claim in conflicts if _value_key(field_name, claim.normalised_value) == chosen_value
    ]
    support = sorted(
        _unique_claims([*support, *matching_external]),
        key=lambda claim: claim_tie_key(claim, sources),
    )
    all_conflicts = sorted(
        _unique_claims(
            [
                *internal_conflicts,
                *(
                    claim
                    for claim in conflicts
                    if _value_key(field_name, claim.normalised_value) != chosen_value
                ),
            ]
        ),
        key=lambda claim: claim_tie_key(claim, sources),
    )
    confidence, components, reasons, selected = confidence_for_group(
        support,
        all_conflicts,
        sources,
        policy,
        today,
        effective_identities=identities,
        quality_by_claim=qualities,
    )
    selected_quality_reason = quality_reasons.get(selected.claim_id)
    if selected_quality_reason:
        reasons.append(selected_quality_reason)
    reasons.extend(reason_codes)
    reasons = list(dict.fromkeys(reasons))
    source_ids = list(dict.fromkeys(claim.source_id for claim in support))
    display = (
        selected.normalised_value
        if field_name == ProfileField.degree_type
        else normalise_display(
            field_name,
            selected.normalised_value if field_name == ProfileField.subject else selected.raw_value,
        )
    )
    mandatory_review = {
        "CURRENT_ROLE_CONFLICT",
        "EDUCATION_GROUPING_AMBIGUITY",
        "NAME_VARIANT",
        "UNPAIRED_FACT",
        "AMBIGUOUS_EDUCATION",
    }
    if field_name == ProfileField.degree_type:
        degree_details = next(
            (
                item
                for item in normalise_degree_candidates(selected.raw_value)
                if item.value == selected.normalised_value
            ),
            normalise_degree_candidates(selected.raw_value)[0],
        )
        components = dict(
            components,
            education_kind=degree_details.education_kind.value if degree_details.education_kind else None,
            qualification_label=degree_details.qualification_label,
            normalisation_reason=degree_details.reason_code,
        )
        if degree_details.reason_code:
            reasons = list(dict.fromkeys([*reasons, degree_details.reason_code]))
    return FieldDecision(
        value=display,
        confidence=confidence,
        selected_claim_id=selected.claim_id,
        supporting_claim_ids=[claim.claim_id for claim in support],
        supporting_source_ids=source_ids,
        sources=list(dict.fromkeys(sources[source_id].final_url for source_id in source_ids)),
        conflicting_claim_ids=[claim.claim_id for claim in all_conflicts],
        alternative_claim_ids=[claim.claim_id for claim in alternatives],
        review_required=bool(all_conflicts)
        or confidence < policy.review_threshold
        or bool(mandatory_review.intersection(reasons))
        or "IDENTITY_AMBIGUITY" in reasons,
        review_reason_codes=reasons,
        scoring_components=components,
    )


def _employment_decisions(
    claims: list[EvidenceClaim],
    seed: PersonSeed,
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    today: date | None,
    identities: dict[str, float],
    qualities: dict[str, float],
    quality_reasons: dict[str, str],
) -> dict[ProfileField, FieldDecision]:
    geography_keys = {
        comparison_key(value) for value in (seed.country, seed.location) if value and comparison_key(value)
    }

    def invalid_organisation(claim: EvidenceClaim) -> bool:
        if claim.field != ProfileField.organisation:
            return False
        if comparison_key(claim.raw_value) in geography_keys:
            return True
        if not is_non_organisation_place(claim.raw_value):
            return False
        paired_titles = [
            item.raw_value
            for item in claims
            if item.field == ProfileField.job_title
            and item.source_id == claim.source_id
            and item.fact_group == claim.fact_group
        ]
        return (
            classify_relationship(
                claim.raw_value,
                " ".join(paired_titles) or None,
                source_types={sources[claim.source_id].source_type},
            )
            == RelationshipType.current_government_office
        )

    invalid_organisations = [claim for claim in claims if invalid_organisation(claim)]
    invalid_ids = {claim.claim_id for claim in invalid_organisations}
    eligible_claims = [claim for claim in claims if claim.claim_id not in invalid_ids]
    historical = [
        claim
        for claim in eligible_claims
        if claim.field in VOLATILE_FIELDS and (claim.is_current is False or claim.end_date is not None)
    ]
    clusters = _employment_clusters(eligible_claims)
    if not clusters:
        return {
            field_name: _decision(
                field_name,
                [],
                sources,
                policy,
                today,
                identities,
                qualities,
                quality_reasons,
                alternatives=[
                    claim for claim in [*historical, *invalid_organisations] if claim.field == field_name
                ],
            )
            for field_name in sorted(VOLATILE_FIELDS, key=lambda value: value.value)
        }

    def relationship_type(cluster: _EvidenceCluster) -> RelationshipType:
        organisations = " ".join(
            claim.raw_value for claim in cluster.claims if claim.field == ProfileField.organisation
        )
        titles = " ".join(
            claim.raw_value for claim in cluster.claims if claim.field == ProfileField.job_title
        )
        return classify_relationship(
            organisations or None,
            titles or None,
            source_types={sources[claim.source_id].source_type for claim in cluster.claims},
        )

    def rank(cluster: _EvidenceCluster) -> tuple[int, int, int, int, float]:
        cluster_claims = cluster.claims
        explicitly_current = int(any(claim.is_current is True for claim in cluster_claims))
        observed = max(
            (
                claim.as_of_date or sources[claim.source_id].published_at or date.min
                for claim in cluster_claims
            ),
            default=date.min,
        )
        completeness = len({claim.field for claim in cluster_claims} & VOLATILE_FIELDS)
        relationship_priority = RELATIONSHIP_PRIORITY[relationship_type(cluster)]
        strength = max(
            claim_strength(
                claim,
                sources[claim.source_id],
                policy,
                today,
                effective_identities=identities,
                field_quality=qualities.get(claim.claim_id, 1),
            )[0]
            for claim in cluster_claims
        )
        return explicitly_current, observed.toordinal(), relationship_priority, completeness, strength

    clusters.sort(
        key=lambda cluster: tuple(-value for value in rank(cluster)) + (_cluster_signature(cluster),)
    )
    selected = clusters[0]
    selected_rank = rank(selected)
    unresolved: list[_EvidenceCluster] = []
    alternatives = list(clusters[1:])
    for cluster in clusters[1:]:
        candidate_rank = rank(cluster)
        same_current_state = candidate_rank[0] == selected_rank[0]
        same_observation = (
            candidate_rank[1] == selected_rank[1]
            and candidate_rank[2] == selected_rank[2]
            and abs(candidate_rank[4] - selected_rank[4]) <= 0.1
        )
        if same_current_state and same_observation:
            unresolved.append(cluster)

    decisions = {}
    for field_name in sorted(VOLATILE_FIELDS, key=lambda value: value.value):
        relevant = [claim for claim in selected.claims if claim.field == field_name]
        conflict_claims = [
            claim for cluster in unresolved for claim in cluster.claims if claim.field == field_name
        ]
        alternative_claims = [
            claim
            for cluster in alternatives
            if cluster not in unresolved
            for claim in cluster.claims
            if claim.field == field_name
        ]
        alternative_claims.extend(claim for claim in historical if claim.field == field_name)
        alternative_claims.extend(claim for claim in invalid_organisations if claim.field == field_name)
        reasons = ["CURRENT_ROLE_CONFLICT"] if conflict_claims else []
        if len({claim.field for claim in selected.claims} & VOLATILE_FIELDS) < len(VOLATILE_FIELDS):
            reasons.append("UNPAIRED_FACT")
        if invalid_organisations and field_name == ProfileField.organisation:
            reasons.append("INVALID_ORGANISATION_VALUE")
        decisions[field_name] = _decision(
            field_name,
            relevant,
            sources,
            policy,
            today,
            identities,
            qualities,
            quality_reasons,
            conflicts=conflict_claims,
            alternatives=alternative_claims,
            reason_codes=reasons,
        )
        decisions[field_name].scoring_components["relationship_type"] = relationship_type(selected).value
    return decisions


def _representative_link(
    fields: dict[ProfileField, FieldDecision],
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    today: date | None,
    identities: dict[str, float],
    qualities: dict[str, float],
) -> FieldDecision:
    claims_by_id = {claim.claim_id: claim for claim in claims}
    per_source_fields: dict[str, dict[ProfileField, tuple[float, EvidenceClaim]]] = defaultdict(dict)
    for field_name, decision in fields.items():
        if field_name == ProfileField.profile_link or decision.value is None:
            continue
        for claim_id in decision.supporting_claim_ids:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                continue
            strength = claim_strength(
                claim,
                sources[claim.source_id],
                policy,
                today,
                effective_identities=identities,
                field_quality=qualities.get(claim.claim_id, 1),
            )[0]
            if strength < policy.identity_minimum:
                continue
            existing = per_source_fields[claim.source_id].get(field_name)
            if (
                existing is None
                or strength > existing[0]
                or (
                    strength == existing[0]
                    and claim_tie_key(claim, sources) < claim_tie_key(existing[1], sources)
                )
            ):
                per_source_fields[claim.source_id][field_name] = (strength, claim)

    candidates = []
    for source_id, contributed in per_source_fields.items():
        source = sources[source_id]
        url = source.canonical_url or source.final_url or source.requested_url
        if not url:
            continue
        contribution_strength = sum(strength for strength, _ in contributed.values())
        directness = max(
            policy.directness.get(claim.directness, policy.directness["ambiguous"])
            for _, claim in contributed.values()
        )
        recency = max(
            (claim.as_of_date or source.published_at or date.min for _, claim in contributed.values()),
            default=date.min,
        ).toordinal()
        candidates.append(
            (
                source_id,
                url,
                len(contributed),
                contribution_strength,
                source.authority_score,
                directness,
                identities.get(source_id, source.identity.score),
                recency,
                contributed,
            )
        )
    if not candidates:
        return FieldDecision(review_reason_codes=["MISSING_FIELD"], review_required=False)

    # Quantity is meaningful only among reasonably reliable sources. If at least
    # one directory-or-better source contributed, aggregators/social/unknown pages
    # cannot become the representative merely by repeating more weak claims.
    reliable_authority = policy.authority[SourceType.directory]
    reliable = [
        candidate
        for candidate in candidates
        if candidate[4] >= reliable_authority and candidate[6] >= policy.identity_minimum
    ]
    weak_fallback = not reliable
    ranked = reliable or candidates
    ranked.sort(
        key=lambda item: (
            -item[2],
            -item[4],
            -item[5],
            -item[6],
            -item[7],
            -item[3],
            item[1],
        )
    )
    source_id, url, count, total, authority, directness, identity, recency, contributed = ranked[0]
    supporting = sorted(
        (claim for _, claim in contributed.values()),
        key=lambda claim: claim_tie_key(claim, sources),
    )
    confidence = round(100 * total / count, 2)
    reasons = []
    if confidence < policy.review_threshold:
        reasons.append("LOW_CONFIDENCE")
    if weak_fallback:
        reasons.append("LOW_SOURCE_AUTHORITY")
    return FieldDecision(
        value=url,
        confidence=confidence,
        selected_claim_id=min(
            supporting,
            key=lambda claim: (
                -claim_strength(
                    claim,
                    sources[claim.source_id],
                    policy,
                    today,
                    effective_identities=identities,
                    field_quality=qualities.get(claim.claim_id, 1),
                )[0],
                claim_tie_key(claim, sources),
            ),
        ).claim_id,
        supporting_claim_ids=[claim.claim_id for claim in supporting],
        supporting_source_ids=[source_id],
        sources=[url],
        review_required=bool(reasons),
        review_reason_codes=reasons,
        scoring_components={
            "selected_field_contributions": count,
            "contribution_strength": total,
            "authority": authority,
            "directness": directness,
            "identity": identity,
            "recency_ordinal": recency,
            "formula_version": "representative-source-v1",
        },
    )


def _record_review(
    fields: dict[ProfileField, FieldDecision], policy: ScoringPolicy
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    reasons_by_field = {
        field_name: set(decision.review_reason_codes) for field_name, decision in fields.items()
    }
    all_reasons = set().union(*reasons_by_field.values()) if reasons_by_field else set()
    if "IDENTITY_AMBIGUITY" in all_reasons:
        reasons.append("IDENTITY_AMBIGUITY")
    if "CURRENT_ROLE_CONFLICT" in all_reasons:
        reasons.append("CURRENT_ROLE_CONFLICT")
    if "EDUCATION_GROUPING_AMBIGUITY" in all_reasons:
        reasons.append("EDUCATION_GROUPING_AMBIGUITY")
    if any(
        "SOURCE_CONFLICT" in reasons_by_field.get(field_name, set())
        and fields[field_name].scoring_components.get("conflict_strength", 0) >= 0.5
        and fields[field_name].scoring_components.get("conflict_strength", 0)
        >= 0.75 * fields[field_name].scoring_components.get("claim_strength", 0)
        for field_name in {
            ProfileField.full_name,
            ProfileField.organisation,
            ProfileField.job_title,
            ProfileField.university_name,
            ProfileField.degree_type,
        }
    ):
        reasons.append("MAJOR_CONTRADICTORY_EVIDENCE")
    if any("UNPAIRED_FACT" in reasons_by_field.get(field_name, set()) for field_name in VOLATILE_FIELDS):
        reasons.append("CURRENT_ROLE_RELATIONSHIP_UNCERTAIN")
    link = fields[ProfileField.profile_link]
    substantive_values = any(
        decision.value is not None
        for field_name, decision in fields.items()
        if field_name != ProfileField.profile_link
    )
    if "LOW_SOURCE_AUTHORITY" in reasons_by_field[ProfileField.profile_link] or (
        substantive_values and link.value is None
    ):
        reasons.append("NO_RELIABLE_REPRESENTATIVE_SOURCE")
    critical_fields = {
        ProfileField.full_name,
        ProfileField.organisation,
        ProfileField.job_title,
        ProfileField.university_name,
        ProfileField.degree_type,
        ProfileField.profile_link,
    }
    low_critical = [
        field_name
        for field_name in critical_fields
        if fields[field_name].value is not None and fields[field_name].confidence < policy.review_threshold
    ]
    if len(low_critical) >= 2:
        reasons.append("MULTIPLE_CRITICAL_LOW_CONFIDENCE_FIELDS")
    reasons = list(dict.fromkeys(reasons))
    return bool(reasons), reasons


def _record_id(fields: dict[ProfileField, FieldDecision]) -> str:
    education = "|".join(
        f"{field_name.value}:{comparison_key(fields[field_name].value or '')}"
        for field_name in sorted(EDUCATION_FIELDS, key=lambda value: value.value)
        if fields[field_name].value
    )
    suffix = hashlib.sha256(education.encode("utf-8")).hexdigest()[:16] if education else "general"
    return "general" if suffix == "general" else f"education:{suffix}"


def reconcile(
    person_id: str,
    seed: PersonSeed,
    claims: list[EvidenceClaim],
    source_records: list[SourceRecord],
    policy: ScoringPolicy,
    today: date | None = None,
) -> PersonProfile:
    """Return a person profile whose legacy fields mirror its primary output record."""

    sources = {source.source_id: source for source in source_records}
    accepted = [
        claim
        for claim in claims
        if claim.source_id in sources
        and not sources[claim.source_id].identity.rejected
        and min(claim.identity_relevance, sources[claim.source_id].identity.score) >= policy.identity_minimum
    ]
    identities = effective_identity_scores(accepted, sources, policy)
    relation_fields = _relation_fields_by_claim(accepted)
    quality_pairs = {claim.claim_id: _quality_for_claim(claim, seed, relation_fields) for claim in accepted}
    qualities = {claim_id: quality for claim_id, (quality, _) in quality_pairs.items()}
    quality_reasons = {
        claim_id: reason for claim_id, (_, reason) in quality_pairs.items() if reason is not None
    }

    full_name = _decision(
        ProfileField.full_name,
        [claim for claim in accepted if claim.field == ProfileField.full_name],
        sources,
        policy,
        today,
        identities,
        qualities,
        quality_reasons,
    )
    employment = _employment_decisions(
        accepted,
        seed,
        sources,
        policy,
        today,
        identities,
        qualities,
        quality_reasons,
    )
    clusters, non_degree_claims = _education_clusters(accepted, sources, policy, today, identities)
    education_scopes: list[_EvidenceCluster | None] = clusters or [None]
    records: list[ProfileRecord] = []
    for scope_index, cluster in enumerate(education_scopes):
        fields = {
            ProfileField.full_name: full_name.model_copy(deep=True),
            ProfileField.organisation: employment[ProfileField.organisation].model_copy(deep=True),
            ProfileField.job_title: employment[ProfileField.job_title].model_copy(deep=True),
        }
        cluster_claims = cluster.claims if cluster else []
        cluster_alternatives = cluster.alternatives if cluster else []
        cluster_reasons = ["EDUCATION_GROUPING_AMBIGUITY"] if cluster and cluster.ambiguous else []
        for field_name in sorted(EDUCATION_FIELDS, key=lambda value: value.value):
            relevant = [claim for claim in cluster_claims if claim.field == field_name]
            alternatives = [claim for claim in cluster_alternatives if claim.field == field_name]
            if scope_index == 0:
                alternatives.extend(claim for claim in non_degree_claims if claim.field == field_name)
            reasons = list(cluster_reasons)
            if alternatives:
                if any(claim in non_degree_claims for claim in alternatives):
                    reasons.append("NON_DEGREE_EDUCATION")
                if any(claim in cluster_alternatives for claim in alternatives):
                    reasons.append("UNPAIRED_FACT")
            fields[field_name] = _decision(
                field_name,
                relevant,
                sources,
                policy,
                today,
                identities,
                qualities,
                quality_reasons,
                alternatives=alternatives,
                reason_codes=reasons,
            )
        fields[ProfileField.profile_link] = _representative_link(
            fields, accepted, sources, policy, today, identities, qualities
        )
        profile_confidence, coverage = profile_scores(fields, policy)
        review, record_reasons = _record_review(fields, policy)
        records.append(
            ProfileRecord(
                record_id=_record_id(fields),
                fields=fields,
                profile_confidence=profile_confidence,
                coverage=coverage,
                review_required=review,
                status=PersonStatus.review_required if review else PersonStatus.completed,
                sources_used=len(
                    {
                        source_id
                        for decision in fields.values()
                        for source_id in decision.supporting_source_ids
                    }
                ),
                review_reason_codes=record_reasons,
            )
        )

    primary = records[0]
    overall_review = any(record.review_required for record in records)
    legacy_fields = {
        field_name: decision.model_copy(deep=True) for field_name, decision in primary.fields.items()
    }
    # Preserve the old flat projection's visibility of unselected education evidence,
    # while record-scoped decisions remain isolated from other credentials.
    for field_name in sorted(EDUCATION_FIELDS, key=lambda value: value.value):
        legacy_fields[field_name].alternative_claim_ids = list(
            dict.fromkeys(
                [
                    *legacy_fields[field_name].alternative_claim_ids,
                    *(
                        claim_id
                        for record in records[1:]
                        for claim_id in (
                            record.fields[field_name].supporting_claim_ids
                            + record.fields[field_name].alternative_claim_ids
                        )
                    ),
                ]
            )
        )
    return PersonProfile(
        person_id=person_id,
        input_name=seed.full_name,
        fields=legacy_fields,
        profile_confidence=primary.profile_confidence,
        coverage=primary.coverage,
        review_required=overall_review,
        status=PersonStatus.review_required if overall_review else PersonStatus.completed,
        sources_considered=len(source_records),
        sources_used=len(
            {
                source_id
                for record in records
                for decision in record.fields.values()
                for source_id in decision.supporting_source_ids
            }
        ),
        records=records,
    )
