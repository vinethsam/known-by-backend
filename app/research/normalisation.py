"""Central deterministic normalization while retaining literal values in claims."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.input_validation import repair_mojibake
from app.schemas import ProfileField, SourceType

BACHELORS_DEGREE = "Bachelor's Degree"
MASTERS_DEGREE = "Master's Degree"
DOCTORAL_DEGREE = "Doctoral Degree"


class EducationKind(StrEnum):
    academic_degree = "academic_degree"
    certification = "certification"
    executive_education = "executive_education"
    honorary_degree = "honorary_degree"
    postdoctoral = "postdoctoral"
    ongoing_study = "ongoing_study"
    training = "training"
    ambiguous = "ambiguous_education"


class RelationshipType(StrEnum):
    current_government_office = "current_government_office"
    current_primary_employment = "current_primary_employment"
    current_executive_role = "current_executive_role"
    current_academic_role = "current_academic_role"
    board_role = "board_role"
    advisory_role = "advisory_role"
    political_party_role = "political_party_role"
    historical_employment = "historical_employment"
    historical_public_office = "historical_public_office"
    other_affiliation = "other_affiliation"


RELATIONSHIP_PRIORITY = {
    RelationshipType.current_government_office: 90,
    RelationshipType.current_executive_role: 80,
    RelationshipType.current_primary_employment: 75,
    RelationshipType.current_academic_role: 65,
    RelationshipType.board_role: 40,
    RelationshipType.advisory_role: 30,
    RelationshipType.political_party_role: 20,
    RelationshipType.other_affiliation: 10,
    RelationshipType.historical_public_office: 5,
    RelationshipType.historical_employment: 4,
}


@dataclass(frozen=True, slots=True)
class NormalisationResult:
    value: str
    certainty: float
    reason_code: str | None = None
    education_kind: EducationKind | None = None
    qualification_label: str | None = None


_LOWERCASE_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "the", "to"}
_ACRONYMS = {
    "ai": "AI",
    "ba": "BA",
    "bba": "BBA",
    "beng": "BEng",
    "bsc": "BSc",
    "btech": "BTech",
    "ceo": "CEO",
    "cfo": "CFO",
    "cio": "CIO",
    "cto": "CTO",
    "hr": "HR",
    "it": "IT",
    "ma": "MA",
    "mba": "MBA",
    "meng": "MEng",
    "mpa": "MPA",
    "mph": "MPH",
    "msc": "MSc",
    "phd": "PhD",
    "pmp": "PMP",
    "stem": "STEM",
}


def comparison_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", repair_mojibake(value)).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


def name_key(value: str) -> str:
    key = comparison_key(value)
    return re.sub(r"^(dr|prof|professor|mr|mrs|ms)\s+", "", key)


def _accentless_key(value: str) -> str:
    key = comparison_key(value)
    return "".join(char for char in unicodedata.normalize("NFKD", key) if not unicodedata.combining(char))


def _clean_display(value: str) -> str:
    value = repair_mojibake(value).replace("\u00a0", " ")
    return " ".join(value.split()).strip(" ,")


def _title_case_token(token: str, *, first: bool) -> str:
    prefix = token[: len(token) - len(token.lstrip("([{\"'"))]
    suffix = token[len(token.rstrip(")]},.;:\"'")) :]
    core = token[len(prefix) : len(token) - len(suffix) if suffix else None]
    key = comparison_key(core)
    if not core:
        return token
    if key in _LOWERCASE_WORDS:
        styled = core[:1].upper() + core[1:].lower() if first else core.casefold()
    elif key in _ACRONYMS:
        styled = _ACRONYMS[key]
    elif core.isupper() and len(core) <= 5:
        styled = core
    else:
        styled = core[:1].upper() + core[1:].lower()
    return prefix + styled + suffix


def _conservative_title_case(value: str) -> str:
    letters = "".join(char for char in value if char.isalpha())
    if not letters or (not letters.islower() and not letters.isupper()):
        return value
    if letters.isupper() and " " not in value and comparison_key(value) not in _ACRONYMS:
        return value
    return " ".join(_title_case_token(token, first=index == 0) for index, token in enumerate(value.split()))


_DOCTORAL_PATTERNS = (
    r"\bph\s*d\b",
    r"\bd\s*phil\b",
    r"\bdoctor(?:ate|al degree| of philosophy|ado)\b",
    r"\bpromoviert(?:e|er|en|es)?\b",
)
_MASTERS_PATTERNS = (
    r"\bm\s*sc\b",
    r"\bm\s*s\b",
    r"\bm\s*b\s*a\b",
    r"\be\s*m\s*b\s*a\b",
    r"\bm\s*p\s*a\b",
    r"\bm\s*p\s*h\b",
    r"\bm\s*eng\b",
    r"\bm\s*res\b",
    r"\bm\s*f\s*a\b",
    r"\bm\s*phil\b",
    r"\bm\s*a\b",
    r"\bmaster(?: s| degree| 2| of\b|s\b|\b)",
    r"\bmaestria\b",
    r"\blaurea magistrale\b",
)
_BACHELORS_PATTERNS = (
    r"\bb\s*sc\b",
    r"\bb\s*a\b",
    r"\bb\s*eng\b",
    r"\bb\s*b\s*a\b",
    r"\bb\s*com\b",
    r"\bb\s*f\s*a\b",
    r"\bb\s*tech\b",
    r"\bbtech\b",
    r"\bbachelor(?: s| degree| of\b|s\b|\b)",
    r"\bundergraduate degree\b",
    r"\blicence\b",
    r"\blaurea\b",
)
_GENERIC_DEGREE_VALUES = {"degree", "college degree", "university degree", "diplome", "diplomee"}


def _degree_levels(value: str) -> list[str]:
    key = _accentless_key(value)
    matches: list[tuple[int, str]] = []
    for level, patterns in (
        (DOCTORAL_DEGREE, _DOCTORAL_PATTERNS),
        (MASTERS_DEGREE, _MASTERS_PATTERNS),
        (BACHELORS_DEGREE, _BACHELORS_PATTERNS),
    ):
        positions = [match.start() for pattern in patterns for match in re.finditer(pattern, key)]
        if positions:
            matches.append((min(positions), level))
    # "MA APP" is a qualification label seen in live data, while word boundaries
    # keep the abbreviation from matching words such as management.
    if re.match(r"^ma(?:\s+[a-z0-9]{2,})+$", key) and not any(
        level == MASTERS_DEGREE for _, level in matches
    ):
        matches.append((0, MASTERS_DEGREE))
    return [level for _, level in sorted(matches, key=lambda item: item[0])]


def _qualification_label(value: str) -> str | None:
    key = _accentless_key(value)
    if re.match(r"^ma(?:\s+[a-z0-9]{2,})+$", key):
        return _clean_display(value)
    labels = (
        (r"\bexecutive\s+mba\b", "Executive MBA"),
        (r"\be\s*m\s*b\s*a\b", "Executive MBA"),
        (r"\bm\s*b\s*a\b", "MBA"),
        (r"\bm\s*p\s*a\b", "MPA"),
        (r"\bm\s*sc\b", "MSc"),
        (r"\bm\s*a\b", "MA"),
        (r"\bb\s*tech\b|\bbtech\b", "BTech"),
        (r"\bb\s*sc\b", "BSc"),
        (r"\bph\s*d\b", "PhD"),
        (r"\bd\s*phil\b", "DPhil"),
    )
    for pattern, label in labels:
        if re.search(pattern, key):
            return label
    return None


def classify_education(value: str) -> EducationKind:
    key = _accentless_key(value)
    if re.search(r"\bhonor(?:ary|is causa)\b", key):
        return EducationKind.honorary_degree
    if re.search(r"\bpost\s*doctoral\b|\bpostdoc\b", key):
        return EducationKind.postdoctoral
    if re.search(r"\b(candidate|candidacy|pursuing|in progress|ongoing|currently studying|enrolled)\b", key):
        return EducationKind.ongoing_study
    if re.search(r"\b(certification|certificate|certified|pmp)\b", key):
        return EducationKind.certification
    if "executive mba" not in key and re.search(
        r"\bexecutive\b.*\b(programs?|programmes?|course|development|education)\b", key
    ):
        return EducationKind.executive_education
    if re.search(r"\b(training|workshop|bootcamp|short course)\b", key):
        return EducationKind.training
    if _degree_levels(value) or key in _GENERIC_DEGREE_VALUES:
        return EducationKind.academic_degree
    return EducationKind.ambiguous


def normalise_degree_candidates(value: str) -> list[NormalisationResult]:
    display = _clean_display(value)
    key = _accentless_key(display)
    kind = classify_education(display)
    levels = list(dict.fromkeys(_degree_levels(display)))
    if kind not in {EducationKind.academic_degree, EducationKind.ambiguous}:
        return [NormalisationResult(display, 1, kind.value.upper(), kind)]
    compound = len(levels) > 1 and bool(re.search(r"\b(and|y|et|e)\b|[,;/+&]", key))
    if compound:
        return [
            NormalisationResult(
                level,
                1,
                reason_code="COMPOUND_QUALIFICATION_SPLIT",
                education_kind=EducationKind.academic_degree,
            )
            for level in levels
        ]
    if levels:
        multilingual = bool(
            re.search(r"\b(maestria|licence|laurea|diplomee?|promoviert(?:e|er|en|es)?)\b", key)
        )
        return [
            NormalisationResult(
                levels[0],
                0.9 if key == "laurea" else 1,
                reason_code="MULTILINGUAL_DEGREE_EQUIVALENCE" if multilingual else None,
                education_kind=EducationKind.academic_degree,
                qualification_label=_qualification_label(display),
            )
        ]
    if key in _GENERIC_DEGREE_VALUES:
        return [
            NormalisationResult(
                BACHELORS_DEGREE,
                0.65,
                reason_code="GENERIC_DEGREE_DEFAULT",
                education_kind=EducationKind.academic_degree,
            )
        ]
    reason = "AMBIGUOUS_EDUCATION" if kind == EducationKind.ambiguous else kind.value.upper()
    return [NormalisationResult(display, 0.55 if kind == EducationKind.ambiguous else 1, reason, kind)]


def normalise_value(field: ProfileField, value: str) -> NormalisationResult:
    display = _clean_display(value)
    key = comparison_key(display)
    if field == ProfileField.degree_type:
        return normalise_degree_candidates(display)[0]
    if field == ProfileField.subject:
        aliases = {"computer sciences": "computer science", "economics science": "economics"}
        return NormalisationResult(aliases.get(key, key), 1)
    if field in {ProfileField.organisation, ProfileField.university_name}:
        key = re.sub(r"\s+(inc|incorporated|ltd|limited|llc|plc)$", "", key)
    if field == ProfileField.full_name:
        key = name_key(display)
    if field == ProfileField.profile_link:
        from app.retrieval.urls import canonicalise_url

        return NormalisationResult(canonicalise_url(display), 1)
    return NormalisationResult(key, 1)


def normalise_display(field: ProfileField, value: str) -> str:
    display = _clean_display(value)
    if field == ProfileField.degree_type:
        return normalise_value(field, display).value
    if field in {ProfileField.subject, ProfileField.job_title, ProfileField.university_name}:
        return _conservative_title_case(display)
    if field == ProfileField.organisation:
        return _conservative_title_case(display)
    return display


def is_non_organisation_place(value: str) -> bool:
    key = comparison_key(value)
    return bool(
        re.search(
            r"\b(official residence|(?:presidential|government|state|executive) "
            r"(?:palace|residence|mansion|house|building))\b",
            key,
        )
        or re.search(r"\b(prime minister|head of state) s (residence|building)\b", key)
    )


def classify_relationship(
    organisation: str | None,
    title: str | None,
    *,
    source_types: set[SourceType] | None = None,
) -> RelationshipType:
    organisation_key = comparison_key(organisation or "")
    title_key = comparison_key(title or "")
    combined = f"{organisation_key} {title_key}".strip()
    source_types = source_types or set()
    if re.search(r"\b(political party|party|movement|coalition)\b", organisation_key):
        return RelationshipType.political_party_role
    if re.search(
        r"\b(board member|board chair|chair of the board|non executive director|trustee)\b", combined
    ):
        return RelationshipType.board_role
    if re.search(r"\b(adviser|advisor|advisory|council member)\b", title_key):
        return RelationshipType.advisory_role
    government_body = bool(
        re.search(
            r"\b(ministry|parliament|cabinet|government|department|agency|legislature|office of the)\b",
            organisation_key,
        )
    )
    inherent_public_title = bool(
        re.search(
            r"\b(prime minister|cabinet minister|minister of|senator|member of parliament|"
            r"head of state|head of government)\b",
            title_key,
        )
    )
    conditional_public_title = bool(re.search(r"\b(president|governor|mayor|minister)\b", title_key))
    if (
        inherent_public_title
        or government_body
        or (SourceType.government in source_types and conditional_public_title)
    ):
        return RelationshipType.current_government_office
    if re.search(r"\b(professor|lecturer|dean|rector|academic|research fellow)\b", title_key):
        return RelationshipType.current_academic_role
    if re.search(
        r"\b(chief .+ officer|ceo|cfo|cio|cto|managing director|executive director|"
        r"founder|co founder|president)\b",
        title_key,
    ):
        return RelationshipType.current_executive_role
    if organisation_key and title_key:
        return RelationshipType.current_primary_employment
    return RelationshipType.other_affiliation


def normalise(field: ProfileField, value: str) -> tuple[str, float]:
    result = normalise_value(field, value)
    return result.value, result.certainty
