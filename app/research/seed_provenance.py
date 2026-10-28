"""Attach supplied-input provenance without manufacturing research evidence."""

from __future__ import annotations

from app.input_validation import sanitize_input_text
from app.research.normalisation import comparison_key, name_key, normalise_display
from app.retrieval.urls import source_policy_allows
from app.schemas import EvidenceClaim, FieldDecision, PersonSeed, PersonStatus, ProfileField, SourceRecord
from app.seed_context import seed_core_values, seed_input_attributes

SEED_REVIEW_REASONS = {"SUPPLIED_VALUE_CONFLICT", "SUPPLIED_VALUE_SUPERSEDED"}


def _field(label: str | None) -> str | None:
    return {
        "current_employer": "organisation",
        "government_office": "organisation",
        "alumni_organisation": "organisation",
        "organisation_affiliation": "organisation",
    }.get(label, label)


def _key(field: ProfileField, value: str) -> str:
    value = sanitize_input_text(value)
    if field == ProfileField.full_name:
        return name_key(value)
    return comparison_key(normalise_display(field, value))


def supplied_attributes(
    seed: PersonSeed, claims: list[EvidenceClaim], sources: dict[str, SourceRecord]
) -> list[dict]:
    """Keep raw values and independently grounded agreement/disagreement side by side."""
    attributes = seed_input_attributes(seed)
    for attribute in attributes:
        historical_input = attribute["field"] in {"historical_employer", "historical_job_title"}
        label = {"historical_employer": "organisation", "historical_job_title": "job_title"}.get(
            attribute["field"], _field(attribute["field"])
        )
        if label not in {field.value for field in ProfileField}:
            continue
        field = ProfileField(label)
        value = attribute["value"]
        if not value:
            continue
        corroborating = [
            claim
            for claim in claims
            if claim.field == field
            and _key(field, claim.raw_value) == _key(field, value)
            and (not historical_input or claim.is_current is False or claim.end_date is not None)
        ]
        # A different school or qualification is usually another credential, not
        # proof that a supplied education record is wrong.
        contradicted_sources = {
            source_id
            for source_id, source in sources.items()
            if label in source.identity.signals.get("contradicted_clues", [])
        }
        conflicts = [
            claim
            for claim in claims
            if claim.field == field
            and claim not in corroborating
            and (claim.source_id in contradicted_sources or field == ProfileField.country_of_origin)
        ]
        historical = field in {ProfileField.organisation, ProfileField.job_title} and any(
            claim.is_current is False or claim.end_date is not None for claim in corroborating
        )
        newer = historical and any(
            claim.field == field
            and claim.is_current is True
            and claim.end_date is None
            and _key(field, claim.raw_value) != _key(field, value)
            for claim in claims
        )
        attribute["provenance_status"] = (
            "conflicting"
            if conflicts
            else "superseded"
            if newer
            else "corroborated"
            if corroborating
            else "supplied"
        )
        attribute["corroborating_claim_ids"] = [claim.claim_id for claim in corroborating]
        attribute["conflicting_claim_ids"] = [claim.claim_id for claim in conflicts]
        attribute["source_urls"] = list(
            dict.fromkeys(
                sources[claim.source_id].canonical_url
                for claim in [*corroborating, *conflicts]
                if claim.source_id in sources
            )
        )
    return attributes


def attach_seed_provenance(
    fields: dict[ProfileField, FieldDecision],
    seed: PersonSeed,
    attributes: list[dict],
    web_identity: FieldDecision,
) -> None:
    """Project supplied values after scoring, keeping the research selection separate."""
    core = seed_core_values(seed)
    for field, decision in fields.items():
        if field != ProfileField.full_name and decision.provenance_status == "supplied":
            decision.value = decision.researched_value
    researched = {field: decision.value for field, decision in fields.items()}
    for field, decision in fields.items():
        supplied = [attribute for attribute in attributes if _field(attribute["field"]) == field.value]
        values = list(dict.fromkeys(attribute["value"] for attribute in supplied if attribute["value"]))
        if not values and core.get(field.value):
            values = [core[field.value]]
        matching_name = (
            bool(
                web_identity.value
                and decision.value
                and _key(field, web_identity.value) == _key(field, decision.value)
            )
            if field == ProfileField.full_name
            else False
        )
        decision.research_confidence = (
            (web_identity.confidence if matching_name else 0)
            if field == ProfileField.full_name
            else decision.confidence
        )
        decision.researched_value = (
            (web_identity.value if matching_name and web_identity.supporting_claim_ids else None)
            if field == ProfileField.full_name
            else decision.value
        )
        decision.provenance_status = "researched" if decision.value is not None else None
        if not values:
            continue
        decision.supplied_values = values
        decision.input_confidence = 100
        decision.provenance_status = "supplied"
        if (
            decision.value is not None
            and (field != ProfileField.full_name or (matching_name and web_identity.supporting_claim_ids))
            and any(_key(field, value) == _key(field, decision.value) for value in values)
        ):
            decision.provenance_status = "corroborated"
        states = {attribute["provenance_status"] for attribute in supplied}
        if "conflicting" in states:
            decision.provenance_status = "conflicting"
        elif "superseded" in states and decision.provenance_status != "corroborated":
            decision.provenance_status = "superseded"
        elif (
            decision.value is not None
            and decision.provenance_status == "supplied"
            and field in {ProfileField.organisation, ProfileField.job_title, ProfileField.country_of_origin}
        ):
            decision.provenance_status = "conflicting"
        elif (
            decision.value is not None
            and decision.provenance_status == "supplied"
            and field != ProfileField.full_name
        ):
            decision.provenance_status = "researched"
        if decision.value is None and values:
            # Do not attach a supplied role/degree fragment to a different researched
            # organisation or credential. The original values remain in provenance.
            context_fields = (
                (ProfileField.organisation, ProfileField.job_title)
                if field in {ProfileField.organisation, ProfileField.job_title}
                else (ProfileField.university_name, ProfileField.degree_type, ProfileField.subject)
                if field in {ProfileField.university_name, ProfileField.degree_type, ProfileField.subject}
                else ()
            )
            compatible = True
            blocked_profile_link = field == ProfileField.profile_link and not source_policy_allows(
                sanitize_input_text(str(values[0]))
            )
            for context_field in context_fields:
                selected = researched.get(context_field)
                supplied_context = [
                    attribute["value"]
                    for attribute in attributes
                    if _field(attribute["field"]) == context_field.value and attribute["value"]
                ]
                if selected and (
                    not supplied_context
                    or not any(
                        _key(context_field, selected) == _key(context_field, value)
                        for value in supplied_context
                    )
                ):
                    compatible = False
            if blocked_profile_link:
                decision.scoring_components["seed_projection_reason"] = "EXCLUDED_SOURCE_POLICY"
            elif compatible:
                decision.value = normalise_display(field, sanitize_input_text(str(values[0])))
                decision.confidence = 0
                decision.research_confidence = 0
                decision.provenance_status = "supplied"
                decision.scoring_components["confidence_kind"] = "seed_input_certainty"
                decision.scoring_components["supplied_temporal_status"] = "unspecified"
            else:
                decision.scoring_components["seed_projection_reason"] = "DIFFERENT_RESEARCH_CONTEXT"
        if decision.provenance_status in {"conflicting", "superseded"}:
            decision.review_required = True
            reason = (
                "SUPPLIED_VALUE_CONFLICT"
                if decision.provenance_status == "conflicting"
                else "SUPPLIED_VALUE_SUPERSEDED"
            )
            decision.review_reason_codes = list(dict.fromkeys([*decision.review_reason_codes, reason]))
        # Evidence IDs live in an additive audit component. They do not enter the
        # selected support set or the independent-source confidence calculation.
        decision.scoring_components["seed_corroborating_claim_ids"] = list(
            dict.fromkeys(
                claim_id
                for attribute in supplied
                for claim_id in attribute.get("corroborating_claim_ids", [])
            )
        )
        decision.scoring_components["seed_conflicting_claim_ids"] = list(
            dict.fromkeys(
                claim_id for attribute in supplied for claim_id in attribute.get("conflicting_claim_ids", [])
            )
        )


def supplied_field_decisions(seed: PersonSeed | dict) -> dict[ProfileField, FieldDecision]:
    """The same projection for tasks that failed before a research profile existed."""
    fields = {field: FieldDecision(review_required=False) for field in ProfileField}
    attach_seed_provenance(fields, seed, seed_input_attributes(seed), FieldDecision())
    return fields


def project_stored_seed(profile, seed):
    """Adapt stored decisions at presentation without rerunning or changing research."""
    profile = profile.model_copy(deep=True)
    attributes = profile.seed_attributes or seed_input_attributes(seed)
    for fields in [profile.fields, *(record.fields for record in profile.records)]:
        name = fields.get(ProfileField.full_name, FieldDecision()).model_copy(deep=True)
        name.confidence = name.research_confidence or 0
        if name.research_confidence and name.provenance_status == "corroborated":
            # Stored provenance already records independent agreement; no new claim
            # identifiers or evidence are fabricated by this presentation adapter.
            name.supporting_claim_ids = list(name.scoring_components.get("seed_corroborating_claim_ids", []))
        attach_seed_provenance(fields, seed, attributes, name)
    for record in profile.records:
        reasons = seed_review_reasons(record.fields)
        if reasons:
            record.review_required = True
            record.review_reason_codes = list(dict.fromkeys([*record.review_reason_codes, *reasons]))
            if record.status == PersonStatus.completed:
                record.status = PersonStatus.review_required
    if seed_review_reasons(profile.fields) or any(record.review_required for record in profile.records):
        profile.review_required = True
        if profile.status == PersonStatus.completed:
            profile.status = PersonStatus.review_required
    profile.seed_attributes = attributes
    return profile


def seed_review_reasons(fields: dict[ProfileField, FieldDecision]) -> list[str]:
    """Keep input disagreement visible without affecting independent evidence scores."""
    return sorted(
        {
            reason
            for decision in fields.values()
            for reason in decision.review_reason_codes
            if reason in SEED_REVIEW_REASONS
        }
    )
