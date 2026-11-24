"""Extract literal, person-scoped education relationships from one evidence span."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.research.normalisation import EducationKind, normalise_degree_candidates
from app.schemas import ExtractedClaim, PersonSeed, ProfileField

from .context import EvidenceSpan

_ARTICLE = r"(?:(?:a|an|the|his|her|their)\s+)?"
_QUALIFICATION = (
    r"b\.?\s*(?:sc|a|eng|b\.?\s*a|com|f\.?\s*a|tech)|"
    r"m\.?\s*(?:sc|s|b\.?\s*a|p\.?\s*[ah]|eng|res|f\.?\s*a|phil|a)|"
    r"ph\.?\s*d|d\.?\s*phil|e\.?\s*m\.?\s*b\.?\s*a|executive mba|"
    r"m\.?\s*d|m\.?\s*b\.?\s*b\.?\s*s|m\.?\s*b\.?\s*ch\.?\s*b|"
    r"j\.?\s*d|l\.?\s*l\.?\s*b|pg\.?\s*dip|"
    r"bachelor of medicine(?: and)? bachelor of surgery|bachelor of laws?|"
    r"doctor of (?:philosophy|medicine)|juris doctor|(?:medical|law) degree|"
    r"(?:advanced )?post[- ]?graduate (?:degree|diploma)|diploma|"
    r"(?:bachelor|master)(?:['’]s|s)?(?: degree)?|undergraduate degree|"
    r"doctorate|doctoral degree|doctorat|doctorado|doutorado|doutoramento|"
    r"maestr[ií]a|ma[iî]trise|mestrado|licence|licenciatura|laurea(?: magistrale)?"
)
_DEGREE = re.compile(rf"^(?:{_QUALIFICATION})(?![\w-])\.?", re.IGNORECASE)
_GENERIC_DEGREE = re.compile(r"^(?:(?:college|university)\s+)?degree\b", re.I)
_LONG_DEGREE = re.compile(
    r"^(?:bachelor|master) of [^,;|]+?(?=\s+(?:in|from|at|with)\s|[,;|]|$)", re.IGNORECASE
)
_HONOURS = re.compile(r"\s*\(hon(?:ours|ors|s\.?)\)\s*", re.I)
_SEPARATOR = re.compile(
    rf"(?:\s*[,;]\s*(?:and\s+)?|\s+and\s+|\s+before\s+)"
    rf"(?:(?:also\s+|later\s+|then\s+)?(?:earn(?:ed|ing)|receiv(?:ed|ing)|obtain(?:ed|ing)|"
    rf"complet(?:ed|ing)|hold(?:s|ing))\s+)?{_ARTICLE}"
    rf"(?=(?:honorary\s+)?(?:{_QUALIFICATION})(?![\w-]))",
    re.IGNORECASE,
)
_EARNED = re.compile(
    r"^(?:(?:also|later|then|previously)\s+)*(?:went on to\s+|has\s+)?"
    r"(?:earned|earn|received|receive|obtained|obtain|completed|complete|holds|hold|has|had|"
    r"was awarded|was granted|graduated with)\s+",
    re.IGNORECASE,
)
_GRADUATED = re.compile(r"^graduated from\s+(.+?)\s+with\s+(.+)$", re.IGNORECASE)
_ATTENDANCE = re.compile(
    r"^(?:(?:also|previously)\s+)?(?:(?P<study>studied|majored in|speciali[sz]ed in)|attended|graduated from|"
    r"(?:is|was)\s+(?:an?\s+)?(?:alumna|alumnus|alum|graduate)\s+of)\s+",
    re.IGNORECASE,
)
_INSTITUTION_WORD = re.compile(
    r"\b(?:universit\w+|college|polytechnic\w*|academy|acad[ée]mie|[ée]cole|"
    r"school of|institute|instituto|institut|hochschule)\b",
    re.IGNORECASE,
)
_EVENT = re.compile(r"\b(?:lecture|seminar|workshop|conference|festival|ceremony|symposium)\b", re.I)
_INSTITUTION_SEPARATOR = re.compile(r"\s+(?:and|or|&|as well as)\s+|\s*[,/]\s*", re.IGNORECASE)
_INSTITUTION_PARTICLES = frozenset("and of the de del della do da des du la le en für di und van von".split())
_INSTITUTION_ACRONYMS = frozenset("ETH EPFL HKU IIT LSE MIT NUS NYU UBC UCL UCLA UNSW USC".split())
_YEAR = re.compile(r"(?:\s*,?\s+(?:in\s+)?|\s*,\s*|\s*\()((?:18|19|20)\d{2})\)?$")
_CLAUSE_END = re.compile(
    r"[,;]\s*(?:where|which|who|when|while|before|after)\b|"
    r"\s+(?:before|after|while)\s+(?:joining|working|moving|serving|becoming)\b|"
    r"\s+and\s+(?:currently|now|later|then)?\s*(?:is|was|works?|serves?|joined|leads?|became)\b",
    re.IGNORECASE,
)
_NON_EARNED = re.compile(
    r"\b(?:honorary|honoris causa|postdoc\w*|candidate|candidacy|pursu\w+|enrolled|"
    r"in progress|expected|planned|not completed|coursework|incomplete)\b",
    re.IGNORECASE,
)
_SUBJECT_PREFIX = re.compile(r"^(?:in|with (?:an?\s+)?(?:major|speciali[sz]ation|concentration) in)\s+", re.I)
_INSTITUTION_PREFIX = re.compile(r"\s+(?:at|from)\s+", re.I)
_EDUCATION_HEADING = re.compile(
    r"^(?:education|academic (?:background|qualifications)|qualifications|degrees|credentials)\b", re.I
)
_EDUCATION_LABEL = re.compile(r"^(?:education|qualification|degree)\s*:\s*", re.I)
_MARKDOWN_LINK = re.compile(r"\[([^\[\]\r\n]+)\]\(https?://[^\s()]+\)")


@dataclass(frozen=True, slots=True)
class _Qualification:
    degree: str | None = None
    subject: str | None = None
    institution: str | None = None
    year: str | None = None


def _institution(text: str) -> str | None:
    text = re.sub(r"^the\s+", "", text.strip(" ,.;"), flags=re.IGNORECASE)
    if text in _INSTITUTION_ACRONYMS:
        return text
    if not _INSTITUTION_WORD.search(text) or _EVENT.search(text) or len(text) > 160:
        return None
    # More than one named institution gives no unique issuer for the credential.
    if (
        sum(
            part in _INSTITUTION_ACRONYMS or bool(_INSTITUTION_WORD.search(part))
            for part in _INSTITUTION_SEPARATOR.split(text)
        )
        > 1
    ):
        return None
    words = re.findall(r"[^\W\d_][\w'’.-]*", text)
    if not 2 <= len(words) <= 16 or not re.fullmatch(r"[\w\s'’.,&()\-–]+", text):
        return None
    if all(word[0].isupper() or word.lower() in _INSTITUTION_PARTICLES for word in words):
        return text
    return None


def _subject(text: str) -> str | None:
    text = text.strip(" .,:;()")
    if (
        not text
        or len(text) > 110
        or len(text.split()) > 12
        or _INSTITUTION_WORD.search(text)
        or _EVENT.search(text)
        or not re.fullmatch(r"[^\W\d_][\w\s'’&+()\-–/]*", text)
        or re.search(
            r"\b(?:respectively|another|various|unspecified|works?|teaches?|from|since|award|prize|degree)\b",
            text,
            re.I,
        )
        or _DEGREE.match(re.sub(rf"^{_ARTICLE}", "", text, flags=re.I))
    ):
        return None
    return text


def _parse_degree(text: str) -> _Qualification | None:
    text = _CLAUSE_END.split(text.strip(" .,:;"), maxsplit=1)[0]
    if _NON_EARNED.search(text):
        return None
    year = None
    if match := _YEAR.search(text):
        year = match[1]
        text = text[: match.start()].rstrip()
    text = re.sub(rf"^{_ARTICLE}", "", text, flags=re.I)
    match = _LONG_DEGREE.match(text) or _DEGREE.match(text) or _GENERIC_DEGREE.match(text)
    if match is None:
        return None
    degree = _HONOURS.sub("", match[0]).strip()
    normalised = normalise_degree_candidates(degree)
    if len(normalised) != 1 or normalised[0].education_kind != EducationKind.academic_degree:
        return None
    degree = degree if normalised[0].reason_code != "GENERIC_DEGREE_DEFAULT" else None
    tail = text[match.end() :].strip()
    if honours := _HONOURS.match(tail):
        tail = tail[honours.end() :]
    tail = re.sub(r"^(?:degree|degrees)\b\s*", "", tail, flags=re.I)
    if tail.startswith("(") and (suffix := re.match(r"^\(([^()]+)\)\s*", tail)):
        # Ignore a parallel spelling only if both literal labels normalize to
        # the same existing qualification level. Other parentheses are unclear.
        equivalent = normalise_degree_candidates(suffix[1])
        if equivalent[0].value == normalised[0].value:
            tail = tail[suffix.end() :]
    if tail and not re.match(r"^(?:in|with|from|at)\s|^,", tail, re.I):
        return None
    subject, institution = None, None
    if tail and not re.search(r"\brespectively\b", tail, re.I):
        # Prepending one space permits institution-only tails such as "from X".
        parts = _INSTITUTION_PREFIX.split(" " + tail, maxsplit=1)
        if len(parts) == 2:
            institution = _institution(parts[1])
        subject_tail = parts[0].strip()
        if len(parts) == 1 and "," in subject_tail:
            before, after = subject_tail.split(",", 1)
            if campus := _institution(after):
                subject_tail, institution = before, campus
        if prefix := _SUBJECT_PREFIX.match(subject_tail):
            subject = _subject(subject_tail[prefix.end() :])
    if subject is None and degree and normalised[0].subject:
        implicit = re.search(re.escape(normalised[0].subject), degree, re.I)
        if implicit:
            subject = _subject(implicit[0])
    return _Qualification(degree, subject, institution, year)


def _credential_list(text: str, *, shared_institution: str | None = None) -> list[_Qualification]:
    both = bool(re.match(r"^both\s+", text, re.I))
    text = re.sub(r"^both\s+", "", text, flags=re.I)
    pieces = _SEPARATOR.split(text)
    credentials = [_parse_degree(piece) for piece in pieces]
    if (
        len(credentials) > 1
        and (both or re.search(r"\bdegrees\s+(?:in|from|at)\s", pieces[-1], re.I))
        and all(
            item and item.degree and not (item.subject or item.institution or item.year)
            for item in credentials[:-1]
        )
        and (last := credentials[-1]) is not None
        and last.degree
    ):
        credentials = [
            _Qualification(item.degree, last.subject, last.institution, last.year) if item else None
            for item in credentials
        ]
    return [
        _Qualification(item.degree, item.subject, item.institution or shared_institution, item.year)
        for item in credentials
        if item is not None
    ]


def _table_row(text: str) -> list[_Qualification]:
    cells = [cell.strip() for cell in text.strip("|").split("|")]
    if len(cells) != 3 or not all(cells):
        return []
    for degree_index, subject_index, institution_index in ((0, 1, 2), (1, 2, 0)):
        item = _parse_degree(cells[degree_index])
        institution = _institution(cells[institution_index])
        if (
            item
            and item.degree == cells[degree_index]
            and institution
            and (subject := _subject(cells[subject_index]))
        ):
            return [_Qualification(item.degree, subject, institution)]
    return []


def _education_relations(body: str, section: str) -> list[_Qualification]:
    if graduated := _GRADUATED.match(body):
        institution = _institution(graduated[1])
        return _credential_list(graduated[2], shared_institution=institution) or [
            _Qualification(institution=institution)
        ]
    if earned := _EARNED.match(body):
        return _credential_list(body[earned.end() :])
    if attendance := _ATTENDANCE.match(body):
        relation = _CLAUSE_END.split(body[attendance.end() :], maxsplit=1)[0]
        relation = _YEAR.sub("", relation.rstrip("."))
        if not attendance["study"]:
            return [_Qualification(institution=_institution(relation))]
        parts = _INSTITUTION_PREFIX.split(" " + relation, maxsplit=1)
        if len(parts) == 2:
            return [_Qualification(subject=_subject(parts[0]), institution=_institution(parts[1]))]
        institution = _institution(relation)
        return [_Qualification(subject=None if institution else _subject(relation), institution=institution)]
    if label := _EDUCATION_LABEL.match(body):
        return _credential_list(body[label.end() :])
    if _EDUCATION_HEADING.match(section):
        return _table_row(body) if "|" in body else _credential_list(body)
    return []


def extract_education(span: EvidenceSpan, _seed: PersonSeed) -> list[ExtractedClaim]:
    """Keep university, degree and subject together only where the wording does."""

    body = _MARKDOWN_LINK.sub(r"\1", span.body).replace("**", "").strip(" \t-*•")
    claims: list[ExtractedClaim] = []
    for index, item in enumerate(_education_relations(body, span.section)):
        for field, value in (
            (ProfileField.university_name, item.institution),
            (ProfileField.degree_type, item.degree),
            (ProfileField.subject, item.subject),
        ):
            if value and value in span.text:
                claims.append(
                    span.claim(
                        field,
                        value,
                        temporal_context=item.year,
                        source_claim_type="education_qualification"
                        if item.degree
                        else "education_attendance",
                        fact_group=f"{span.group[:76]}:education:{index}",
                    )
                )
    return claims
