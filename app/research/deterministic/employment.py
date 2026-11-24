"""Extract literal employer and title claims from owned employment statements."""

from __future__ import annotations

import re
from datetime import date, datetime

from app.research.claims import literal_key
from app.schemas import ExtractedClaim, PersonSeed, ProfileField

from .context import EvidenceSpan

_DATE = r"(?:\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9} \d{1,2}, \d{4}|\d{1,2} [A-Za-z]{3,9} \d{4}|\d{4})"
_AS_OF = re.compile(rf"(?:^|\n)\s*as of (?P<date>{_DATE}),\s*", re.IGNORECASE)
_DATE_SUFFIX = re.compile(
    rf"\s*[,]?\s+(?P<kind>as of|until|through|since|in|from) (?P<first>{_DATE})"
    rf"(?:\s+(?:to|until|through) (?P<last>{_DATE}))?$",
    re.IGNORECASE,
)
_DATE_RANGE = re.compile(rf"(?P<first>{_DATE})\s*[–—-]\s*(?P<last>{_DATE}|present|current)", re.IGNORECASE)
_TIME_CUES = r"currently|now|still|formerly|previously|former"
_TITLE_VERBS = (
    r"works? as|worked as|serves? as|served as|has served as|has worked as|"
    r"(?:is|was) (?:employed|working|serving) as|has been|had been|is|was"
)
_EMPLOYER_VERBS = (
    r"works? (?:for|at)|worked (?:for|at)|has worked (?:for|at)|(?:is|was) (?:employed by|working (?:at|for))|"
    r"joined|left|retired from"
)
_EMPLOYER_VERB = re.compile(_EMPLOYER_VERBS)
_PROGRESSIVE = re.compile(r"(?:employed (?:by|as)|working (?:at|for|as)|serving as)\s+", re.I)
_INTRO = re.compile(
    rf"^(?:(?P<before>{_TIME_CUES})\s+)?(?P<verb>{_EMPLOYER_VERBS}|{_TITLE_VERBS})\s+"
    rf"(?:(?P<after>{_TIME_CUES})\s+)?(?P<payload>.+)$",
    re.IGNORECASE,
)
_COORDINATED_ROLE = re.compile(
    rf"(?:;\s*|\s*,?\s+\b(?:and|but|while)\s+)"
    rf"(?=(?:(?:{_TIME_CUES})\s+)?(?:{_TITLE_VERBS}|{_EMPLOYER_VERBS})\s+)",
    re.IGNORECASE,
)
_TITLE = re.compile(
    r"\b(?:officer|president|director|chair(?:man|woman|person)?|board member|founder|owner|manager|lead|head|"
    r"engineer|architect|scientist|analyst|professor|lecturer|dean|rector|fellow|consultant|advis[eo]r|"
    r"counsel|lawyer|editor|journalist|reporter|designer|specialist|developer|coordinator|researcher|"
    r"teacher|secretary|treasurer|accountant|producer|partner|administrator|practitioner|planner|investigator|"
    r"assistant|strategist|economist|librarian|curator|physician|surgeon|trustee|"
    r"CEO|CFO|COO|CTO|CIO|CISO|CMO|CHRO|CPO|VP|SVP|EVP)\b",
    re.IGNORECASE,
)
_NOT_ROLE = re.compile(
    r"^(?:not|never|no longer|expected to|set to|candidate for|friend|colleague|"
    r"spouse|wife|husband|widow|widower|son|daughter|mother|father|parent|brother|sister|sibling|client|student|"
    r"related to|responsible for|working with|meeting with|met|welcomed|"
    r"(?:accompanied|advised|assisted|interviewed|introduced|quoted|trained|mentored) by)\b",
    re.IGNORECASE,
)
_CAREER_SECTION = re.compile(
    r"\b(?:experience|employment|career|work history|professional roles)\b", re.IGNORECASE
)
_TABLE_FIELDS = re.compile(
    r"^(?P<field>role|title|position|job title|employer|company|organisation|organization):\s*", re.I
)
_TRAILING_CLAUSE = re.compile(r",\s*(?:where|which|who)\b|,?\s+(?:and|but|while)\s+(?:she|he|they)\b", re.I)
_LINK_LABEL = re.compile(r"\[([^\[\]\r\n]+)\]\(https?://[^\s()<>\"]+\)", re.IGNORECASE)
_ARTICLE = re.compile(r"^(?:the|an?)\s+", re.I)
_ROLE_INTRODUCTION = re.compile(r"\b(?:with|by|alongside)\b", re.I)
_ROLE_LINKS = tuple(re.compile(rf"\s+{word}\s+", re.I) for word in ("at", "for", "of"))
_ORG_OF_ROLE = re.compile(
    r"\b(?:chief .+ officer|chief executive|CEO|CFO|COO|CTO|president|chair(?:man|woman|person)?|"
    r"founder|owner|trustee|partner)\b",
    re.I,
)
_AS_ROLE = re.compile(r"\s+as\s+", re.I)
_ALIAS = re.compile(r"^(?P<name>.+?)\s+\((?P<alias>[A-Z][A-Z0-9]{1,11})\)$")
_ORGANISATION_SUFFIX = r"Inc|Ltd|LLC|PLC|LLP|Corp|Corporation|Limited"
_APPOSITIVE_END = re.compile(rf",(?!\s+(?:{_ORGANISATION_SUFFIX})\b)", re.I)
_ORGANISATION_LIST = re.compile(r"\s+and\s+", re.I)
_NAMED_INSTITUTION = re.compile(
    rf"^(?:the )?(?:University|College|Institute|Department|Ministry|Office|Museum)\b|"
    rf"\b(?:{_ORGANISATION_SUFFIX}|Group|University|College|Institute|Transit|Foundation|Hospital|Bank|Studio)$",
    re.I,
)
_ROLE_STATUS = re.compile(r"^(?P<cue>current|former|previous)\s+", re.I)
_ROLE_LABEL = re.compile(
    r"^(?P<label>current (?:role|position)|employment|employer|job title|position|role):\s*(?P<value>.+)$",
    re.I,
)
_GENERIC_ORG = re.compile(
    r"(?:the )?(?:company|employer|organisation|organization|department|government|university|"
    r"school|research|research and development|human resources|engineering|marketing|education|"
    r"operations|finance|administration|a firm|a company)",
    re.I,
)
_CONNECTORS = frozenset(
    {"a", "an", "and", "at", "de", "del", "des", "di", "for", "in", "la", "of", "the", "van", "von"}
)
_PAST = frozenset({"was", "worked", "served", "had", "left", "retired", "former", "formerly", "previously"})
_PRESENT = frozenset({"is", "works", "serves", "currently", "now", "still"})


def _display_fragment(text: str) -> str:
    # Whole-cell links and emphasis surround a literal value. Preserve internal
    # markup so a reconstituted string cannot pass as a verbatim proposal.
    text = text.strip().strip("*`_").strip().rstrip(". ")
    if (match := _ALIAS.fullmatch(text)) and match["alias"] == "".join(
        word[0].upper() for word in match["name"].split() if word.casefold() not in _CONNECTORS
    ):
        return match["name"]
    return text


def _organisation(text: str) -> str | None:
    value = _display_fragment(text)
    words = value.split()
    if not words or len(words) > 14 or len(value) > 180 or _GENERIC_ORG.fullmatch(value):
        return None
    if not any(char.isupper() for char in value):
        return None
    if any(
        word.casefold() not in _CONNECTORS
        and word not in {"&", "+"}
        and not any(char.isupper() or char.isdigit() for char in word)
        for word in words
    ):
        return None
    if words[-1].casefold() in _CONNECTORS or re.search(r"[;|!?/:\[\]{}]", value):
        return None
    if _APPOSITIVE_END.search(value):
        return None
    if (conjunction := _ORGANISATION_LIST.search(value)) and (
        not _NAMED_INSTITUTION.match(value) or _NAMED_INSTITUTION.search(value[conjunction.end() :])
    ):
        return None
    return value


def _job_title(text: str) -> str | None:
    value = _display_fragment(_ARTICLE.sub("", text))
    if len(value) > 120 or len(value.split()) > 12 or _NOT_ROLE.match(value):
        return None
    head = _TITLE.search(value)
    if not head or _ROLE_INTRODUCTION.search(value, 0, head.start()):
        return None
    if re.search(r"[;|!?/:\[\]{}]|\b(?:that|who|which|where|because)\b", value, re.I):
        return None
    return value


def _full_date(value: str | None) -> date | None:
    if value is None or len(value) == 4:
        return None
    for form in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, form).date()
        except ValueError:
            continue
    return None


def _timing(
    value: str, current: bool | None
) -> tuple[str, bool | None, date | None, date | None, str | None]:
    match = _DATE_SUFFIX.search(value)
    if match is None:
        return value, current, None, None, None
    text = value[: match.start()].rstrip(", ")
    kind = match["kind"].casefold()
    if match["last"] or kind in {"until", "through"}:
        return text, False, None, _full_date(match["last"] or match["first"]), match[0].strip(", ")
    if kind == "since" and current is not False:
        current = True
    return text, current, _full_date(match["first"]) if kind == "as of" else None, None, match[0].strip(", ")


def _role_pair(payload: str, verb: str) -> tuple[str | None, str | None]:
    if _EMPLOYER_VERB.fullmatch(verb):
        parts = _AS_ROLE.split(payload, maxsplit=1)
        if len(parts) == 1:
            return _organisation(parts[0]), None
        company, title = parts
        employer, job_title = _organisation(company), _job_title(title)
        return (employer, job_title) if employer and job_title else (None, None)

    for separator in _ROLE_LINKS:
        match = separator.search(payload)
        if match:
            job_title = _job_title(payload[: match.start()])
            employer = _organisation(payload[match.end() :])
            if match[0].strip().casefold() == "of" and job_title and not _ORG_OF_ROLE.search(job_title):
                # Academic and departmental titles commonly have an `of` phrase.
                # Keep it in the title unless the object is recognizably an institution.
                if not employer or not _NAMED_INSTITUTION.search(employer):
                    return None, _job_title(payload)
            return (employer, job_title) if employer and job_title else (None, None)
    return None, _job_title(payload)


def _career_row(
    body: str, section: str
) -> tuple[str | None, str | None, bool | None, date | None, str | None]:
    if not _CAREER_SECTION.search(section) or "|" not in body:
        return None, None, None, None, None
    cells = [cell.strip() for cell in body.strip("| ").split("|")]
    if len(cells) not in {2, 3}:
        return None, None, None, None, None
    left, right = cells[:2]
    labels = [_TABLE_FIELDS.match(cell) for cell in (left, right)]
    if any(labels):
        if not all(labels):
            return None, None, None, None, None
        values = {
            label["field"].casefold(): cell[label.end() :] for cell, label in zip((left, right), labels)
        }
        employer = next(
            (
                _organisation(values[field])
                for field in values
                if field in {"employer", "company", "organisation", "organization"}
            ),
            None,
        )
        title = next(
            (
                _job_title(values[field])
                for field in values
                if field in {"role", "position", "title", "job title"}
            ),
            None,
        )
    else:
        employer, title = _organisation(right), _job_title(left)
        # Two title-shaped cells can name two distinct roles rather than an employer.
        if _job_title(right):
            return None, None, None, None, None
    if not employer or not title:
        return None, None, None, None, None
    context = cells[2] if len(cells) == 3 else ""
    duration = _DATE_RANGE.fullmatch(context)
    if duration:
        current = duration["last"].casefold() in {"current", "present"}
        return employer, title, current, _full_date(duration["last"]), context
    if context.casefold() in {"current", "present", "former", "previous"}:
        return employer, title, context.casefold() in {"current", "present"}, None, context
    return employer, title, None, None, None


def _claims(
    span: EvidenceSpan,
    index: int,
    employer: str | None,
    title: str | None,
    current: bool | None,
    as_of: date | None,
    ended: date | None,
    temporal_context: str | None,
) -> list[ExtractedClaim]:
    evidence_key = literal_key(span.text)
    return [
        span.claim(
            field,
            value,
            fact_group=f"employment:{span.group[:77]}:{index}",
            is_current=current,
            as_of_date=as_of,
            end_date=ended,
            temporal_context=temporal_context,
        )
        for field, value in ((ProfileField.organisation, employer), (ProfileField.job_title, title))
        if value and literal_key(value) in evidence_key
    ]


def extract_employment(span: EvidenceSpan, seed: PersonSeed) -> list[ExtractedClaim]:
    """Use explicit role syntax or owned career rows, without copying seed fields."""
    if not 3 <= len(span.text) <= 800:
        return []
    body = _LINK_LABEL.sub(r"\1", span.body).strip(" \t–—").rstrip(". ")
    if body.startswith(",") and span.explicit:
        apposition = _APPOSITIVE_END.split(body[1:].strip(), maxsplit=1)[0]
        title_phrase = _ARTICLE.sub("", apposition)
        status = _ROLE_STATUS.match(title_phrase)
        if status:
            title_phrase = title_phrase[status.end() :]
        employer, title = _role_pair(title_phrase, "")
        if not employer or not title:
            return []
        current = status["cue"].casefold() == "current" if status else None
        return _claims(span, 0, employer, title, current, None, None, None)

    if label := _ROLE_LABEL.fullmatch(body):
        current = True if label["label"].casefold().startswith("current ") else None
        if label["label"].casefold() in {"employer", "employment"}:
            employer, title = _organisation(label["value"]), None
        else:
            employer, title = _role_pair(label["value"], "")
        return _claims(span, 0, employer, title, current, None, None, None)

    employer, title, current, ended, context = _career_row(body, span.section)
    if employer and title:
        return _claims(span, 0, employer, title, current, None, ended, context)

    claims = []
    for index, statement in enumerate(_COORDINATED_ROLE.split(body)):
        match = _INTRO.fullmatch(statement.strip(" ,"))
        if match is None:
            continue
        verb = match["verb"].casefold()
        payload = match["payload"]
        if _NOT_ROLE.match(payload):
            continue
        if verb in {"is", "was"} and (progressive := _PROGRESSIVE.match(payload)):
            verb = f"{verb} {progressive[0].strip().casefold()}"
            payload = payload[progressive.end() :]
        cues = {verb.split()[0], (match["before"] or "").casefold(), (match["after"] or "").casefold()}
        current = False if cues & _PAST else True if cues & _PRESENT else None
        payload, current, as_of, ended, context = _timing(_TRAILING_CLAUSE.split(payload, 1)[0], current)
        if not index and as_of is None and ended is None and current is not False:
            prefix = _AS_OF.search(span.text)
            if prefix and literal_key(span.text[prefix.end() :]).startswith(
                literal_key(span.subject_name) + " "
            ):
                as_of, context = _full_date(prefix["date"]), prefix[0].strip(" ,")
        employer, title = _role_pair(payload, verb)
        claims.extend(_claims(span, index, employer, title, current, as_of, ended, context))
    return claims
