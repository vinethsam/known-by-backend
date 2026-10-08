"""Bounded internal decision logs; never serialize evidence or provider payloads."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from app.config import ScoringPolicy
from app.research.identity import eligible_identity_source_ids, seed_known_attributes
from app.retrieval.urls import source_record_allowed
from app.schemas import PersonSeed, ProfileField, ResearchResult, SourceRecord

logger = logging.getLogger(__name__)

_EDUCATION_FIELDS = (ProfileField.university_name, ProfileField.degree_type, ProfileField.subject)
_CONTEXT_FIELDS = ("organisation", "job_title", "country", "location", "university_name", "subject")
_CONTEXT_LABEL = re.compile(
    r"\b(?:organisation|organization|company|employer|alumni|office|ministry|agency|institution|"
    r"university|title|role|country|location)\b"
)
_SENSITIVE_KEY = re.compile(
    r"(?:prompt|response|secret|password|token|authorization|cookie|api_key|html|markdown|"
    r"raw_content|compacted_text|evidence_text|^evidence$|^messages$)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"<>]+", re.IGNORECASE)


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if not parts.hostname:
            return "[invalid URL]"
        # Query strings, fragments and userinfo can carry access credentials.
        return urlunsplit((parts.scheme, parts.hostname, parts.path, "", ""))
    except ValueError:
        return "[invalid URL]"


def _text(value: str, limit: int = 160) -> str:
    value = " ".join(_URL.sub(lambda match: _safe_url(match.group()), value).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _compact(value, *, depth: int = 0, budget: list[int] | None = None):
    """Keep deterministic JSON-safe primitives, with a shared size/node budget."""
    budget = budget if budget is not None else [3600, 100]
    if budget[0] <= 0 or budget[1] <= 0 or depth > 5:
        return "…"
    budget[1] -= 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _text(value, min(160, max(1, budget[0])))
        budget[0] -= len(text)
        return text
    if isinstance(value, Mapping):
        result = {}
        for original_key in sorted(value, key=str):
            key = str(original_key)
            if _SENSITIVE_KEY.search(key):
                continue
            if len(result) >= 32 or budget[0] <= 0 or budget[1] <= 0:
                result["omitted"] = True
                break
            safe_key = _text(key, 60)
            budget[0] -= len(safe_key)
            result[safe_key] = _compact(value[original_key], depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        result = [_compact(item, depth=depth + 1, budget=budget) for item in list(items)[:8]]
        if len(items) > 8:
            result.append(f"+{len(items) - 8} omitted")
        return result
    # Do not stringify arbitrary objects: repr/model dumps can contain evidence or secrets.
    return f"[{type(value).__name__} omitted]"


def _emit(level: int, kind: str, person_id: str, job_id: str | None, details: dict) -> None:
    if not logger.isEnabledFor(level):
        return
    data = _compact(details)
    # Railway text exports may omit structured columns. Keep correlation IDs in
    # the message payload too, especially when several people run concurrently.
    data["person_id"] = _text(person_id, 80)
    if job_id is not None:
        data["job_id"] = _text(job_id, 80)
    kind = re.sub(r"[^a-z0-9_]", "_", kind.casefold())[:40]
    extra = {"person_id": person_id, "pipeline_stage": "decision", "decision": data}
    if job_id is not None:
        extra["job_id"] = job_id
    logger.log(
        level,
        "decision_%s %s",
        kind,
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        extra=extra,
    )


def log_decision(kind: str, *, person_id: str, job_id: str | None = None, **details) -> None:
    """Emit DEBUG selection details supplied by existing deterministic scoring.

    Callers pass compact values/candidate scores/reason codes, never source text.
    Useful kinds are current_role, field_selection, identity, education and profile_link.
    This helper logs decisions; it must not calculate or alter ranking.
    """
    _emit(logging.DEBUG, kind, person_id, job_id, details)


def _seed_context(seed: PersonSeed) -> dict[str, str]:
    values = {field: getattr(seed, field) for field in _CONTEXT_FIELDS if getattr(seed, field)}
    for label, value in seed_known_attributes(seed).items():
        if _CONTEXT_LABEL.search(label.casefold().replace("_", " ")) and not _SENSITIVE_KEY.search(label):
            values.setdefault(label, value)
    # Reserve the INFO line's shared budget for every required summary field.
    # Input context may contain several long user-supplied names and values.
    return {_text(label, 48): _text(value, 64) for label, value in list(values.items())[:8]}


def log_result_decisions(
    seed: PersonSeed,
    result: ResearchResult,
    *,
    job_id: str,
    candidate_count: int,
    rejected_source_count: int,
    source_records: list[SourceRecord] | None = None,
    policy: ScoringPolicy | None = None,
) -> None:
    """Emit exactly one INFO final summary, plus bounded opt-in evidence diagnostics."""
    if not logger.isEnabledFor(logging.INFO):
        return
    profile = result.profile
    fields = profile.fields
    records = profile.records
    role = fields[ProfileField.job_title]
    organisation = fields[ProfileField.organisation]
    profile_link = fields[ProfileField.profile_link]
    policy = policy or ScoringPolicy()
    all_sources = source_records if source_records is not None else result.sources
    allowed_sources = {
        source.source_id: source
        for source in all_sources
        if source_record_allowed(source)
        and source.processing_status not in {"failed", "identity_rejected", "duplicate"}
    }
    selection_eligible = eligible_identity_source_ids(seed, result.claims, allowed_sources, policy)
    selection_rejected = {source.source_id for source in all_sources} - selection_eligible
    selected_claim_ids = {
        claim_id
        for record_fields in [fields, *(record.fields for record in records)]
        for decision in record_fields.values()
        for claim_id in [decision.selected_claim_id, *decision.supporting_claim_ids]
        if claim_id is not None
    }
    final_fields_selected = sum(
        decision.value is not None for field, decision in fields.items() if field != ProfileField.full_name
    )
    credential_count = sum(
        any(record.fields[field].value for field in _EDUCATION_FIELDS) for record in records
    )
    review_reasons = sorted(
        {reason for record in records for reason in record.review_reason_codes}
        | {
            reason
            for decision in fields.values()
            if decision.review_required
            for reason in decision.review_reason_codes
        }
    )
    _emit(
        logging.INFO,
        "summary",
        profile.person_id,
        job_id,
        {
            "seed_name": seed.full_name,
            "seed_context": _seed_context(seed),
            "candidate_count": candidate_count,
            "sources_attempted": len(all_sources),
            "sources_retrieved": sum(200 <= source.fetch_status < 300 for source in all_sources),
            "sources_failed": sum(
                source.processing_status in {"failed", "extraction_failed"} for source in all_sources
            ),
            "accepted_source_count": profile.metrics.sources_accepted,
            "rejected_source_count": rejected_source_count,
            "selection_eligible_source_count": len(selection_eligible),
            "selection_rejected_source_count": len(selection_rejected),
            "extraction_claim_count": len(result.claims),
            "claims_extracted": profile.metrics.claims_extracted,
            "claims_grounded": len(result.claims),
            "claims_rejected": profile.metrics.claims_rejected,
            "claims_selected": len(selected_claim_ids),
            "final_fields_selected": final_fields_selected,
            "selected_relationship": {"organisation": organisation.value, "job_title": role.value},
            "selected_confidence": {
                "organisation": organisation.confidence,
                "job_title": role.confidence,
                "profile": profile.profile_confidence,
            },
            "credential_count": credential_count,
            "profile_link": profile_link.value,
            "review_required": profile.review_required,
            "review_outcome": profile.research_status,
            "person_outcome": profile.research_status,
            "failure_reason": profile.metrics.failure_reason,
            "review_reasons": review_reasons,
            "stop_reason": profile.metrics.stop_reason,
        },
    )
    if not logger.isEnabledFor(logging.DEBUG):
        return
    claims_by_id = {claim.claim_id: claim for claim in result.claims}
    for field, decision in sorted(fields.items(), key=lambda item: item[0].value):
        alternatives = [
            {"value": claim.raw_value, "source_id": claim.source_id, "currentness": claim.is_current}
            for claim_id in decision.alternative_claim_ids[:3]
            if (claim := claims_by_id.get(claim_id)) is not None
        ]
        log_decision(
            "field_selection",
            person_id=profile.person_id,
            job_id=job_id,
            field=field.value,
            value=decision.value,
            confidence=decision.confidence,
            alternatives=alternatives,
            components=decision.scoring_components,
            review_required=decision.review_required,
            review_reasons=decision.review_reason_codes,
        )
    for source in all_sources[:20]:
        reasons = list(source.identity.reason_codes)
        source_claims = [claim for claim in result.claims if claim.source_id == source.source_id]
        if source.source_id in selection_rejected:
            if not source_record_allowed(source):
                reasons.append("SOURCE_POLICY_BLOCKED")
            elif source.processing_status in {"failed", "identity_rejected", "duplicate"}:
                reasons.append("SOURCE_UNUSABLE")
            elif source.identity.rejected or source.identity.score < policy.identity_minimum:
                reasons.append("LOW_IDENTITY_CONFIDENCE")
            else:
                reasons.append("IDENTITY_CONTEXT_NOT_CONNECTED")
        elif source.identity.signals.get("seed_anchor_required") and not source.identity.signals.get(
            "matched_seed_anchors"
        ):
            reasons.append("COHERENT_UNANCHORED_IDENTITY")
        if source.processing_status == "failed" and source.error_code == "RETRIEVAL_FAILED":
            reasons.append("SOURCE_RETRIEVAL_FAILED")
        if source.processing_status in {"extracted", "extraction_failed"} and not source_claims:
            reasons.append("NO_SELECTABLE_CLAIMS")
        log_decision(
            "identity",
            person_id=profile.person_id,
            job_id=job_id,
            source_id=source.source_id,
            url=source.canonical_url,
            processing_status=source.processing_status,
            identity_score=source.identity.score,
            rejected=source.source_id in selection_rejected,
            selection_eligible=source.source_id in selection_eligible,
            signals=source.identity.signals,
            reasons=sorted(set(reasons)),
            error_code=source.error_code,
        )
