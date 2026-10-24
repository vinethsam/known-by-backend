"""Attach supplied-input provenance without manufacturing research evidence."""

from __future__ import annotations

from app.research.normalisation import comparison_key, name_key, normalise_display
from app.schemas import EvidenceClaim, FieldDecision, PersonSeed, ProfileField, SourceRecord
from app.seed_context import seed_core_values, seed_input_attributes


def _field(label: str | None) -> str | None:
    return {"current_employer": "organisation", "government_office": "organisation"}.get(label, label)


def _key(field: ProfileField, value: str) -> str:
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
    """Annotate decisions after scoring; seed values never fill researched cells."""
    core = seed_core_values(seed)
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
