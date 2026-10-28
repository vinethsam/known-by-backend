"""Authority never substitutes for identifying the person a page describes."""

from __future__ import annotations

import re
from collections import defaultdict

from app.config import ScoringPolicy
from app.research.normalisation import comparison_key, name_key
from app.schemas import (
    EvidenceClaim,
    IdentityMatch,
    PersonSeed,
    ProfileField,
    SourceCandidate,
    SourceRecord,
    SourceType,
)
from app.seed_context import is_research_attribute, safe_context_value, seed_context_values

_ROLE_ALIASES = {
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
    "coo": "chief operating officer",
    "cto": "chief technology officer",
}
_INSTITUTION_FIELDS = {"organisation", "university_name", "historical_employer"}
_CONTEXT_FIELDS = {
    ProfileField.organisation,
    ProfileField.job_title,
    ProfileField.university_name,
    ProfileField.degree_type,
    ProfileField.subject,
}


def _clue_field(label: str) -> str:
    field = label.partition(":")[0]
    # These fields describe an affiliation, not a claim of current employment.
    # They are nevertheless specific institutional identity anchors.
    return {
        "alumni_organisation": "organisation",
        "organisation_affiliation": "organisation",
        "government_office": "organisation",
    }.get(field, field)


def _clue_key(label: str, value: str) -> str:
    label = _clue_field(label)
    key = comparison_key(value)
    if label in _INSTITUTION_FIELDS:
        key = re.sub(r"\s+(inc|incorporated|ltd|limited|llc|plc|corporation|corp)$", "", key)
        key = re.sub(r"\s+newspaper$", "", key)
    if label == "job_title":
        for abbreviation, expanded in _ROLE_ALIASES.items():
            key = re.sub(rf"\b{abbreviation}\b", expanded, key)
    return key


def is_passive_seed_attribute(label: str) -> bool:
    """Return whether an attribute describes the input dataset, not the person."""

    return not is_research_attribute(label)


def seed_known_attributes(seed: PersonSeed) -> dict[str, str]:
    """Return person context only, excluding passive source-list metadata."""

    return {
        label: value
        for label, value in seed.known_attributes.items()
        if value and not is_passive_seed_attribute(label) and safe_context_value(value)
    }


def research_seed_payload(seed: PersonSeed, *, purpose: str | None = None, unresolved_fields=()) -> dict:
    """Send bounded research hypotheses, never the original metadata envelope."""

    context = seed_context_values(seed)
    if purpose == "education":
        fields = {"university_name", "degree_type", "subject", "country", "location"}
    elif purpose == "professional":
        fields = {
            "organisation",
            "job_title",
            "historical_employer",
            "historical_job_title",
            "industry",
            "country",
            "location",
        }
    else:
        fields = set(context)
    unresolved = set(unresolved_fields)
    if purpose == "discovery" and unresolved:
        if unresolved <= {"university_name", "degree_type", "subject", "profile_link", "full_name"}:
            fields &= {"organisation", "university_name", "degree_type", "subject", "country", "location"}
        elif unresolved <= {"organisation", "job_title", "profile_link", "full_name"}:
            fields &= {
                "organisation",
                "university_name",
                "job_title",
                "historical_employer",
                "historical_job_title",
                "industry",
                "country",
                "location",
            }
    payload = {"full_name": seed.full_name}
    legacy_context = {"alumni_organisation", "organisation_affiliation", "government_office"}
    for label, values in context.items():
        if label in fields and values and label not in legacy_context:
            payload[label] = values[0] if len(values) == 1 else values
    # Keep the legacy provider shape, but only for recognized, safe context.
    payload["known_attributes"] = {
        label: values[0]
        for label, values in context.items()
        if label in fields and label in legacy_context and values
    }
    return payload


def _seed_clues(seed: PersonSeed) -> dict[str, str]:
    clues = {}
    for label, values in seed_context_values(seed).items():
        if label == "organisation" and seed.organisation:
            values = [seed.organisation]
        if _clue_field(label) in _INSTITUTION_FIELDS:
            # A semicolon is an explicit list separator; commas and 'and' often
            # belong to one institution and must not be guessed apart.
            values = [part.strip() for value in values for part in value.split(";") if part.strip()]
        # An explicit current employer still leads over a conflicting alias.
        # Multiple alma maters or former employers may all be legitimate anchors.
        multiple = _clue_field(label) in _INSTITUTION_FIELDS
        for index, value in enumerate(values if multiple else values[:1]):
            clues[label if index == 0 else f"{label}:{index + 1}"] = value
    return clues


def seed_identity_anchors(seed: PersonSeed) -> dict[str, str]:
    """Return specific seed context that independent namesakes cannot establish alone.

    Country, location, subject and year remain supporting clues. A title is an anchor
    when no institutional clue was supplied; a generic shared title cannot substitute
    for an explicitly supplied employer or university.
    """
    clues = _seed_clues(seed)
    anchors = {}
    institution_keys = set()
    for label, value in clues.items():
        if _clue_field(label) not in _INSTITUTION_FIELDS:
            continue
        key = _clue_key(label, value)
        if key not in institution_keys:
            anchors[label] = value
            institution_keys.add(key)
    if not anchors and clues.get("job_title"):
        anchors["job_title"] = clues["job_title"]
    return anchors


def _matches_clue(label: str, value: str, text: str) -> bool:
    return contains_phrase(_clue_key(label, text), _clue_key(label, value))


def _phrase_starts(tokens: list[str], phrase: list[str]) -> list[int]:
    if not phrase or len(phrase) > len(tokens):
        return []
    return [
        index
        for index in range(len(tokens) - len(phrase) + 1)
        if tokens[index : index + len(phrase)] == phrase
    ]


def _clue_is_near_name(label: str, value: str, text: str, target: str) -> bool:
    """Require page-level clues to be locally associated with the seeded name."""

    clue_tokens = _clue_key(label, value).split()
    target_tokens = target.split()

    def close(context: str) -> bool:
        tokens = _clue_key(label, context).split()
        name_starts = _phrase_starts(tokens, target_tokens)
        clue_starts = _phrase_starts(tokens, clue_tokens)
        return any(abs(name - clue) <= 40 for name in name_starts for clue in clue_starts)

    # Narrative pages must associate the clue in the same sentence. Short adjacent
    # lines also cover common profile headers such as name / title / institution.
    if any(close(segment) for segment in re.split(r"[.!?;\r\n]+", text)):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not contains_phrase(_clue_key(label, line), target) or len(line.split()) > 12:
            continue
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < len(lines) and len(lines[neighbour].split()) <= 12:
                if close(f"{line} {lines[neighbour]}"):
                    return True
    return False


def _explicitly_negated_clue(label: str, value: str, text: str, target: str) -> bool:
    """Treat explicit denials as contradictions, never missing or historical context."""
    clue = re.escape(_clue_key(label, value))
    for sentence in re.split(r"[.!?\n]+", text):
        key = _clue_key(label, sentence)
        if not contains_phrase(key, target):
            continue
        relation = (
            r"(?:work(?:ed|s|ing)?|employ(?:ed|ee|ment)?|serve(?:d|s|ing)?|"
            r"affiliat(?:ed|ion)|associat(?:ed|ion)|member(?:ship)?|join(?:ed|s|ing)?|"
            r"lead(?:s|ing)?|led|attend(?:ed|s|ing)?|graduat(?:e|ed|es|ing)|enroll(?:ed|s|ing)?|"
            r"stud(?:y|ied|ies|ying)|earn(?:ed|s|ing)?|hold(?:s|ing)?|held)"
        )
        if re.search(
            rf"\b(?:not(?! only\b| just\b| merely\b)|never)\s+"
            rf"(?:\w+\s+){{0,3}}{relation}\b(?:\s+\w+){{0,4}}\s+{clue}\b",
            key,
        ) or re.search(
            rf"\bno\s+(?:known\s+)?(?:affiliation|association|connection|relationship|employment)"
            rf"\s+(?:with|to|at|for)\s+{clue}\b",
            key,
        ):
            return True
    return False


def contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def candidate_has_seed_identity_context(seed: PersonSeed, candidate: SourceCandidate) -> bool:
    """Allow a disputed candidate to be fetched when its own metadata has direct context.

    This is only a retrieval admission hint. The fetched body must independently
    establish identity before extraction or final selection.
    """

    target = name_key(seed.full_name)
    return any(
        _clue_is_near_name(label, value, f"{candidate.title}\n{candidate.snippet}", target)
        for label, value in seed_identity_anchors(seed).items()
        if _clue_field(label) in _INSTITUTION_FIELDS
    )


def claim_is_eligible_for_source(
    seed: PersonSeed,
    source: SourceRecord,
    field: ProfileField,
    value: str,
    evidence: str,
) -> bool:
    """Do not let one valid quote admit another person's claim on a mixed page."""

    if source.identity.signals.get("advisor_relevance") != "unrelated":
        return True
    target = name_key(seed.full_name)
    target_tokens = target.split()
    quote_tokens = comparison_key(evidence).split()
    # A disputed page can mention the seeded person and a partial namesake in
    # the same sentence. Do not use either mixed-name quote as identity evidence.
    if len(target_tokens) > 1 and any(
        quote_tokens[index - len(target_tokens) + 1 : index + 1] != target_tokens
        for index, token in enumerate(quote_tokens)
        if token == target_tokens[-1]
    ):
        return False
    clauses = re.split(
        r",\s*(?:and|but|while|whereas)\b|\b(?:but|while|whereas|meanwhile)\b",
        evidence,
        flags=re.IGNORECASE,
    )
    return any(_clue_is_near_name(field.value, value, clause, target) for clause in clauses)


def assess_identity(
    seed: PersonSeed,
    text: str,
    policy: ScoringPolicy,
    subject_name: str | None = None,
    model_relevance: str | None = None,
    source_type: SourceType | None = None,
) -> IdentityMatch:
    key = comparison_key(text)
    target = name_key(seed.full_name)
    exact = contains_phrase(key, target)
    opening = re.split(r"[.!?;\r\n]+", text.strip(), maxsplit=1)[0]
    named_biography_opening = len(opening.split()) <= 12 and contains_phrase(comparison_key(opening), target)
    subject_matches = subject_name is None or name_key(subject_name) == target
    anchors = seed_identity_anchors(seed)
    signals = {
        "exact_name": exact,
        "subject_matches": subject_matches,
        "matched_clues": [],
        "matched_seed_anchors": [],
        "unmatched_seed_anchors": list(anchors),
        "contradicted_clues": [],
        "seed_anchor_required": bool(anchors),
        "model_relevance": model_relevance,
    }
    if named_biography_opening:
        signals["named_biography_opening"] = True
    if not exact or not subject_matches:
        return IdentityMatch(rejected=True, signals=signals, reason_codes=["IDENTITY_MISMATCH"])
    clues = _seed_clues(seed)
    matched_values: dict[str, float] = {}
    for label, value in clues.items():
        if name_key(value) == target:
            continue
        if _explicitly_negated_clue(label, value, text, target):
            signals["contradicted_clues"].append(label)
        elif _clue_is_near_name(label, value, text, target):
            weight = 1.5 if _clue_field(label) in _INSTITUTION_FIELDS else 1.0
            clue_key = _clue_key(label, value)
            matched_values[clue_key] = max(weight, matched_values.get(clue_key, 0))
            signals["matched_clues"].append(label)
            if label in anchors:
                signals["matched_seed_anchors"].append(label)
                signals["unmatched_seed_anchors"].remove(label)
    score = min(1, policy.identity_name_only + sum(matched_values.values()) * policy.identity_clue_bonus)
    unanchored = bool(anchors) and not signals["matched_seed_anchors"]
    if unanchored:
        # Retain provisional evidence for a later seed-linked source; absence itself
        # is not a rejection, but country/year matches must not make it certain.
        score = min(score, policy.identity_name_only)
    # A retrieved official biography with direct name/institution agreement is
    # stronger than an advisor's pre-retrieval dismissal. Preserve disagreement as
    # ambiguity so the resulting fields remain reviewable.
    if model_relevance == "unrelated":
        official = source_type in {
            SourceType.first_party,
            SourceType.government,
            SourceType.employer,
            SourceType.university,
            SourceType.professional_body,
        }
        institutional_match = any(
            _clue_field(label) in _INSTITUTION_FIELDS for label in signals["matched_seed_anchors"]
        )
        if not (official and institutional_match and not signals["contradicted_clues"]):
            return IdentityMatch(rejected=True, signals=signals, reason_codes=["IDENTITY_MISMATCH"])
        signals["advisor_relevance"] = "unrelated"
        signals["model_relevance"] = "ambiguous"
        model_relevance = "ambiguous"
    if model_relevance == "ambiguous":
        score = min(score, policy.identity_review_threshold)
    seed_contradiction = bool(signals["contradicted_clues"])
    if seed_contradiction:
        # A spreadsheet clue is a hypothesis. Preserve an explicit contradiction
        # for downstream corroboration instead of discarding potentially corrective
        # current evidence at page-ingestion time.
        score = min(score, policy.identity_name_only)
    ambiguous = (
        seed_contradiction
        or unanchored
        or model_relevance == "ambiguous"
        or score < policy.identity_review_threshold
    )
    reasons = ["IDENTITY_AMBIGUITY"] if ambiguous else []
    if seed_contradiction:
        reasons.append("SEED_IDENTITY_CONTRADICTION")
    if unanchored:
        reasons.append("SEED_IDENTITY_UNCONFIRMED")
    return IdentityMatch(
        score=score,
        ambiguous=ambiguous,
        signals=signals,
        reason_codes=reasons,
    )


def eligible_identity_source_ids(
    seed: PersonSeed,
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
) -> set[str]:
    """Gate selected facts on seed agreement without discarding provisional evidence.

    A source may omit the seed clue when it shares an explicit institution, or two
    non-degree context fields, with an independent directly anchored source. Bridges
    are one hop: an unrelated cluster cannot manufacture a seed match by repetition.
    """
    eligible = {
        source_id
        for source_id, source in sources.items()
        if not source.identity.rejected and source.identity.score >= policy.identity_minimum
    }
    claims = [
        claim
        for claim in claims
        if claim.source_id in sources
        and claim_is_eligible_for_source(
            seed, sources[claim.source_id], claim.field, claim.raw_value, claim.evidence_text
        )
    ]
    anchors = seed_identity_anchors(seed)
    if not anchors:
        return _unseeded_identity_source_ids(claims, sources, policy, eligible)

    disputed_institutions = {
        source_id: {
            label
            for label in source.identity.signals.get("matched_seed_anchors", [])
            if _clue_field(label) in _INSTITUTION_FIELDS
        }
        for source_id, source in sources.items()
        if source_id in eligible and source.identity.signals.get("advisor_relevance") == "unrelated"
    }
    # Candidate and page metadata justified checking these disputed sources, but
    # cannot turn a related-search/footer mention into evidence about their claims.
    locally_anchored_disputed = {
        claim.source_id
        for claim in claims
        if claim.source_id in disputed_institutions
        and any(
            _clue_is_near_name(label, anchors[label], claim.evidence_text, name_key(seed.full_name))
            for label in disputed_institutions[claim.source_id]
        )
    }

    def disputed_source_has_anchored_claim(source_id: str) -> bool:
        return source_id not in disputed_institutions or source_id in locally_anchored_disputed

    claim_source_ids = {claim.source_id for claim in claims}
    direct = {
        source_id
        for source_id in eligible
        if source_id in claim_source_ids
        and set(sources[source_id].identity.signals.get("matched_seed_anchors", [])) & set(anchors)
        and disputed_source_has_anchored_claim(source_id)
    }
    corrective_groups: dict[tuple[str, str], set[ProfileField]] = defaultdict(set)
    current_organisations: set[str] = set()
    for claim in claims:
        source = sources.get(claim.source_id)
        if (
            source is None
            or claim.source_id not in eligible
            or not source.identity.signals.get("contradicted_clues")
            or not source.identity.signals.get("exact_name")
            or not source.identity.signals.get("subject_matches")
            or source.authority_score < policy.authority[SourceType.publication]
            or claim.identity_relevance < policy.identity_minimum
            or claim.directness != "explicit"
            or claim.is_current is not True
            or claim.end_date is not None
            or claim.field not in {ProfileField.organisation, ProfileField.job_title}
        ):
            continue
        group = claim.fact_group or f"claim:{claim.claim_id}"
        corrective_groups[(claim.source_id, group)].add(claim.field)
        if claim.field == ProfileField.organisation:
            current_organisations.add(claim.source_id)
    primary_types = {
        SourceType.first_party,
        SourceType.government,
        SourceType.employer,
        SourceType.university,
        SourceType.professional_body,
    }
    direct.update(
        source_id
        for (source_id, _), fields in corrective_groups.items()
        if fields == {ProfileField.organisation, ProfileField.job_title}
        or (source_id in current_organisations and sources[source_id].source_type in primary_types)
    )
    keys: dict[str, set[tuple[ProfileField, str]]] = defaultdict(set)
    for claim in claims:
        if (
            claim.source_id not in eligible
            or claim.identity_relevance < policy.identity_minimum
            or claim.directness != "explicit"
            or claim.field not in _CONTEXT_FIELDS
            or not claim.normalised_value
        ):
            continue
        key = _clue_key(claim.field.value, claim.normalised_value)
        keys[claim.source_id].add((claim.field, key))
        if disputed_source_has_anchored_claim(claim.source_id) and any(
            (
                _clue_field(label) in _INSTITUTION_FIELDS
                and claim.field in {ProfileField.organisation, ProfileField.university_name}
                or _clue_field(label) == claim.field.value
            )
            and _matches_clue(label, value, claim.raw_value)
            for label, value in anchors.items()
        ):
            direct.add(claim.source_id)
    confirmed = set(direct)
    for source_id in eligible - direct:
        source = sources[source_id]
        for anchor_id in direct:
            anchor = sources[anchor_id]
            if (
                source.domain == anchor.domain
                or (source.content_hash and source.content_hash == anchor.content_hash)
                or min(source.authority_score, anchor.authority_score)
                < policy.authority[SourceType.directory]
            ):
                continue
            shared = keys[source_id] & keys[anchor_id]
            fields = {field for field, _ in shared} - {ProfileField.degree_type}
            if fields & {ProfileField.organisation, ProfileField.university_name} or len(fields) >= 2:
                confirmed.add(source_id)
                break
    if not confirmed:
        # Seed context is a hypothesis, not a permanent veto. When no page repeats
        # it, salvage exactly one independently corroborated institution cluster.
        # Disconnected same-name groups (the namesake case) remain ineligible.
        confirmed.update(_coherent_unanchored_source_ids(seed, claims, sources, policy, eligible))
    return confirmed


def _coherent_unanchored_source_ids(
    seed: PersonSeed,
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    eligible: set[str],
) -> set[str]:
    institution_keys: dict[str, set[str]] = defaultdict(set)
    minimum_authority = policy.authority[SourceType.directory]
    generic = {
        "agency",
        "college",
        "company",
        "government",
        "institution",
        "ministry",
        "office",
        "organisation",
        "organization",
        "school",
        "university",
    }
    for claim in claims:
        source = sources.get(claim.source_id)
        if (
            source is None
            or claim.source_id not in eligible
            or source.authority_score < minimum_authority
            or claim.identity_relevance < policy.identity_minimum
            or claim.directness != "explicit"
            or claim.field != ProfileField.organisation
            or claim.is_current is not True
            or claim.normalisation_certainty < 0.8
            # A full-name biography heading may introduce a following pronoun
            # sentence; a partial name elsewhere on a mixed page cannot do so.
            or not (
                _clue_is_near_name(
                    "organisation", claim.normalised_value, claim.evidence_text, name_key(seed.full_name)
                )
                or (
                    source.identity.signals.get("named_biography_opening")
                    and source.identity.signals.get("advisor_relevance") != "unrelated"
                    and re.match(r"^\s*(?:he|she|they)\b", claim.evidence_text, re.IGNORECASE)
                    and _matches_clue("organisation", claim.normalised_value, claim.evidence_text)
                )
            )
        ):
            continue
        key = _clue_key(claim.field.value, claim.normalised_value).removeprefix("the ")
        if key and key not in generic:
            institution_keys[claim.source_id].add(key)

    participants = set(institution_keys)
    if len(participants) < 2:
        return set()

    def independent(left_id: str, right_id: str) -> bool:
        left, right = sources[left_id], sources[right_id]
        return left.domain != right.domain and not (
            left.content_hash and right.content_hash and left.content_hash == right.content_hash
        )

    def connected(left_id: str, right_id: str) -> bool:
        return independent(left_id, right_id) and bool(institution_keys[left_id] & institution_keys[right_id])

    remaining = set(participants)
    corroborated_components: list[set[str]] = []
    while remaining:
        component = {min(remaining)}
        frontier = list(component)
        remaining -= component
        has_independent_edge = False
        while frontier:
            source_id = frontier.pop()
            adjacent = {other for other in remaining if connected(source_id, other)}
            if adjacent:
                has_independent_edge = True
            component.update(adjacent)
            remaining -= adjacent
            frontier.extend(sorted(adjacent))
        if len(component) >= 2 and has_independent_edge:
            corroborated_components.append(component)

    return corroborated_components[0] if len(corroborated_components) == 1 else set()


def _unseeded_identity_source_ids(
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    eligible: set[str],
) -> set[str]:
    """Do not assemble a name-only profile from disconnected namesakes.

    A shared explicit institution connects provisional sources. Names, degree
    levels and generic role/subject combinations cannot establish that connection.
    This is an eligibility check only: it never increases an identity score.
    """

    institutions: dict[str, set[str]] = defaultdict(set)
    substantive: set[str] = set()
    generic_institutions = {
        "agency",
        "college",
        "company",
        "government",
        "institution",
        "ministry",
        "office",
        "organisation",
        "organization",
        "school",
        "university",
    }
    for claim in claims:
        if (
            claim.source_id not in eligible
            or claim.identity_relevance < policy.identity_minimum
            or claim.field not in _CONTEXT_FIELDS
        ):
            continue
        substantive.add(claim.source_id)
        if (
            claim.directness != "explicit"
            or claim.field.value not in _INSTITUTION_FIELDS
            or claim.normalisation_certainty < 0.8
        ):
            continue
        key = _clue_key(claim.field.value, claim.normalised_value)
        if key and key.removeprefix("the ") not in generic_institutions:
            # An institution may be an employer on one page and an alma mater on
            # another; that is still a useful, externally grounded identity clue.
            institutions[claim.source_id].add(key)
    if len(substantive) <= 1:
        return eligible
    non_substantive = eligible - substantive
    secure = {
        source_id
        for source_id in substantive
        if sources[source_id].identity.score >= policy.identity_review_threshold
        and sources[source_id].identity.signals.get("model_relevance") != "ambiguous"
    }

    def compatible(left: str, right: str) -> bool:
        return bool(institutions[left] & institutions[right])

    if secure:
        # Keep the existing one-hop safeguard: an ambiguous intermediate page
        # cannot admit another disconnected page into an identified person's facts.
        return (
            non_substantive
            | secure
            | {
                source_id
                for source_id in substantive - secure
                if any(compatible(source_id, anchor_id) for anchor_id in secure)
            }
        )
    credible = {
        source_id
        for source_id in substantive
        if sources[source_id].authority_score >= policy.authority[SourceType.directory]
    }
    participants = credible or substantive
    remaining = set(participants)
    components: list[set[str]] = []
    while remaining:
        connected = {min(remaining)}
        pending = list(connected)
        remaining -= connected
        while pending:
            source_id = pending.pop()
            adjacent = {other for other in remaining if compatible(source_id, other)}
            connected.update(adjacent)
            remaining -= adjacent
            pending.extend(sorted(adjacent))
        components.append(connected)
    if len(components) != 1:
        # There is no evidence that identifies the intended namesake. Preserve
        # provisional claims outside final selection instead of guessing a cluster.
        return non_substantive
    connected = components[0]
    return (
        non_substantive
        | connected
        | {
            source_id
            for source_id in substantive - connected
            if any(compatible(source_id, anchor_id) for anchor_id in connected)
        }
    )


def effective_identity_scores(
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    seed: PersonSeed | None = None,
) -> dict[str, float]:
    """Return immutable, cross-source identity scores for reconciliation and discovery.

    Only grounded, explicit context repeated by a genuinely independent source can
    strengthen a name-only match. The source's stored identity assessment remains the
    baseline and is never mutated, so calling this function repeatedly is idempotent.
    """

    eligible = (
        eligible_identity_source_ids(seed, claims, sources, policy) if seed is not None else set(sources)
    )
    scores = {
        source_id: source.identity.score
        if source_id in eligible
        else min(source.identity.score, policy.identity_name_only)
        for source_id, source in sources.items()
    }
    minimum_context_authority = policy.authority[SourceType.directory]
    key_sources: dict[tuple[ProfileField, str], set[str]] = defaultdict(set)
    source_keys: dict[str, set[tuple[ProfileField, str]]] = defaultdict(set)

    for claim in claims:
        source = sources.get(claim.source_id)
        if (
            source is None
            or claim.source_id not in eligible
            or source.identity.rejected
            or source.identity.score < policy.identity_minimum
            or claim.identity_relevance < policy.identity_minimum
            or source.authority_score < minimum_context_authority
            or claim.directness != "explicit"
            or claim.field not in _CONTEXT_FIELDS
            or not claim.normalised_value
        ):
            continue
        key = (claim.field, claim.normalised_value)
        key_sources[key].add(claim.source_id)
        source_keys[claim.source_id].add(key)

    def independent(left_id: str, right_id: str) -> bool:
        left, right = sources[left_id], sources[right_id]
        left_hash = left.content_hash or left.source_id
        right_hash = right.content_hash or right.source_id
        return left.domain != right.domain and left_hash != right_hash

    def independent_peer_count(peer_ids: set[str]) -> int:
        """Count peers with a maximum distinct domain/content pairing."""

        domain_hashes: dict[str, set[str]] = defaultdict(set)
        for peer_id in peer_ids:
            peer = sources[peer_id]
            domain_hashes[peer.domain].add(peer.content_hash or peer.source_id)
        paired: dict[str, str] = {}

        def assign(domain: str, visited: set[str]) -> bool:
            for content_hash in sorted(domain_hashes[domain]):
                if content_hash in visited:
                    continue
                visited.add(content_hash)
                if content_hash not in paired or assign(paired[content_hash], visited):
                    paired[content_hash] = domain
                    return True
            return False

        for domain in sorted(domain_hashes):
            assign(domain, set())
        return len(paired)

    importance = {
        ProfileField.organisation: 1.0,
        ProfileField.university_name: 1.0,
        ProfileField.job_title: 0.65,
        ProfileField.subject: 0.65,
        # A degree level alone is common and therefore only a weak identity clue.
        ProfileField.degree_type: 0.25,
    }
    for source_id, keys in source_keys.items():
        source = sources[source_id]
        if source.identity.rejected:
            continue
        corroborated: list[tuple[ProfileField, str]] = []
        peers: set[str] = set()
        for key in keys:
            independent_peers = {
                other_id
                for other_id in key_sources[key]
                if other_id != source_id and independent(source_id, other_id)
            }
            if independent_peers:
                corroborated.append(key)
                peers.update(independent_peers)
        if not corroborated:
            continue
        context_bonus = sum(policy.identity_clue_bonus * importance[field] for field, _ in corroborated)
        context_bonus = min(2 * policy.identity_clue_bonus, context_bonus)
        peer_bonus = min(
            policy.corroboration_cap,
            policy.corroboration_step * independent_peer_count(peers),
        )
        strengthened = min(1.0, source.identity.score + context_bonus + peer_bonus)
        # An explicit model ambiguity may constrain a candidate but can never reject it.
        if source.identity.signals.get("model_relevance") == "ambiguous":
            strengthened = min(strengthened, policy.identity_review_threshold)
        scores[source_id] = strengthened
    return scores
