"""Bounded, literal person-owned spans shared by the field matchers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

from app.research.identity import expanded_subject_matches
from app.research.normalisation import name_key
from app.schemas import ExtractedClaim, PersonSeed, ProfileField, SourceRecord

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(?:[-*+]\s+|\d{1,3}[.)]\s+)")
_PREFIX = re.compile(
    r"^(?:(?:as of|in|since|until|on|from) "
    r"(?:[A-Za-z]{3,9} \d{1,2}, \d{4}|\d{1,2} [A-Za-z]{3,9} \d{4}|\d{4}(?:-\d{2}-\d{2})?),\s*)?",
    re.I,
)
_HONORIFIC = r"(?:(?:Dr|Prof|Professor|Mr|Mrs|Ms)\.?\s+)?"
_NAME_SEPARATOR = r"(?:[ \t]+|[ \t]*[-.'’][ \t]*)"
_NAME_END = r"(?![\w]|[-'’][^\W\d_])"
_INITIAL = re.compile(r"(?:^|\s)[A-Z]\.$")
_ABBREVIATION = re.compile(r"(?:\b(?:Dr|Mr|Mrs|Ms|Prof|St|Inc|Ltd|Jr|Sr)\.|\b(?:[A-Za-z]\.){2,})$", re.I)
_SENTENCE_BOUNDARY = re.compile(r"[.!?][\"”)]*(?=\s+[A-Z#])")
_SUBSECTION = re.compile(
    r"^(?:about(?: me)?|biograph(?:y|ical profile)|profile|education|academic (?:background|qualifications)|"
    r"qualifications|credentials|degrees|employment|career(?: history)?|experience|professional "
    r"(?:experience|background)|current (?:role|position)|contact|links)\s*:?$",
    re.I,
)
_LABEL = re.compile(
    r"^(?:education|qualification|degree|university|institution|major|subject|current (?:role|position)|"
    r"employment|employer|job title|position|role|profile|biography)\s*:",
    re.I,
)
_PRONOUN = re.compile(r"^(?:she|he|they)\b\s*", re.I)
_ALIAS = re.compile(r"^\s*\((?:also\s+)?(?:known as|called|credited as)\s+([^()]{2,100})\)\s*", re.I)
_APPOSITION = re.compile(r"^,\s*(?!and\b|or\b|with\b)[^,;]{2,100}\s(?:at|of)\s[^,;]{2,100},", re.I)
_PRONOUN_ANCHOR = re.compile(
    r"^(?i:is|was) an? [a-z]+(?:[ -](?!(?:of|with|to|at|for|from|in|and)\b)[a-z]+){0,4}[.!?]$"
)
_NAME_LIKE_PREFIX = re.compile(r"^(?:[A-Z][^\W\d_]*(?:[-'’][^\W\d_]+)*\s+){2,4}")
_SUBJECT_BODY = re.compile(
    r"^(?:[,;:]\s*|[–—]\s*|\|\s*|\(.*?\)\s*)?"
    r"(?:(?:currently|previously|formerly|also|now|still|later|then|first|once)\s+)*"
    r"(?:is|was|has|had|holds?|earned|received|completed|graduated|studied|attended|obtained|"
    r"gained|got|read|majored|speciali[sz]ed|works?|worked|serves?|served|joined|leads?|led|"
    r"became|remains?|retired|left|founded|co-founded|cofounded|teaches|taught|took|"
    r"trained|pursued|pursues|isn’t|isn't)\b",
    re.I,
)
_LINK_OPEN = re.compile(r"^\]\(https?://[^\s()<>]+\)", re.I)


@dataclass(frozen=True)
class EvidenceSpan:
    text: str
    body: str
    subject_name: str
    location: str
    section: str
    group: str
    explicit: bool = True

    def claim(self, field: ProfileField, value: str, **metadata) -> ExtractedClaim:
        return ExtractedClaim(
            field=field,
            value=value,
            evidence=self.text,
            subject_name=self.subject_name,
            evidence_location=self.location,
            **metadata,
        )


def _sentences(line: str):
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(line):
        end = match.end()
        prefix = line[max(start, end - 24) : end]
        if not (_INITIAL.search(prefix) or _ABBREVIATION.search(prefix)):
            yield start, end
            start = end
    if start < len(line):
        yield start, len(line)


class _Target:
    def __init__(self, seed: PersonSeed, source: SourceRecord, chunk: str):
        self.seed, self.source, self.chunk = seed, source, chunk
        tokens = name_key(seed.full_name).split()
        self.exact = re.compile(_HONORIFIC + _NAME_SEPARATOR.join(map(re.escape, tokens)) + _NAME_END, re.I)
        self.expanded = (
            re.compile(
                _HONORIFIC
                + re.escape(tokens[0])
                + r"(?P<middle>(?:\s+[^\W\d_]+\.?){1,2})\s+"
                + re.escape(tokens[-1])
                + _NAME_END,
                re.I,
            )
            if len(tokens) == 2 and source.identity.signals.get("matched_seed_anchors")
            else None
        )

    def leading(self, text: str):
        content = text[_PREFIX.match(text).end() :].lstrip("[*_ ")
        for matcher in (self.exact, self.expanded):
            if matcher is None or (match := matcher.match(content)) is None or not match.group():
                continue
            name = match.group()
            if matcher is self.expanded and (
                not all(token[0].isupper() for token in match["middle"].split())
                or not expanded_subject_matches(self.seed, name, self.chunk)
            ):
                continue
            remainder = _LINK_OPEN.sub("", content[match.end() :], count=1).lstrip("*_ ")
            return name, remainder
        return None


def evidence_spans(chunk: str, seed: PersonSeed, source: SourceRecord) -> list[EvidenceSpan]:
    """Find explicit subjects and narrowly scoped heading, alias and pronoun continuations."""
    target = _Target(seed, source, chunk)
    chunk_key = sha256(chunk.encode()).hexdigest()[:16]
    spans = []
    offset = 0
    owner = recent = None
    alias = section = ""

    def add(start, end, body, name, *, explicit=True):
        if 3 <= end - start <= 800 and 2 <= len(name) <= 200:
            spans.append(
                EvidenceSpan(
                    chunk[start:end],
                    body,
                    name,
                    f"chunk:{start}-{end}",
                    section,
                    f"{chunk_key}:{start}-{end}",
                    explicit,
                )
            )

    for raw_line in chunk.splitlines(keepends=True):
        line_start = offset + len(raw_line) - len(raw_line.lstrip())
        offset += len(raw_line)
        line = raw_line.strip()
        if not line:
            if not alias:
                recent = None
            continue
        heading = _HEADING.fullmatch(line)
        if heading:
            level, section = len(heading[1]), heading[2].strip("*_ ")
            identity = target.leading(section)
            recent, alias = None, ""
            if identity and (not identity[1] or re.match(r"^(?:[|–—]|-\s)", identity[1])):
                owner = (level, line_start, identity[0])
                add(line_start, line_start + len(line), "", identity[0])
            elif not owner or level <= owner[0] or not _SUBSECTION.fullmatch(section):
                owner = None
            continue
        bullet = _BULLET.match(line)
        ranges = [(0, len(line))] if bullet or line.startswith("|") else _sentences(line)
        for left, right in ranges:
            raw = line[left:right]
            body = raw.strip()
            start, end = line_start + left + len(raw) - len(raw.lstrip()), line_start + right
            if bullet:
                body = body[bullet.end() :]
            identity = target.leading(body)
            if identity:
                name, remainder = identity
                declaration = _ALIAS.match(remainder)
                relation = remainder[declaration.end() :] if declaration else remainder
                statement = _SUBJECT_BODY.match(relation)
                apposition = not statement and _APPOSITION.match(relation)
                if not relation or statement or apposition or relation in {".", ":"}:
                    add(start, end, relation if apposition else relation.lstrip(" ,:;|–—"), name)
                    recent = (start, name) if declaration or _PRONOUN_ANCHOR.fullmatch(relation) else None
                    alias = declaration[1].strip("\"'“” ") if declaration else ""
                    if not relation and body.startswith(("**", "__")):
                        owner = (0, start, name)
                    continue
            continuation = _PRONOUN.match(body)
            alias_prefix = bool(alias and body.casefold().startswith(alias.casefold() + " "))
            if recent and (alias_prefix or (continuation and not alias)):
                relation = body[continuation.end() if continuation else len(alias) :].lstrip()
                if _SUBJECT_BODY.match(relation):
                    add(recent[0], end, relation, recent[1], explicit=False)
            elif owner and (bullet or body.startswith("|") or _LABEL.match(body)):
                add(owner[1], end, body, owner[2], explicit=False)
            elif (other := _NAME_LIKE_PREFIX.match(body)) and _SUBJECT_BODY.match(body[other.end() :]):
                owner = None
            recent, alias = None, ""
    return spans
