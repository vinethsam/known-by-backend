"""Shared domain contracts; models never assign numeric confidence."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.input_validation import (
    MAX_INPUT_CELL_CHARS,
    MAX_INPUT_HEADER_CHARS,
    MAX_INPUT_URL_CHARS,
    normalized_input_text,
    sanitize_input_text,
    validate_input_text,
)


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
    country_of_origin = "country_of_origin"
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
    encyclopedia = "encyclopedia"
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

    @property
    def label(self) -> str:
        return "Completed with issues" if self == JobStatus.partial else self.value.capitalize()


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
    country_of_origin: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    university_name: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    degree_type: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=200)
    historical_employer: str | None = Field(default=None, max_length=200)
    historical_job_title: str | None = Field(default=None, max_length=200)
    profile_link: str | None = Field(default=None, max_length=MAX_INPUT_URL_CHARS)
    program_year: str | None = Field(default=None, max_length=20)
    known_attributes: dict[str, str] = Field(default_factory=dict, max_length=100)
    # Exact supplied text is separate from normalized, bounded research context.
    input_metadata: dict[
        Annotated[str, StringConstraints(strip_whitespace=False)],
        Annotated[str, StringConstraints(strip_whitespace=False)],
    ] = Field(default_factory=dict, max_length=1000)
    input_field_map: dict[Annotated[str, StringConstraints(strip_whitespace=False)], str] = Field(
        default_factory=dict, max_length=1000
    )
    preferred_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def capture_supplied_values(cls, value):
        if isinstance(value, dict) and isinstance(value.get("input_metadata", {}), dict):
            from app.seed_context import classify_seed_label

            value = dict(value)
            supplied = dict(value.get("input_metadata", {}))
            field_map = value.get("input_field_map", {})
            field_map = field_map if isinstance(field_map, dict) else {}

            def represented(key, item):
                meaning = classify_seed_label(key)
                aliases = {meaning} if meaning else set()
                if meaning == "organisation":
                    aliases.update({"current_employer", "government_office", "organisation_affiliation"})
                for label, raw in supplied.items():
                    if not isinstance(label, str) or not isinstance(raw, str):
                        continue
                    original_meaning = field_map.get(label, classify_seed_label(label))
                    if (
                        label == key or (isinstance(original_meaning, str) and original_meaning in aliases)
                    ) and sanitize_input_text(raw).strip() == sanitize_input_text(item).strip():
                        return True
                return False

            def retain(key, item):
                if represented(key, item):
                    return
                target = key
                while target in supplied:
                    target = "known:" + target
                supplied[target] = item

            for key, item in value.items():
                if (
                    key in cls.model_fields
                    and isinstance(item, str)
                    and key not in {"input_metadata", "input_field_map"}
                ):
                    retain(key, item)
            known = value.get("known_attributes")
            for key, item in known.items() if isinstance(known, dict) else ():
                if isinstance(key, str) and isinstance(item, str):
                    retain(key, item)
            if isinstance(value.get("preferred_urls"), (list, tuple)):
                for index, url in enumerate(value["preferred_urls"], start=1):
                    key = f"preferred_url_{index}"
                    raw_url = str(url)
                    # Persisted URLs are normalized by AnyHttpUrl. Match them
                    # back to their original text rather than replacing that
                    # text (or adding a new value on every model round trip).
                    matched = False
                    for label, raw in supplied.items():
                        if not isinstance(label, str) or not isinstance(raw, str):
                            continue
                        while label.startswith("known:"):
                            label = label.removeprefix("known:")
                        if label != key:
                            continue
                        try:
                            if AnyHttpUrl(raw) == AnyHttpUrl(raw_url):
                                matched = True
                                break
                        except (TypeError, ValueError):
                            continue
                    if not matched:
                        retain(key, raw_url)
            value["input_metadata"] = supplied
        return value

    @field_validator(
        "full_name",
        "organisation",
        "country",
        "country_of_origin",
        "location",
        "university_name",
        "job_title",
        "subject",
        "degree_type",
        "industry",
        "historical_employer",
        "historical_job_title",
        "program_year",
        mode="before",
    )
    @classmethod
    def safe_identity_text(cls, value):
        if isinstance(value, str):
            return normalized_input_text(value, max_length=200)
        return value

    @field_validator("known_attributes", mode="before")
    @classmethod
    def bounded_attributes(cls, value):
        if not isinstance(value, dict):
            return value
        result = {}
        for key, attribute in value.items():
            if isinstance(key, str):
                key = normalized_input_text(key, max_length=80).strip()
            if isinstance(attribute, str):
                attribute = normalized_input_text(attribute, max_length=200)
            if key in result:
                raise ValueError("Identity attribute keys must be distinct after Unicode normalization")
            result[key] = attribute
        return result

    @field_validator("input_metadata", "input_field_map", mode="before")
    @classmethod
    def preserve_input_metadata(cls, value):
        if isinstance(value, dict):
            for key, attribute in value.items():
                if isinstance(key, str):
                    validate_input_text(key, max_length=MAX_INPUT_HEADER_CHARS, allow_controls=True)
                if isinstance(attribute, str):
                    # Original controls are safe inside JSON strings (escaped by
                    # serialization); only sanitized text enters processing.
                    validate_input_text(
                        attribute, max_length=MAX_INPUT_CELL_CHARS, multiline=True, allow_controls=True
                    )
        return value

    @field_validator("profile_link")
    @classmethod
    def safe_profile_link(cls, value):
        if value:
            validate_input_text(value, max_length=MAX_INPUT_URL_CHARS)
        return value

    @field_validator("preferred_urls", mode="before")
    @classmethod
    def bounded_url_text(cls, value):
        if isinstance(value, (list, tuple)):
            for url in value:
                if isinstance(url, (str, AnyHttpUrl)):
                    validate_input_text(str(url), max_length=MAX_INPUT_URL_CHARS)
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
    supplied_values: list[str] = Field(default_factory=list)
    input_confidence: float | None = Field(default=None, ge=0, le=100)
    research_confidence: float | None = Field(default=None, ge=0, le=100)
    provenance_status: str | None = None


LEGACY_RESEARCH_FIELDS = (
    ProfileField.organisation,
    ProfileField.job_title,
    ProfileField.university_name,
    ProfileField.degree_type,
    ProfileField.subject,
    ProfileField.profile_link,
)


def legacy_research_coverage(fields: dict[ProfileField, FieldDecision]) -> float:
    """Keep the original six-researched-field measure comparable across revisions."""

    return round(
        100
        * sum(bool(fields.get(field) and fields[field].value is not None) for field in LEGACY_RESEARCH_FIELDS)
        / len(LEGACY_RESEARCH_FIELDS),
        2,
    )


def _fill_profile_origin_and_coverage(profile: ProfileRecord | PersonProfile) -> None:
    """Adapt stored pre-origin profiles without changing their source decisions."""

    missing_origin = ProfileField.country_of_origin not in profile.fields
    profile.fields.setdefault(ProfileField.country_of_origin, FieldDecision())
    if profile.legacy_coverage is None:
        profile.legacy_coverage = legacy_research_coverage(profile.fields)
    if missing_origin:
        researched = (field for field in ProfileField if field != ProfileField.full_name)
        present = sum(
            bool(profile.fields.get(field) and profile.fields[field].value is not None)
            for field in researched
        )
        profile.coverage = round(100 * present / (len(ProfileField) - 1), 2)


class ProfileRecord(Contract):
    """One exportable record for a person, scoped to one education credential."""

    record_id: str = Field(min_length=1, max_length=240)
    fields: dict[ProfileField, FieldDecision]
    profile_confidence: float = Field(ge=0, le=100)
    coverage: float = Field(ge=0, le=100)
    review_required: bool
    status: PersonStatus = PersonStatus.review_required
    sources_used: int = 0
    review_reason_codes: list[str] = Field(default_factory=list)
    legacy_coverage: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def fill_legacy_field(self):
        _fill_profile_origin_and_coverage(self)
        return self


class ResearchMetrics(Contract):
    queries_performed: int = 0
    web_search_calls_reserved: int = 0
    web_search_requests: int = 0
    search_results_reserved: int = 0
    sources_discovered: int = 0
    sources_fetched: int = 0
    sources_accepted: int = 0
    claims_extracted: int = 0
    claims_rejected: int = 0
    llm_calls: int = 0
    tokens_used: int = 0
    tokens_budgeted: int = 0
    cost: float = 0
    stop_reason: str | None = None
    failure_reason: str | None = None
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
    # Additive multi-record output. ``fields`` remains the primary/legacy projection.
    records: list[ProfileRecord] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime = Field(default_factory=utcnow)
    metrics: ResearchMetrics = Field(default_factory=ResearchMetrics)
    legacy_coverage: float | None = Field(default=None, ge=0, le=100)
    seed_attributes: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_legacy_field(self):
        _fill_profile_origin_and_coverage(self)
        return self


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

    @computed_field
    @property
    def status_label(self) -> str:
        return self.status.label

    @computed_field
    @property
    def outcome_counts(self) -> dict[str, int]:
        successful = self.counts.get(PersonStatus.completed.value, 0) + self.counts.get(
            PersonStatus.review_required.value, 0
        )
        unsuccessful = self.counts.get(PersonStatus.failed.value, 0) + self.counts.get(
            PersonStatus.cancelled.value, 0
        )
        return {
            "attempted": successful + unsuccessful,
            "successful": successful,
            "unsuccessful": unsuccessful,
        }


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

    @computed_field
    @property
    def status_label(self) -> str:
        return self.status.label

    @computed_field
    @property
    def outcome_counts(self) -> dict[str, int]:
        successful = sum(
            person.status in {PersonStatus.completed, PersonStatus.review_required} for person in self.people
        )
        unsuccessful = sum(
            person.status in {PersonStatus.failed, PersonStatus.cancelled} for person in self.people
        )
        return {
            "attempted": successful + unsuccessful,
            "successful": successful,
            "unsuccessful": unsuccessful,
        }
