"""Authority never substitutes for identifying the person a page describes."""

from __future__ import annotations

from app.config import ScoringPolicy
from app.research.normalisation import comparison_key, name_key
from app.schemas import IdentityMatch, PersonSeed


def contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def assess_identity(
    seed: PersonSeed,
    text: str,
    policy: ScoringPolicy,
    subject_name: str | None = None,
    model_relevance: str | None = None,
) -> IdentityMatch:
    key = comparison_key(text)
    target = name_key(seed.full_name)
    exact = contains_phrase(key, target)
    subject_matches = subject_name is None or name_key(subject_name) == target
    signals = {"exact_name": exact, "subject_matches": subject_matches, "matched_clues": []}
    if not exact or not subject_matches:
        return IdentityMatch(rejected=True, signals=signals, reason_codes=["IDENTITY_MISMATCH"])
    clues = {
        k: getattr(seed, k)
        for k in ("organisation", "country", "location", "university_name", "subject", "program_year")
    }
    clues.update(seed.known_attributes)
    matched_values = set()
    for label, value in clues.items():
        if value and name_key(value) != target and contains_phrase(key, comparison_key(value)):
            matched_values.add(comparison_key(value))
            signals["matched_clues"].append(label)
    score = min(1, policy.identity_name_only + len(matched_values) * policy.identity_clue_bonus)
    # LLM judgement may constrain a match; it cannot raise same-name evidence to certainty.
    if model_relevance == "unrelated":
        return IdentityMatch(rejected=True, signals=signals, reason_codes=["IDENTITY_MISMATCH"])
    if model_relevance == "ambiguous":
        score = min(score, policy.identity_review_threshold)
    ambiguous = model_relevance == "ambiguous" or score < policy.identity_review_threshold
    return IdentityMatch(
        score=score,
        ambiguous=ambiguous,
        signals=signals,
        reason_codes=["IDENTITY_AMBIGUITY"] if ambiguous else [],
    )
