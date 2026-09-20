"""Shared domain contracts; models never assign numeric confidence."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class ProfileField(StrEnum):
    full_name = "full_name"
    organisation = "organisation"
    job_title = "job_title"
    university_name = "university_name"
    degree_type = "degree_type"
    subject = "subject"
    profile_link = "profile_link"


class SourceType(StrEnum):
    first_party = "first_party"
    government = "government"
    employer = "employer"
    university = "university"
    professional_body = "professional_body"
    publication = "publication"
    conference = "conference"
    directory = "directory"
    aggregator = "aggregator"
    social = "social"
    unknown = "unknown"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class PersonStatus(StrEnum):
    queued = "queued"
    researching = "researching"
    completed = "completed"
    failed = "failed"
    review_required = "review_required"
    cancelled = "cancelled"


class PersonSeed(Contract):
    full_name: str = Field(min_length=2, max_length=200)
    organisation: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    university_name: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    program_year: str | None = Field(default=None, max_length=20)
    known_attributes: dict[str, str] = Field(default_factory=dict, max_length=10)
    preferred_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=10)

    @field_validator("known_attributes")
    @classmethod
    def bounded_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 80 or len(v) > 200 for k, v in value.items()):
            raise ValueError("Identity attribute keys/values are too long")
        return value


class SourceCandidate(Contract):
    candidate_id: str = Field(default_factory=new_id)
    url: str
    title: str = ""
    snippet: str = ""
    domain: str = ""
    origin: str = "search"
    preferred_source: bool = False
    source_type: SourceType = SourceType.unknown
    relevance: str = "ambiguous"
    score: float = 0


class IdentityMatch(Contract):
    score: float = Field(default=0, ge=0, le=1)
    ambiguous: bool = True
    rejected: bool = False
    signals: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)


class SourceRecord(Contract):
    source_id: str = Field(default_factory=new_id)
    person_id: str
    requested_url: str
    final_url: str
    canonical_url: str
    domain: str
    title: str = ""
    source_type: SourceType = SourceType.unknown
    retrieval_method: str = "static"
    retrieved_at: datetime = Field(default_factory=utcnow)
    published_at: date | None = None
    authority_score: float = Field(default=0, ge=0, le=1)
    authority_components: dict[str, Any] = Field(default_factory=dict)
    identity: IdentityMatch = Field(default_factory=IdentityMatch)
    preferred_source: bool = False
    content_hash: str = ""
    content_type: str | None = None
    fetch_status: int = 0
    processing_status: str = "pending"
    error_code: str | None = None
    compacted_text: str | None = None
    raw_content: str | None = None


class ExtractedClaim(Contract):
    field: ProfileField
    value: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=3, max_length=800)
    evidence_location: str | None = Field(default=None, max_length=200)
    temporal_context: str | None = Field(default=None, max_length=200)
    as_of_date: date | None = None
    end_date: date | None = None
    is_current: bool | None = None
    directness: str = Field(default="explicit", pattern="^(explicit|implied|ambiguous)$")
    source_claim_type: str = Field(default="statement", max_length=80)
    # Links education/employment components from the same record, avoiding mixed degrees.
    fact_group: str | None = Field(default=None, max_length=100)
    subject_name: str = Field(min_length=2, max_length=200)


class ExtractionResponse(Contract):
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=60)


class EvidenceClaim(Contract):
    claim_id: str = Field(default_factory=new_id)
    person_id: str
    source_id: str
    field: ProfileField
    raw_value: str
    normalised_value: str
    normalisation_certainty: float = Field(default=1, ge=0, le=1)
    evidence_text: str
    evidence_location: str | None = None
    temporal_context: str | None = None
    as_of_date: date | None = None
    end_date: date | None = None
    is_current: bool | None = None
    directness: str = "explicit"
    source_claim_type: str = "statement"
    fact_group: str | None = None
    subject_name: str
    identity_relevance: float = Field(default=0, ge=0, le=1)
    extraction_model: str
    prompt_version: str = "claims-v1"
    created_at: datetime = Field(default_factory=utcnow)


class FieldDecision(Contract):
    value: str | None = None
    confidence: float = Field(default=0, ge=0, le=100)
    selected_claim_id: str | None = None
    supporting_claim_ids: list[str] = Field(default_factory=list)
    supporting_source_ids: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    conflicting_claim_ids: list[str] = Field(default_factory=list)
    alternative_claim_ids: list[str] = Field(default_factory=list)
    review_required: bool = True
    review_reason_codes: list[str] = Field(default_factory=list)
    scoring_components: dict[str, Any] = Field(default_factory=dict)


class ResearchMetrics(Contract):
    queries_performed: int = 0
    web_search_calls_reserved: int = 0
    web_search_requests: int = 0
    search_results_reserved: int = 0
    sources_discovered: int = 0
    sources_fetched: int = 0
    sources_accepted: int = 0
    llm_calls: int = 0
    tokens_used: int = 0
    tokens_budgeted: int = 0
    cost: float = 0
    stop_reason: str | None = None
    error_codes: list[str] = Field(default_factory=list)


class PersonProfile(Contract):
    person_id: str
    input_name: str
    status: PersonStatus = PersonStatus.review_required
    fields: dict[ProfileField, FieldDecision]
    profile_confidence: float = Field(ge=0, le=100)
    coverage: float = Field(ge=0, le=100)
    review_required: bool
    research_status: str = "completed"
    sources_considered: int = 0
    sources_used: int = 0
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime = Field(default_factory=utcnow)
    metrics: ResearchMetrics = Field(default_factory=ResearchMetrics)


class UsageRecord(Contract):
    usage_id: str = Field(default_factory=new_id)
    job_id: str
    person_id: str
    source_id: str | None = None
    role: str
    model: str
    prompt_version: str
    operation: str = "structured_completion"
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    web_search_requests: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    success: bool = True
    error_code: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    exception_type: str | None = None
    retry_attempt: int = Field(default=0, ge=0)
    request_sent: bool = False
    response_received: bool = False
    response_body_received: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class ResearchResult(Contract):
    profile: PersonProfile
    sources: list[SourceRecord] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)


class JobCreated(Contract):
    job_id: str
    status: JobStatus
    total_people: int


class JobView(JobCreated):
    counts: dict[str, int]
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class PersonResultView(Contract):
    person_id: str
    row_index: int
    original_row: dict[str, Any]
    status: PersonStatus
    error_code: str | None = None
    result: ResearchResult | None = None


class JobResults(Contract):
    job_id: str
    status: JobStatus
    people: list[PersonResultView]
