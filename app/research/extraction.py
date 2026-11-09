"""Bounded chunk extraction, consumed in source order for stable evidence decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping

from bs4 import BeautifulSoup

from app.prompts.extraction import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT
from app.research.concurrency import ordered_window
from app.research.identity import research_seed_payload
from app.research.normalisation import (
    EducationKind,
    classify_education,
    name_key,
    normalise_degree_candidates,
)
from app.research.telemetry import count, stage
from app.retrieval.urls import canonicalise_url, source_policy_allows
from app.schemas import ExtractedClaim, ExtractionResponse, PersonSeed, ProfileField

DETERMINISTIC_EXTRACTION_VERSION = "structured-v1"
_PERSON_EVIDENCE_PROPERTIES = {
    "@type",
    "@id",
    "@context",
    "name",
    "givenName",
    "familyName",
    "additionalName",
    "description",
    "disambiguatingDescription",
    "jobTitle",
    "worksFor",
    "alumniOf",
    "hasCredential",
    "hasOccupation",
    "affiliation",
    "memberOf",
}
_ORGANISATION_TYPES = {
    "Organization",
    "Corporation",
    "CollegeOrUniversity",
    "EducationalOrganization",
    "GovernmentOrganization",
    "LocalBusiness",
    "NGO",
    "ResearchOrganization",
}


def _schema_types(node: dict) -> set[str]:
    values = node.get("@type", [])
    return {
        value.rsplit("/", 1)[-1]
        for value in (values if isinstance(values, list) else [values])
        if isinstance(value, str)
        and ("/" not in value or value.startswith(("https://schema.org/", "http://schema.org/")))
    }


def _json_objects(value, path: str = "$", *, depth: int = 0):
    # Retrieved responses already have byte limits. Bound nesting as well so
    # hostile structured data cannot exhaust the Python recursion stack.
    if depth > 20:
        return
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _json_objects(child, f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_objects(child, f"{path}[{index}]", depth=depth + 1)


def _structured_nodes(page: object) -> list[tuple[str, dict, dict[str, dict]]]:
    roots = []
    captured = getattr(page, "captured_json", None)
    html = str(getattr(page, "html", "") or "")
    if captured is not None:
        roots.append(("json", captured))
    elif "json" in str(getattr(page, "content_type", "") or "").lower():
        try:
            roots.append(("json", json.loads(html)))
        except (ValueError, RecursionError):
            pass
    else:
        soup = BeautifulSoup(html, "html.parser")
        for index, script in enumerate(soup.find_all("script", type="application/ld+json")):
            try:
                roots.append((f"jsonld[{index}]", json.loads(script.get_text())))
            except (ValueError, RecursionError):
                continue
        for index, scope in enumerate(soup.find_all(attrs={"itemscope": True})):
            if "Person" not in _schema_types({"@type": scope.get("itemtype", "").split()}):
                continue

            def item_value(item, depth=0):
                if depth > 20:
                    return None
                if item.has_attr("itemscope"):
                    values = {"@type": item.get("itemtype", "").rsplit("/", 1)[-1]}
                    for child in item.find_all(attrs={"itemprop": True}):
                        if child.find_parent(attrs={"itemscope": True}) is item:
                            values[child["itemprop"]] = item_value(child, depth + 1)
                    return values
                return item.get("content") or item.get("href") or item.get_text(" ", strip=True)

            person = {"@type": "Person"}
            for item in scope.find_all(attrs={"itemprop": True}):
                if item.find_parent(attrs={"itemscope": True}) is scope:
                    key, value = item["itemprop"], item_value(item)
                    if key in person:
                        person[key] = (
                            [*person[key], value] if isinstance(person[key], list) else [person[key], value]
                        )
                    else:
                        person[key] = value
            roots.append((f"microdata[{index}]", person))
    nodes = []
    for origin, root in roots:
        objects = list(_json_objects(root))
        # Resolve only local graph references; never fetch JSON-LD contexts or IDs.
        references = {}
        conflicts = set()
        for _, node in objects:
            identifier = node.get("@id")
            if not isinstance(identifier, str) or identifier in conflicts:
                continue
            previous = references.get(identifier, {})
            if any(key in previous and previous[key] != value for key, value in node.items()):
                conflicts.add(identifier)
                references.pop(identifier, None)
            else:
                references[identifier] = previous | node
        nodes.extend(
            (f"{origin}{path}", node, references) for path, node in objects if "Person" in _schema_types(node)
        )
    return nodes


def _node_value(value, references: dict[str, dict]):
    if isinstance(value, dict) and isinstance(value.get("@id"), str):
        return references.get(value["@id"], value) | value
    return value


def _expand_references(value, references: dict[str, dict], *, depth=0, seen=frozenset()):
    if depth > 8:
        return value
    if isinstance(value, list):
        return [_expand_references(item, references, depth=depth + 1, seen=seen) for item in value]
    if isinstance(value, dict):
        identifier = value.get("@id")
        if isinstance(identifier, str) and identifier not in seen:
            value = _node_value(value, references)
            seen = seen | {identifier}
        return {
            key: _expand_references(item, references, depth=depth + 1, seen=seen)
            for key, item in value.items()
        }
    return value


def _literal_name(value, references: dict[str, dict]) -> str | None:
    value = _node_value(value, references)
    value = value.get("name") if isinstance(value, dict) else value
    return value if isinstance(value, str) and 0 < len(value.strip()) <= 500 else None


def structured_evidence(page: object, seed: object) -> list[tuple[str, ExtractionResponse]]:
    """Keep explicit Person properties auditable; ambiguous properties stay for the model.

    A response with no claims is retained as interpretation context. The source
    identity gate and ordinary validate_claims still apply to every factual claim.
    """
    full_name = str(
        seed.get("full_name", "") if isinstance(seed, Mapping) else getattr(seed, "full_name", "")
    )
    batches = []
    seen = set()
    nodes = _structured_nodes(page)
    signatures = {}
    ambiguous_name = False
    for _, person, references in nodes:
        if not isinstance(person.get("name"), str) or name_key(person["name"]) != name_key(full_name):
            continue
        for key in ("worksFor", "jobTitle", "alumniOf", "hasCredential"):
            items = person.get(key, [])
            items = items if isinstance(items, list) else [items]
            names = frozenset(name_key(value) for item in items if (value := _literal_name(item, references)))
            if names:
                if key in signatures and signatures[key] != names:
                    ambiguous_name = True
                signatures[key] = names
    for location, person, references in nodes:
        person = {key: value for key, value in person.items() if key in _PERSON_EVIDENCE_PROPERTIES}
        name = person.get("name")
        if not isinstance(name, str) or not name.strip() or not full_name:
            text = json.dumps(_expand_references(person, references), ensure_ascii=False, sort_keys=True)
            batches.append((text, ExtractionResponse()))
            continue
        if name_key(name) != name_key(full_name):
            # A different or abbreviated name cannot supply deterministic facts.
            # Keep it available for model-assisted identity interpretation.
            text = json.dumps(_expand_references(person, references), ensure_ascii=False, sort_keys=True)
            batches.append((text, ExtractionResponse()))
            continue
        remaining = dict(person)

        def emit(properties: dict, fields: list[tuple[ProfileField, str]], group: str):
            text = json.dumps(
                {"@type": "Person", "name": name, **properties}, ensure_ascii=False, sort_keys=True
            )
            if len(text) > 800 or not 2 <= len(name) <= 200 or any(value not in text for _, value in fields):
                return False
            if text in seen:
                # This projection already has claims. Keep any unrepresented
                # properties below without sending the same facts back to the model.
                return True
            seen.add(text)
            response = ExtractionResponse(
                claims=[
                    ExtractedClaim(
                        field=field,
                        value=value,
                        evidence=text,
                        subject_name=name,
                        evidence_location=f"{location}/{group}"[:200],
                        fact_group="structured:"
                        + hashlib.sha256(f"{location}|{group}".encode()).hexdigest()[:32],
                        source_claim_type="microdata_person"
                        if location.startswith("microdata")
                        else "jsonld_person",
                    )
                    for field, value in fields
                ]
            )
            batches.append((text, response))
            return True

        emit({}, [(ProfileField.full_name, name)], "name")
        if ambiguous_name:
            # Several same-name Person records with different explicit roles or
            # credentials can be namesakes or career changes. Keep all records
            # for interpretation instead of letting one page's anchor admit both.
            text = json.dumps(_expand_references(remaining, references), ensure_ascii=False, sort_keys=True)
            batches.append((text, ExtractionResponse()))
            continue
        employers = person.get("worksFor", [])
        employers = employers if isinstance(employers, list) else [employers]
        title = person.get("jobTitle")
        # A page with several organisations or titles needs relationship judgment.
        # Do not manufacture a Cartesian product from independent property arrays.
        if len(employers) == 1:
            employer = _literal_name(employers[0], references)
            employer_node = _node_value(employers[0], references)
            if employer and (
                not isinstance(employer_node, dict)
                or not _schema_types(employer_node)
                or _schema_types(employer_node) & _ORGANISATION_TYPES
            ):
                fields = [(ProfileField.organisation, employer)]
                properties = {"worksFor": {"name": employer}}
                if isinstance(title, str) and 0 < len(title.strip()) <= 500:
                    properties["jobTitle"] = title
                    fields.append((ProfileField.job_title, title))
                if emit(properties, fields, "worksFor"):
                    if not isinstance(employer_node, dict) or set(employer_node) <= {
                        "@type",
                        "@id",
                        "name",
                        "url",
                    }:
                        remaining.pop("worksFor", None)
                        if "jobTitle" in properties:
                            remaining.pop("jobTitle", None)
        elif not employers and isinstance(title, str) and 0 < len(title.strip()) <= 500:
            if emit({"jobTitle": title}, [(ProfileField.job_title, title)], "jobTitle"):
                remaining.pop("jobTitle", None)
        alumni = person.get("alumniOf", [])
        alumni = alumni if isinstance(alumni, list) else [alumni]
        alumni_handled = True
        for index, item in enumerate(alumni):
            institution = _literal_name(item, references)
            node = _node_value(item, references)
            # alumniOf accepts any Organisation; only an explicit educational
            # institution type supports the University field without interpretation.
            educational = isinstance(node, dict) and bool(
                _schema_types(node) & {"CollegeOrUniversity", "EducationalOrganization"}
            )
            if (
                not institution
                or not educational
                or not emit(
                    {"alumniOf": {"name": institution}},
                    [(ProfileField.university_name, institution)],
                    f"alumniOf[{index}]",
                )
            ):
                alumni_handled = False
            if isinstance(node, dict) and set(node) - {"@type", "@id", "name", "url"}:
                alumni_handled = False
        if alumni_handled:
            remaining.pop("alumniOf", None)
        credentials = person.get("hasCredential", [])
        credentials = credentials if isinstance(credentials, list) else [credentials]
        credentials_handled = True
        for index, item in enumerate(credentials):
            credential = _node_value(item, references)
            label = _literal_name(credential, references)
            if (
                not label
                or classify_education(label) != EducationKind.academic_degree
                or any(
                    candidate.reason_code == "GENERIC_DEGREE_DEFAULT"
                    for candidate in normalise_degree_candidates(label)
                )
                or (
                    isinstance(credential, dict)
                    and _schema_types(credential)
                    and not _schema_types(credential) & {"Credential", "EducationalOccupationalCredential"}
                )
            ):
                credentials_handled = False
                continue
            # Credential recognizers, teaching affiliations and alumni arrays do
            # not establish which university awarded a particular qualification.
            fields = [(ProfileField.degree_type, label)]
            properties = {"name": label}
            if isinstance(credential, dict):
                represented = {"@type", "@id", "name"}
                about = _node_value(credential.get("about"), references)
                subject = _literal_name(about, references)
                if subject and (isinstance(about, str) or _schema_types(about) <= {"DefinedTerm", "Thing"}):
                    fields.append((ProfileField.subject, subject))
                    properties["about"] = {"name": subject}
                    represented.add("about")
                issuers = [(key, credential[key]) for key in ("issuer", "issuedBy") if key in credential]
                if len(issuers) == 1:
                    issuer_key, issuer = issuers[0]
                    issuer = issuer[0] if isinstance(issuer, list) and len(issuer) == 1 else issuer
                    issuer = _node_value(issuer, references)
                    institution = _literal_name(issuer, references)
                    if (
                        isinstance(issuer, dict)
                        and institution
                        and _schema_types(issuer) & {"CollegeOrUniversity", "EducationalOrganization"}
                    ):
                        fields.append((ProfileField.university_name, institution))
                        properties[issuer_key] = {"name": institution}
                        if set(issuer) <= {"@type", "@id", "name", "url"}:
                            represented.add(issuer_key)
                if set(credential) - represented:
                    credentials_handled = False
            if not emit({"hasCredential": properties}, fields, f"hasCredential[{index}]"):
                credentials_handled = False
        if credentials_handled:
            remaining.pop("hasCredential", None)
        # Uninterpreted biography, multiple affiliations, dates and credential
        # relationships still reach the LLM, with their named owner retained.
        if any(key not in {"@type", "@id", "@context", "name"} for key in remaining):
            expanded = _expand_references(remaining, references)
            # Expanding the person's own @id can restore removed properties.
            # Retain other linked context while keeping those facts omitted.
            for key in person.keys() - remaining.keys():
                expanded.pop(key, None)
            text = json.dumps(expanded, ensure_ascii=False, sort_keys=True)
            if text not in seen:
                batches.append((text, ExtractionResponse()))
    return batches


def deterministic_evidence(page: object, seed: object) -> list[tuple[str, ExtractionResponse]]:
    return [(text, response) for text, response in structured_evidence(page, seed) if response.claims]


def extraction_chunk_key(
    source_id: str,
    chunk: str,
    *,
    person_id: str = "",
    source_url: str = "",
    model: str = "",
) -> str:
    """Bind applied evidence to its person, source, exact content and extraction rules."""
    envelope = [
        person_id,
        source_id,
        canonicalise_url(source_url) if source_url else "",
        chunk,
        model,
        EXTRACTION_PROMPT_VERSION,
        EXTRACTION_SYSTEM_PROMPT,
    ]
    return hashlib.sha256(json.dumps(envelope, ensure_ascii=True).encode("utf-8")).hexdigest()


async def extract_chunks(
    client,
    seed: PersonSeed,
    chunks: list[str],
    source_url: str,
    context: dict,
    semaphore: asyncio.Semaphore,
    concurrency: int,
    *,
    applied_chunks: set[str] | None = None,
) -> AsyncIterator[tuple[str, ExtractionResponse]]:
    if not source_policy_allows(source_url):
        return
    payloads = []
    model = getattr(getattr(client, "settings", None), "OPENROUTER_EXTRACTION_MODEL", "") or ""
    for index, chunk in enumerate(chunks):
        if (
            applied_chunks is not None
            and context.get("source_id")
            and extraction_chunk_key(
                context["source_id"],
                chunk,
                person_id=context.get("person_id", ""),
                source_url=source_url,
                model=model,
            )
            in applied_chunks
        ):
            count("extraction_cache_hits")
            metrics = getattr(client, "metrics", None)
            if metrics is not None:
                metrics.extraction_cache_hits += 1
            continue
        payloads.append(
            {
                "person": research_seed_payload(seed, purpose="extraction"),
                "source_url": source_url,
                "chunk_index": index,
                "content": chunk,
            }
        )
    # Near a cap, retain the serial budget/claim order. Parallelize only when
    # every remaining chunk plus all configured retries fits the reservation.
    width = (
        concurrency
        if client.can_parallel_complete(ExtractionResponse, EXTRACTION_SYSTEM_PROMPT, payloads, context)
        else 1
    )

    async def extract(payload: dict) -> ExtractionResponse:
        async with semaphore:
            count("extraction_chunks")
            with stage("extraction_ms"):
                response, _ = await client.complete(
                    ExtractionResponse,
                    "extraction",
                    EXTRACTION_SYSTEM_PROMPT,
                    payload,
                    context,
                    EXTRACTION_PROMPT_VERSION,
                )
                return response

    async with ordered_window(payloads, extract, width) as results:
        async for payload, outcome in results:
            yield payload["content"], outcome.unwrap()
