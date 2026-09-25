"""Bounded person research loop: discover -> evidence -> profile, never URL guessing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from contextlib import aclosing
from time import monotonic

from app.config import Settings
from app.processing import document_metadata, process_content
from app.providers.openrouter import OPENROUTER_SCHEMA_ERROR, OpenRouterError
from app.providers.search import SearchProviderError
from app.providers.source_advisor import SourceAdvisor
from app.research.budget import BudgetedModel, BudgetExceeded
from app.research.claims import deduplicate_claims, validate_claims
from app.research.concurrency import Outcome, ordered_window
from app.research.discovery import (
    build_queries,
    build_query,
    deduplicate_candidates,
    query_key,
    rank_candidates,
    source_authority,
)
from app.research.extraction import extract_chunks
from app.research.identity import assess_identity, effective_identity_scores
from app.research.reconciliation import reconcile
from app.research.telemetry import count, person_performance, stage
from app.retrieval.service import FetchError, RetrievedPage
from app.retrieval.urls import canonicalise_url, domain_key
from app.schemas import (
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


def _process_source(
    source: SourceRecord,
    candidate: SourceCandidate,
    page: RetrievedPage,
    seed: PersonSeed,
    settings: Settings,
) -> list[str]:
    chunks = process_content(page, seed, settings)
    text = "\n\n".join(chunks)
    # A captured-JSON response may have no HTML. Hash its actual payload instead
    # of treating every such source as the same empty page.
    body = page.html
    if not body and page.captured_json is not None:
        body = json.dumps(page.captured_json, ensure_ascii=False, sort_keys=True)
    source.content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
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
    source.identity = assess_identity(seed, text, settings.SCORING, model_relevance=candidate.relevance)
    return chunks


class ResearchOrchestrator:
    def __init__(self, settings: Settings, search, model, retrieval):
        self.settings, self.search, self.model, self.retrieval = settings, search, model, retrieval
        self._extraction_slots = asyncio.Semaphore(settings.MAX_CONCURRENT_EXTRACTIONS)

    async def research(
        self, job_id: str, person_id: str, seed: PersonSeed, checkpoint=None
    ) -> ResearchResult:
        with person_performance(job_id, person_id):
            return await self._research(job_id, person_id, seed, checkpoint)

    async def _research(
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
        cached_profile = None
        evidence_changed = True
        checkpoint_lock = asyncio.Lock()

        def result():
            nonlocal cached_profile, evidence_changed
            if evidence_changed:
                with stage("reconciliation_ms"):
                    cached_profile = reconcile(person_id, seed, claims, sources, settings.SCORING)
                evidence_changed = False
            profile = cached_profile.model_copy(deep=True)
            profile.metrics = metrics
            profile.started_at = started
            profile.completed_at = utcnow()
            profile.sources_considered = metrics.sources_discovered
            profile.research_status = "partial" if metrics.error_codes else "completed"
            return ResearchResult(profile=profile, sources=sources, claims=claims, usage=usage)

        async def save():
            if checkpoint is not None:
                async with checkpoint_lock:
                    # The store runs in a thread. Freeze mutable usage/evidence while
                    # other requests finish; sessions never cross network awaits.
                    current = result().model_copy(deep=True)
                    current.profile.research_status = "researching"
                    await checkpoint(current)

        client.checkpoint = save

        def stopped(*, include_target: bool = True):
            if metrics.sources_fetched >= settings.MAX_SOURCES_PER_PERSON:
                metrics.stop_reason = "MAX_SOURCES"
                return True
            if no_new_claims >= settings.MAX_SOURCES_WITHOUT_NEW_CLAIMS:
                metrics.stop_reason = "NO_NEW_CLAIMS"
                return True
            if include_target:
                profile = result().profile
                record_fields = [record.fields for record in profile.records] or [profile.fields]
                if all(
                    all(
                        decision.value is not None
                        and decision.confidence >= settings.TARGET_FIELD_CONFIDENCE
                        and not decision.review_required
                        for decision in fields.values()
                    )
                    for fields in record_fields
                ):
                    metrics.stop_reason = "TARGET_CONFIDENCE"
                    return True
            return False

        async def process_candidate(candidate, page=None):
            nonlocal claims, no_new_claims, evidence_changed
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
            evidence_changed = True
            before = len(claims)
            timer = monotonic()
            try:
                if isinstance(page, Outcome):
                    page = page.unwrap()
                if page is None:
                    with stage("retrieval_ms"):
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
                with stage("processing_ms"):
                    chunks = _process_source(source, candidate, page, seed, settings)
                source.processing_status = "processed"
                evidence_changed = True
                await save()
                if source.content_hash in seen_hashes:
                    source.processing_status = "duplicate"
                    return
                seen_hashes.add(source.content_hash)
                if source.identity.rejected or source.identity.score < settings.SCORING.identity_minimum:
                    source.processing_status = "identity_rejected"
                    return
                metrics.sources_accepted += 1
                async with aclosing(
                    extract_chunks(
                        client,
                        seed,
                        chunks,
                        page.final_url,
                        dict(context, source_id=source.source_id),
                        self._extraction_slots,
                        settings.MAX_CONCURRENT_EXTRACTIONS,
                    )
                ) as extractions:
                    async for chunk, response in extractions:
                        extracted, reasons = validate_claims(
                            response,
                            seed,
                            source,
                            chunk,
                            settings.OPENROUTER_EXTRACTION_MODEL or "configured",
                        )
                        claims.extend(extracted)
                        evidence_changed = True
                        metrics.error_codes.extend(
                            code for code in reasons if code not in metrics.error_codes
                        )
                        await save()
                claims = deduplicate_claims(claims)
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
                evidence_changed = True
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
            previous_discovered = len(discovered_urls)
            discovered_urls.update(canonicalise_url(candidate.url) for candidate in candidates)
            count("candidates_discovered", len(discovered_urls) - previous_discovered)
            metrics.sources_discovered = len(discovered_urls)
            new_candidates = [c for c in candidates if c.url not in validated_candidates]
            by_id = {}
            if new_candidates:
                try:
                    with stage("source_validation_ms"):
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

            async def fetch(candidate):
                # An earlier redirect may identify a queued candidate before it
                # starts. Avoid fetching that now-known alias even in serial mode.
                if canonicalise_url(candidate.url) in seen_urls:
                    return None
                page = (preloaded or {}).get(candidate.url)
                if page is None:
                    with stage("retrieval_ms"):
                        page = await self.retrieval.retrieve(candidate.url, job_id=job_id)
                return page

            # Only I/O runs ahead. Evidence and stop decisions keep ranked source
            # order, and exiting the window cancels/awaits every speculative fetch.
            def fetch_capacity():
                # Once a discovery round has selected sources, consume that bounded
                # evidence set even if its first page fills the legacy primary record.
                # Hard source/no-new-evidence limits still stop work immediately.
                if stopped(include_target=False):
                    return 0
                return min(
                    settings.MAX_CONCURRENT_FETCHES, settings.MAX_SOURCES_PER_PERSON - metrics.sources_fetched
                )

            if selected and not stopped(include_target=False):
                # Recompute available slots after each source: skipped redirect
                # aliases must not discard later selected evidence from this round.
                async with ordered_window(selected, fetch, fetch_capacity) as fetched:
                    async for candidate, page in fetched:
                        if stopped(include_target=False):
                            break
                        count("candidates_selected")
                        pending_candidates.pop(candidate.url, None)
                        await process_candidate(candidate, page)
            return len(selected)

        try:
            # Operator-configured public structured datasets may serve the whole batch.
            for plan in settings.STRUCTURED_SOURCES:
                if stopped(include_target=False):
                    break
                try:
                    bounded_plan = dict(
                        plan,
                        max_pages=min(
                            settings.MAX_STRUCTURED_PAGES,
                            settings.MAX_SOURCES_PER_PERSON - metrics.sources_fetched,
                        ),
                    )
                    with stage("retrieval_ms"):
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
            if preferred and not stopped(include_target=False):
                await validate_and_process(preferred)
            if not stopped():
                # Start with the exact name and supplied clues. Pay for further planning
                # only after useful already-discovered candidates have been processed.
                queue = deque([build_query(seed)])
                last_planned_clues = None
                while not stopped(include_target=not bool(pending_candidates)):
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
                        identity_scores = effective_identity_scores(
                            claims, {source.source_id: source for source in sources}, settings.SCORING
                        )
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
                                and identity_scores.get(c.source_id, c.identity_relevance)
                                >= settings.SCORING.identity_review_threshold
                            )
                        )[:12]
                        if clues == last_planned_clues:
                            break
                        last_planned_clues = clues
                        profile = result().profile
                        record_fields = [record.fields for record in profile.records] or [profile.fields]
                        unresolved = [
                            field.value
                            for field in ProfileField
                            if any(
                                (decision := fields[field]).value is None
                                or decision.review_required
                                or decision.confidence < settings.TARGET_FIELD_CONFIDENCE
                                for fields in record_fields
                            )
                        ]
                        try:
                            with stage("discovery_ms"):
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
                    count("search_queries")
                    try:
                        with stage("discovery_ms"):
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
