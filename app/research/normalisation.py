"""Conservative comparison keys, retaining every original value in its claim."""

from __future__ import annotations

import re
import unicodedata

from app.schemas import ProfileField


def comparison_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


def name_key(value: str) -> str:
    key = comparison_key(value)
    return re.sub(r"^(dr|prof|professor|mr|mrs|ms)\s+", "", key)


def normalise(field: ProfileField, value: str) -> tuple[str, float]:
    key = comparison_key(value)
    if field == ProfileField.degree_type:
        compact = re.sub(r"[\s.]", "", value.casefold())
        # Word/punctuation boundaries prevent BA matching unrelated words.
        if re.match(r"^(phd|dphil|doctorofphilosophy|doctorate)(?:\b|\(|$)", compact):
            return "Doctorate", 1
        if re.match(r"^(msc|ma|mba|mph|meng|mres|mfa|mphil)(?:\b|\(|$)", compact) or key.startswith(
            ("master of ", "masters ", "master s ")
        ):
            return "Master's", 1
        if re.match(r"^(bsc|ba|beng|bba|bcom|bfa)(?:\b|\(|$)", compact) or key.startswith(
            ("bachelor of ", "bachelors ", "bachelor s ")
        ):
            return "Bachelor's", 1
        if key in {"bachelor", "master"}:
            return ("Bachelor's" if key == "bachelor" else "Master's"), 0.9
        return key, 0.75
    if field == ProfileField.subject:
        aliases = {"computer sciences": "computer science", "economics science": "economics"}
        return aliases.get(key, key), 1
    if field in {ProfileField.organisation, ProfileField.university_name}:
        key = re.sub(r"\s+(inc|incorporated|ltd|limited|llc|plc)$", "", key)
    if field == ProfileField.full_name:
        key = name_key(value)
    if field == ProfileField.profile_link:
        from app.retrieval.urls import canonicalise_url

        return canonicalise_url(value), 1
    return key, 1
