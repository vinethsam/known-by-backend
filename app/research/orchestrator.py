"""Bounded person research loop: discover -> evidence -> profile, never URL guessing."""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from time import monotonic

from app.config import Settings
from app.processing import document_metadata, process_content
from app.prompts.extraction import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT
from app.providers.openrouter import OPENROUTER_SCHEMA_ERROR, OpenRouterError
from app.providers.search import SearchProviderError
from app.providers.source_advisor import SourceAdvisor
from app.research.budget import BudgetedModel, BudgetExceeded
from app.research.claims import deduplicate_claims, validate_claims
from app.research.discovery import (
    build_queries,
    build_query,
    deduplicate_candidates,
    query_key,
    rank_candidates,
    source_authority,
)
from app.research.identity import assess_identity, contains_phrase
from app.research.normalisation import comparison_key
from app.research.reconciliation import reconcile
from app.retrieval.service import FetchError
from app.retrieval.urls import canonicalise_url, domain_key
from app.schemas import (
    EvidenceClaim,
    ExtractionResponse,
    PersonSeed,
    ProfileField,
    ResearchMetrics,
    ResearchResult,
    SourceCandidate,
    SourceRecord,
    SourceType,
    utcnow,
)

logger = logging.getLogger(__name__)


def _source_advisor_error_code(exc: OpenRouterError) -> str:
    if exc.error_code == OPENROUTER_SCHEMA_ERROR:
        return "SOURCE_ADVISOR_VALIDATION_ERROR"
    return "SOURCE_ADVISOR_PROVIDER_ERROR"


class ResearchOrchestrator:
    def __init__(self, settings: Settings, search, model, retrieval):
        self.settings, self.search, self.model, self.retrieval = settings, search, model, retrieval

    async def research(
        self, job_id: str, person_id: str, seed: PersonSeed, checkpoint=None
    ) -> ResearchResult:
        settings = self.settings
        started = utcnow()
        metrics = ResearchMetrics()
        usage, sources, claims = [], [], []
        client = BudgetedModel(self.model, settings, metrics, usage)
        advisor = SourceAdvisor(client, max_queries=settings.MAX_SEARCH_QUERIES_PER_PERSON)
        context = {"job_id": job_id, "person_id": person_id}
        seen_urls, seen_hashes, queries_done, discovered_urls = set(), set(), set(), set()
        validated_candidates, pending_candidates = {}, {}
        rejected_urls = set()
        no_new_claims = 0
        citation_count = 0
        eligible_candidate_count = 0
        candidates_filtered_empty = False

        def result():
            profile = reconcile(person_id, seed, claims, sources, settings.SCORING)
            profile.metrics = metrics
            profile.started_at = started
            profile.completed_at = utcnow()
            profile.sources_considered = metrics.sources_discovered
            profile.research_status = "partial" if metrics.error_codes else "completed"
            return ResearchResult(profile=profile, sources=sources, claims=claims, usage=usage)

        async def save():
            if checkpoint is not None:
                current = result()
                current.profile.research_status = "researching"
                await checkpoint(current)

        client.checkpoint = save

        def stopped():
            if metrics.sources_fetched >= settings.MAX_SOURCES_PER_PERSON:
                metrics.stop_reason = "MAX_SOURCES"
                return True
            profile = result().profile
            if all(
                d.value is not None
                and d.confidence >= settings.TARGET_FIELD_CONFIDENCE
                and not d.review_required
                for d in profile.fields.values()
            ):
                metrics.stop_reason = "TARGET_CONFIDENCE"
                return True
            if no_new_claims >= settings.MAX_SOURCES_WITHOUT_NEW_CLAIMS:
                metrics.stop_reason = "NO_NEW_CLAIMS"
                return True
            return False

        async def process_candidate(candidate, page=None):
            nonlocal claims, no_new_claims
            canonical = canonicalise_url(candidate.url)
            if canonical in seen_urls:
                return
            seen_urls.add(canonical)
            metrics.sources_fetched += 1
            source = SourceRecord(
                person_id=person_id,
                requested_url=candidate.url,
                final_url=candidate.url,
                canonical_url=canonical,
                domain=domain_key(canonical),
                title=candidate.title,
                source_type=candidate.source_type,
                preferred_source=candidate.preferred_source,
            )
            sources.append(source)
            before = len(claims)
            timer = monotonic()
            try:
                if page is None:
                    page = await self.retrieval.retrieve(candidate.url, job_id=job_id)
                source.final_url = page.final_url
                source.canonical_url = canonicalise_url(page.final_url)
                seen_urls.add(source.canonical_url)
                source.domain = domain_key(page.final_url)
                source.fetch_status = page.status
                source.content_type = page.content_type
                source.retrieval_method = page.retrieval_method
                if not 200 <= page.status < 300:
                    raise FetchError("SOURCE_HTTP_ERROR")
                chunks = process_content(page, seed, settings)
                text = "\n\n".join(chunks)
                # Hash original source body (not person-specific chunks) to detect mirrors.
                source.content_hash = hashlib.sha256(page.html.encode("utf-8")).hexdigest()
                if settings.RETAIN_RAW_CONTENT:
                    source.raw_content = page.html
                source.compacted_text = text if settings.RETAIN_COMPACTED_TEXT else None
                if not page.content_type or "html" in page.content_type.lower():
                    title, source.published_at = document_metadata(page.html)
                    source.title = title or source.title
                source.authority_score, source.authority_components = source_authority(
                    candidate.source_type, page.final_url, settings.SCORING
                )
                source.source_type = SourceType(source.authority_components["source_type"])
                source.identity = assess_identity(
                    seed, text, settings.SCORING, model_relevance=candidate.relevance
                )
                source.processing_status = "processed"
                await save()
                if source.content_hash in seen_hashes:
                    source.processing_status = "duplicate"
                    return
                seen_hashes.add(source.content_hash)
                if source.identity.rejected or source.identity.score < settings.SCORING.identity_minimum:
                    source.processing_status = "identity_rejected"
                    return
                metrics.sources_accepted += 1
                for index, chunk in enumerate(chunks):
                    response, _ = await client.complete(
                        ExtractionResponse,
                        "extraction",
                        EXTRACTION_SYSTEM_PROMPT,
                        {
                            "person": seed.model_dump(mode="json"),
                            "source_url": page.final_url,
                            "chunk_index": index,
                            "content": chunk,
                        },
                        dict(context, source_id=source.source_id),
                        EXTRACTION_PROMPT_VERSION,
                    )
                    extracted, reasons = validate_claims(
                        response, seed, source, chunk, settings.OPENROUTER_EXTRACTION_MODEL or "configured"
                    )
                    claims.extend(extracted)
                    metrics.error_codes.extend(code for code in reasons if code not in metrics.error_codes)
                    await save()
                claims = deduplicate_claims(claims)
                # A link to a known, person-labelled profile is derived from the retrieved
                # URL. A generic listing URL is never promoted into a personal profile link.
                profile_types = {
                    SourceType.first_party,
                    SourceType.employer,
                    SourceType.university,
                    SourceType.professional_body,
                }
                if (
                    source.source_type in profile_types
                    and contains_phrase(comparison_key(source.title), comparison_key(seed.full_name))
                    and any(
                        c.source_id == source.source_id and c.field == ProfileField.full_name for c in claims
                    )
                ):
                    claims.append(
                        EvidenceClaim(
                            person_id=person_id,
                            source_id=source.source_id,
                            field=ProfileField.profile_link,
                            raw_value=page.final_url,
                            normalised_value=source.canonical_url,
                            evidence_text=source.title,
                            evidence_location="source_title",
                            subject_name=seed.full_name,
                            identity_relevance=source.identity.score,
                            extraction_model="deterministic:retrieved-profile-link",
                        )
                    )
                source.processing_status = "extracted"
            except (FetchError, ValueError) as exc:
                source.processing_status = "failed"
                source.error_code = "RETRIEVAL_FAILED" if isinstance(exc, FetchError) else "CONTENT_INVALID"
                metrics.error_codes.append(source.error_code)
            except OpenRouterError as exc:
                source.processing_status = "extraction_failed"
                source.error_code = "EXTRACTION_PROVIDER_FAILED"
                metrics.error_codes.append(source.error_code)
                logger.error(
                    "extraction_provider_failed",
                    extra=dict(
                        context,
                        source_id=source.source_id,
                        pipeline_stage="extraction",
                        operation=exc.operation or "extract_claims",
                        provider="openrouter",
                        model=settings.OPENROUTER_EXTRACTION_MODEL,
                        error_code=exc.error_code,
                        http_status=exc.http_status,
                        exception_type=exc.exception_type,
                        request_sent=exc.request_sent,
                        response_received=exc.response_received,
                        response_body_received=exc.response_body_received,
                    ),
                )
            finally:
                no_new_claims = no_new_claims + 1 if len(claims) == before else 0
                await save()
                logger.info(
                    "source_processed",
                    extra=dict(
                        context,
                        source_id=source.source_id,
                        domain=source.domain,
                        retrieval_method=source.retrieval_method,
                        pipeline_stage="extraction",
                        duration_ms=round((monotonic() - timer) * 1000),
                    ),
                )

        async def validate_and_process(candidates, preloaded=None, limit=None):
            nonlocal eligible_candidate_count, candidates_filtered_empty
            supplied_count = len(candidates)
            for url in set(pending_candidates) & (seen_urls | rejected_urls):
                pending_candidates.pop(url, None)
            candidates = [
                c
                for c in deduplicate_candidates(candidates, settings)
                if canonicalise_url(c.url) not in seen_urls | rejected_urls
            ]
            if not candidates:
                if supplied_count:
                    candidates_filtered_empty = True
                    logger.info(
                        "source_candidates_filtered",
                        extra=dict(
                            context,
                            pipeline_stage="source",
                            operation="filter_candidates",
                            error_code="NO_ELIGIBLE_CANDIDATES",
                            candidate_count=supplied_count,
                            selected_source_count=0,
                        ),
                    )
                return 0
            discovered_urls.update(canonicalise_url(candidate.url) for candidate in candidates)
            metrics.sources_discovered = len(discovered_urls)
            new_candidates = [c for c in candidates if c.url not in validated_candidates]
            by_id = {}
            if new_candidates:
                try:
                    decisions, _ = await advisor.validate(seed, new_candidates, context)
                except OpenRouterError as exc:
                    error_code = _source_advisor_error_code(exc)
                    logger.error(
                        "source_advisor_failed",
                        extra=dict(
                            context,
                            pipeline_stage="source",
                            operation=exc.operation or "validate_candidates",
                            provider="openrouter",
                            model=settings.OPENROUTER_SOURCE_MODEL,
                            error_code=error_code,
                            http_status=exc.http_status,
                            exception_type=exc.exception_type,
                            request_sent=exc.request_sent,
                            response_received=exc.response_received,
                            response_body_received=exc.response_body_received,
                            candidate_count=len(new_candidates),
                            citation_count=citation_count,
                            selected_source_count=0,
                        ),
                    )
                    raise
                by_id = {d.candidate_id: d for d in decisions}
            eligible = []
            for candidate in candidates:
                cached = validated_candidates.get(candidate.url)
                if cached is not None:
                    candidate = cached
                else:
                    decision = by_id[candidate.candidate_id]
                    if decision.relevance == "unrelated":
                        rejected_urls.add(candidate.url)
                        continue
                    # Duplicate suggestions remain advisory; equality removes evidence.
                    candidate.source_type = decision.source_type
                    candidate.relevance = decision.relevance
                    validated_candidates[candidate.url] = candidate
                eligible.append(candidate)
                if limit is not None:
                    pending_candidates[candidate.url] = candidate
            selected = rank_candidates(eligible, seed, settings, limit=limit)
            eligible_candidate_count += len(eligible)
            logger.info(
                "source_candidates_selected",
                extra=dict(
                    context,
                    pipeline_stage="source",
                    operation="validate_candidates",
                    candidate_count=len(candidates),
                    citation_count=citation_count,
                    selected_source_count=len(selected),
                    error_code="NO_SELECTED_SOURCES" if new_candidates and not eligible else None,
                ),
            )
            for candidate in selected:
                if stopped():
                    break
                pending_candidates.pop(candidate.url, None)
                await process_candidate(candidate, (preloaded or {}).get(candidate.url))
            return len(selected)

        try:
            # Operator-configured public structured datasets may serve the whole batch.
            for plan in settings.STRUCTURED_SOURCES:
                if stopped():
                    break
                try:
                    bounded_plan = dict(
                        plan,
                        max_pages=min(
                            settings.MAX_STRUCTURED_PAGES,
                            settings.MAX_SOURCES_PER_PERSON - metrics.sources_fetched,
                        ),
                    )
                    pages = await self.retrieval.retrieve_structured(bounded_plan, seed, job_id=job_id)
                    candidates = [
                        SourceCandidate(url=p.requested_url, origin="configured_structured") for p in pages
                    ]
                    await validate_and_process(candidates, {p.requested_url: p for p in pages})
                except FetchError:
                    metrics.error_codes.append("STRUCTURED_RETRIEVAL_FAILED")
            preferred = [
                SourceCandidate(url=str(url), preferred_source=True, origin="supplied")
                for url in seed.preferred_urls
            ]
            if preferred and not stopped():
                await validate_and_process(preferred)
            if not stopped():
                # Start with the exact name and supplied clues. Pay for further planning
                # only after useful already-discovered candidates have been processed.
                queue = deque([build_query(seed)])
                last_planned_clues = None
                while not stopped():
                    if pending_candidates:
                        await validate_and_process(
                            list(pending_candidates.values()), limit=settings.SOURCES_PER_ROUND
                        )
                        continue
                    if metrics.queries_performed >= settings.MAX_SEARCH_QUERIES_PER_PERSON:
                        metrics.stop_reason = "MAX_QUERIES"
                        break
                    if metrics.web_search_calls_reserved >= settings.MAX_SOURCE_MODEL_TOOL_CALLS:
                        metrics.stop_reason = "MAX_SEARCH_TOOL_CALLS"
                        break
                    remaining_results = (
                        settings.MAX_TOTAL_SEARCH_RESULTS_PER_PERSON - metrics.search_results_reserved
                    )
                    if remaining_results <= 0:
                        metrics.stop_reason = "MAX_SEARCH_RESULTS"
                        break
                    if not queue:
                        clues = tuple(
                            dict.fromkeys(
                                c.raw_value
                                for c in claims
                                if c.field
                                in {
                                    ProfileField.organisation,
                                    ProfileField.university_name,
                                    ProfileField.subject,
                                }
                                and c.identity_relevance >= settings.SCORING.identity_review_threshold
                            )
                        )[:12]
                        if clues == last_planned_clues:
                            break
                        last_planned_clues = clues
                        unresolved = [
                            field.value
                            for field, decision in result().profile.fields.items()
                            if decision.value is None
                            or decision.review_required
                            or decision.confidence < settings.TARGET_FIELD_CONFIDENCE
                        ]
                        try:
                            planned, _ = await advisor.plan(
                                seed, list(clues), dict(context, unresolved_fields=unresolved)
                            )
                        except OpenRouterError as exc:
                            error_code = _source_advisor_error_code(exc)
                            metrics.error_codes.append(error_code)
                            metrics.stop_reason = "PROVIDER_FAILED"
                            logger.error(
                                "source_advisor_failed",
                                extra=dict(
                                    context,
                                    pipeline_stage="source",
                                    operation=exc.operation or "plan_search_queries",
                                    provider="openrouter",
                                    model=settings.OPENROUTER_SOURCE_MODEL,
                                    error_code=error_code,
                                    http_status=exc.http_status,
                                    exception_type=exc.exception_type,
                                    request_sent=exc.request_sent,
                                    response_received=exc.response_received,
                                    response_body_received=exc.response_body_received,
                                    candidate_count=0,
                                    citation_count=citation_count,
                                    selected_source_count=0,
                                ),
                            )
                            break
                        queue.extend(
                            q
                            for q in build_queries(seed, settings, extra_queries=planned)
                            if query_key(q) not in queries_done
                        )
                        if not queue:
                            break
                    query = queue.popleft()
                    key = query_key(query)
                    if key in queries_done:
                        continue
                    queries_done.add(key)
                    metrics.queries_performed += 1
                    try:
                        candidates = await client.search(
                            self.search,
                            query,
                            min(settings.MAX_SEARCH_RESULTS_PER_QUERY, remaining_results),
                            context,
                        )
                    except SearchProviderError as exc:
                        metrics.error_codes.append(exc.error_code)
                        logger.error(
                            "web_search_failed",
                            extra=dict(
                                context,
                                pipeline_stage="source",
                                operation="web_search",
                                provider="openrouter",
                                model=settings.OPENROUTER_SOURCE_MODEL,
                                error_code=exc.error_code,
                                http_status=exc.http_status,
                                candidate_count=0,
                                citation_count=0,
                                selected_source_count=0,
                            ),
                        )
                        continue
                    citation_count += len(candidates)
                    logger.info(
                        "web_search_completed",
                        extra=dict(
                            context,
                            pipeline_stage="source",
                            operation="web_search",
                            provider="openrouter",
                            model=settings.OPENROUTER_SOURCE_MODEL,
                            candidate_count=len(candidates),
                            citation_count=len(candidates),
                        ),
                    )
                    if not candidates:
                        metrics.stop_reason = "NO_SEARCH_CITATIONS"
                        break
                    selected_count = await validate_and_process(candidates, limit=settings.SOURCES_PER_ROUND)
                    if selected_count == 0:
                        metrics.stop_reason = (
                            "NO_ELIGIBLE_CANDIDATES"
                            if candidates_filtered_empty and metrics.sources_discovered == 0
                            else "NO_SELECTED_SOURCES"
                        )
                        break
                if not metrics.stop_reason:
                    metrics.stop_reason = (
                        "MAX_QUERIES"
                        if metrics.queries_performed >= settings.MAX_SEARCH_QUERIES_PER_PERSON
                        else "DISCOVERY_EXHAUSTED"
                    )
        except BudgetExceeded as exc:
            metrics.stop_reason = str(exc)
        except OpenRouterError as exc:
            metrics.error_codes.append(_source_advisor_error_code(exc))
            metrics.stop_reason = "PROVIDER_FAILED"
        if not metrics.error_codes and not metrics.stop_reason:
            if metrics.queries_performed and citation_count == 0:
                metrics.stop_reason = "NO_SEARCH_CITATIONS"
            elif metrics.sources_discovered and eligible_candidate_count == 0:
                metrics.stop_reason = "NO_SELECTED_SOURCES"
            elif candidates_filtered_empty and not sources:
                metrics.stop_reason = "NO_ELIGIBLE_CANDIDATES"
        metrics.error_codes = sorted(set(metrics.error_codes))
        await save()
        return result()
