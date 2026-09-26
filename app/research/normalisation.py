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
MEDICAL_DEGREE = "Medical Degree"
LAW_DEGREE = "Law Degree"
DIPLOMA = "Diploma"
POSTGRADUATE_DIPLOMA = "Postgraduate Diploma"
POSTGRADUATE_DEGREE = "Postgraduate Degree"


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


class PublicOfficeType(StrEnum):
    head_of_state = "head_of_state"
    head_of_government = "head_of_government"
    ministerial = "ministerial"
    legislature = "legislature"
    government_department = "government_department"
    government_agency = "government_agency"
    executive_office = "executive_office"
    judiciary = "judiciary"
    local_government = "local_government"
    diplomatic_mission = "diplomatic_mission"
    public_institution = "public_institution"


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
    subject: str | None = None


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
_MEDICAL_PATTERNS = (
    r"\bdoctor of medicine\b",
    r"\bm\s*d\b",
    r"\bm\s*b\s*b\s*s\b",
    r"\bm\s*b\s*ch\s*b\b",
    r"\bmedical degree\b",
)
_LAW_PATTERNS = (
    r"\bjuris doctor\b",
    r"\bj\s*d\b",
    r"\bl\s*l\s*b\b",
    r"\bbachelor of laws?\b",
    r"\blaw degree\b",
)
_POSTGRADUATE_DIPLOMA_PATTERNS = (
    r"\bpost\s*graduate diploma\b",
    r"\bpg\s*dip\b",
)
_POSTGRADUATE_DEGREE_PATTERNS = (r"\b(?:advanced )?post\s*graduate degree\b",)
_DIPLOMA_PATTERNS = (r"\bdiploma\b",)
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
        (MEDICAL_DEGREE, _MEDICAL_PATTERNS),
        (LAW_DEGREE, _LAW_PATTERNS),
        (POSTGRADUATE_DIPLOMA, _POSTGRADUATE_DIPLOMA_PATTERNS),
        (POSTGRADUATE_DEGREE, _POSTGRADUATE_DEGREE_PATTERNS),
        (DOCTORAL_DEGREE, _DOCTORAL_PATTERNS),
        (MASTERS_DEGREE, _MASTERS_PATTERNS),
        (BACHELORS_DEGREE, _BACHELORS_PATTERNS),
        (DIPLOMA, _DIPLOMA_PATTERNS),
    ):
        if level == DIPLOMA and re.search(r"\bpost\s*graduate diploma\b", key):
            continue
        if level == BACHELORS_DEGREE and re.search(r"\bbachelor of laws?\b", key):
            # The professional-law mapping is the controlled output for this title;
            # do not also expand its embedded bachelor's wording as a second degree.
            non_law_bachelor = any(
                re.search(pattern, re.sub(r"\bbachelor of laws?\b", "", key))
                for pattern in _BACHELORS_PATTERNS
            )
            if not non_law_bachelor:
                continue
        if level == BACHELORS_DEGREE and re.search(
            r"\bbachelor of medicine(?:\s+and)?\s+bachelor of surgery\b", key
        ):
            # MBBS expands to a single professional medical qualification. Its
            # two embedded "Bachelor" words are not separate degree records.
            without_medical_title = re.sub(
                r"\bbachelor of medicine(?:\s+and)?\s+bachelor of surgery\b",
                "",
                key,
            )
            without_medical_title = re.sub(r"\bm\s*b\s*b\s*s\b", "", without_medical_title)
            if not any(re.search(pattern, without_medical_title) for pattern in _BACHELORS_PATTERNS):
                continue
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
        (r"\bdoctor of medicine\b|\bm\s*d\b", "MD"),
        (r"\bm\s*b\s*b\s*s\b", "MBBS"),
        (r"\bjuris doctor\b|\bj\s*d\b", "JD"),
        (r"\bl\s*l\s*b\b", "LLB"),
        (r"\bpg\s*dip\b", "PGDip"),
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


def _degree_subject(value: str, level: str) -> str | None:
    """Return a subject explicitly encoded in a qualification title."""

    display = _clean_display(value)
    if level == MEDICAL_DEGREE:
        return "medicine"
    if level == LAW_DEGREE:
        return "law"

    suffix = re.search(r"\s*\(([^()]*)\)\s*$", display)
    qualification_label = _qualification_label(display)
    if suffix and qualification_label:
        suffix_key = comparison_key(suffix.group(1)).replace(" ", "")
        label_key = comparison_key(qualification_label).replace(" ", "")
        if suffix_key == label_key:
            display = display[: suffix.start()].rstrip()

    def clean_subject(subject: str) -> str | None:
        subject = subject.strip(" .,:;")
        if len(subject) >= 2 and (subject[0], subject[-1]) in {("(", ")"), ("[", "]")}:
            subject = subject[1:-1].strip(" .,:;")
        return subject or None

    in_match = re.search(r"\b(?:degree\s+)?in\s+(.+)$", display, re.IGNORECASE)
    if in_match:
        return clean_subject(in_match.group(1))

    of_match = re.match(r"^(?:bachelor|master|doctor)\s+of\s+(.+)$", display, re.IGNORECASE)
    if of_match:
        subject = clean_subject(of_match.group(1))
        if subject and comparison_key(subject) not in {"arts", "science", "philosophy", "laws"}:
            return subject

    if level == BACHELORS_DEGREE:
        subject_degree = re.match(r"^(.+?)\s+degree$", display, re.IGNORECASE)
        if subject_degree:
            subject = clean_subject(subject_degree.group(1))
            if subject and comparison_key(subject) not in {"college", "university", "undergraduate"}:
                return subject
    return None


def _compound_degree_subjects(value: str) -> dict[str, str | None]:
    """Associate explicit subjects with their own qualification segment."""

    segments = re.split(
        r"\s*(?:,|;|/|\+|\|)\s*|\s+(?:and|&|y|e|et|und)\s+",
        _clean_display(value),
        flags=re.IGNORECASE,
    )
    recognized: list[tuple[str, list[str]]] = []
    for segment in segments:
        levels = _degree_levels(segment)
        if levels:
            recognized.append((segment, levels))
    if len(recognized) < 2:
        return {}
    subjects: dict[str, str | None] = {}
    for segment, levels in recognized:
        for level in levels:
            subjects.setdefault(level, _degree_subject(segment, level))
    return subjects


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
    if re.search(r"\b(training|workshop|bootcamp|short course|speciali[sz]ation course)\b", key):
        return EducationKind.training
    if _degree_levels(value) or key in _GENERIC_DEGREE_VALUES:
        return EducationKind.academic_degree
    if re.match(r"^.+\sdegree$", key) and not re.search(r"\b(two|several|multiple|various) degrees?\b", key):
        return EducationKind.academic_degree
    return EducationKind.ambiguous


def normalise_degree_candidates(value: str) -> list[NormalisationResult]:
    display = _clean_display(value)
    key = _accentless_key(display)
    kind = classify_education(display)
    levels = list(dict.fromkeys(_degree_levels(display)))
    if kind not in {EducationKind.academic_degree, EducationKind.ambiguous}:
        return [NormalisationResult(display, 1, kind.value.upper(), kind)]
    compound = len(levels) > 1 and bool(
        re.search(r"\b(and|y|et|e|und)\b", key) or re.search(r"[,;/+&|]", display)
    )
    if compound:
        subjects = _compound_degree_subjects(display)
        return [
            NormalisationResult(
                level,
                1,
                reason_code="COMPOUND_QUALIFICATION_SPLIT",
                education_kind=EducationKind.academic_degree,
                qualification_label=_qualification_label(display),
                subject=subjects.get(level),
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
                subject=_degree_subject(display, levels[0]),
            )
        ]
    if key in _GENERIC_DEGREE_VALUES:
        return [
            NormalisationResult(
                BACHELORS_DEGREE,
                0.65,
                reason_code="GENERIC_DEGREE_DEFAULT",
                education_kind=EducationKind.academic_degree,
                subject=_degree_subject(display, BACHELORS_DEGREE),
            )
        ]
    if kind == EducationKind.academic_degree and re.match(r"^.+\sdegree$", key):
        return [
            NormalisationResult(
                BACHELORS_DEGREE,
                0.65,
                reason_code="GENERIC_DEGREE_DEFAULT",
                education_kind=EducationKind.academic_degree,
                subject=_degree_subject(display, BACHELORS_DEGREE),
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


_PUBLIC_INSTITUTION_RE = re.compile(
    r"\b(ministry|parliament|legislature|national assembly|senate|congress|cabinet|"
    r"government|government department|government agency|public authority|"
    r"presidency|chancellery|court|judiciary|municipality|city council|"
    r"county government|embassy|consulate|high commission|diplomatic mission)\b"
)
_PRIVATE_INSTITUTION_RE = re.compile(
    r"\b(company|corporation|corp|limited|ltd|plc|llc|holdings|foundation|university|"
    r"college|school|bank|organisation|organization|association|committee|council|"
    r"federation|union|institute|society|group|network|club|team|talent agency)\b"
)


def classify_public_office(
    organisation: str | None,
    title: str | None,
    *,
    source_types: set[SourceType] | None = None,
) -> PublicOfficeType | None:
    organisation_key = comparison_key(organisation or "")
    title_key = comparison_key(title or "")
    source_types = source_types or set()
    government_source = SourceType.government in source_types
    public_reference_source = government_source or SourceType.encyclopedia in source_types
    organisation_letters = "".join(character for character in (organisation or "") if character.isalpha())
    abbreviated_institution = 2 <= len(organisation_letters) <= 10 and organisation_letters.isupper()
    private_institution = bool(_PRIVATE_INSTITUTION_RE.search(organisation_key)) or abbreviated_institution
    if re.search(r"\b(political party|party|movement|coalition)\b", organisation_key):
        return None
    if re.search(r"\b(court|judiciary)\b", organisation_key) or re.search(
        r"\b(chief justice|justice|judge|magistrate)\b", title_key
    ):
        return PublicOfficeType.judiciary
    if re.search(
        r"\b(parliament|legislature|national assembly|senate|congress)\b", organisation_key
    ) or re.search(r"\b(member of parliament|senator|legislator)\b", title_key):
        return PublicOfficeType.legislature
    if re.search(
        r"\b(embassy|consulate|high commission|diplomatic mission)\b", organisation_key
    ) or re.search(r"\b(ambassador|consul|high commissioner)\b", title_key):
        return PublicOfficeType.diplomatic_mission
    if re.search(
        r"\b(municipality|city council|county government|local government)\b", organisation_key
    ) or re.search(r"\b(mayor|councillor|councilor)\b", title_key):
        return PublicOfficeType.local_government
    if (
        re.search(r"\b(ministry|ministerial department)\b", organisation_key)
        or re.search(r"\b(cabinet minister|minister of|government minister)\b", title_key)
        or (government_source and re.search(r"\bminister\b", title_key))
    ):
        return PublicOfficeType.ministerial
    if re.search(r"\b(government agency|public agency|regulatory agency)\b", organisation_key) or (
        government_source and re.search(r"\bagency\b", organisation_key) and not private_institution
    ):
        return PublicOfficeType.government_agency
    if re.search(r"\bgovernment department\b", organisation_key) or (
        government_source and re.search(r"\b(?:department of|department)\b", organisation_key)
    ):
        return PublicOfficeType.government_department
    if re.search(r"\b(prime minister|head of government)\b", title_key) or (
        re.search(r"\bchancellor\b", title_key)
        and (government_source or bool(_PUBLIC_INSTITUTION_RE.search(organisation_key)))
    ):
        return PublicOfficeType.head_of_government
    if re.search(r"\b(head of state)\b", title_key) or (
        re.search(r"\bpresident\b", title_key)
        and not private_institution
        and (public_reference_source or bool(_PUBLIC_INSTITUTION_RE.search(organisation_key)))
    ):
        return PublicOfficeType.head_of_state
    if re.search(r"\b(cabinet|executive office|presidency|chancellery)\b", organisation_key) or (
        government_source and re.search(r"\boffice of the\b", organisation_key)
    ):
        return PublicOfficeType.executive_office
    if _PUBLIC_INSTITUTION_RE.search(organisation_key):
        return PublicOfficeType.public_institution
    return None


def is_probable_country_context(
    organisation: str,
    title: str | None,
    *,
    source_types: set[SourceType] | None = None,
) -> bool:
    """Detect a country-like context paired with a national leadership title."""

    key = comparison_key(organisation)
    office_type = classify_public_office(organisation, title, source_types=source_types)
    if office_type not in {PublicOfficeType.head_of_state, PublicOfficeType.head_of_government}:
        return False
    if (
        _PUBLIC_INSTITUTION_RE.search(key)
        or _PRIVATE_INSTITUTION_RE.search(key)
        or re.search(r"\b(office|department|agency|authority)\b", key)
    ):
        return False
    return 0 < len(key.split()) <= 5


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
    if classify_public_office(organisation, title, source_types=source_types) is not None:
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
