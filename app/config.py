"""Central limits and evidence weights. No provider keys belong in domain data."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas import Contract, ProfileField, SourceType


class ScoringPolicy(Contract):
    @model_validator(mode="before")
    @classmethod
    def merge_policy_maps(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for name in (
            "authority",
            "directness",
            "field_weights",
            "candidate_relevance_weights",
            "candidate_identity_weights",
        ):
            if isinstance(value.get(name), dict):
                defaults = cls.model_fields[name].default_factory()
                value[name] = {**defaults, **value[name]}
        return value

    authority: dict[SourceType, float] = Field(
        default_factory=lambda: {
            SourceType.first_party: 0.95,
            SourceType.government: 0.95,
            SourceType.employer: 0.90,
            SourceType.university: 0.90,
            SourceType.professional_body: 0.85,
            SourceType.publication: 0.80,
            SourceType.conference: 0.72,
            SourceType.directory: 0.65,
            SourceType.aggregator: 0.45,
            SourceType.social: 0.35,
            SourceType.unknown: 0.25,
        }
    )
    domain_overrides: dict[str, SourceType] = Field(default_factory=dict)
    directness: dict[str, float] = Field(
        default_factory=lambda: {
            "explicit": 1,
            "implied": 0.72,
            "ambiguous": 0.35,
        }
    )
    field_weights: dict[ProfileField, float] = Field(
        default_factory=lambda: {field: 1 for field in ProfileField}
    )
    authority_weight: float = Field(default=0.55, ge=0, le=1)
    directness_weight: float = Field(default=0.30, ge=0, le=1)
    recency_weight: float = Field(default=0.15, ge=0, le=1)
    corroboration_step: float = Field(default=0.08, ge=0, le=1)
    corroboration_cap: float = Field(default=0.20, ge=0, le=1)
    preferred_bonus: float = Field(default=0.02, ge=0, le=0.1)
    conflict_penalty: float = Field(default=0.25, ge=0, le=1)
    identity_minimum: float = Field(default=0.45, ge=0, le=1)
    identity_review_threshold: float = Field(default=0.80, ge=0, le=1)
    review_threshold: float = Field(default=75, ge=0, le=100)
    current_half_life_days: int = Field(default=730, ge=1)
    unknown_recency: float = Field(default=0.65, ge=0, le=1)
    historical_current_factor: float = Field(default=0.25, ge=0, le=1)
    identity_name_only: float = Field(default=0.55, ge=0, le=1)
    identity_clue_bonus: float = Field(default=0.18, ge=0, le=1)
    identity_mismatch_penalty: float = Field(default=0.30, ge=0, le=1)
    candidate_authority_weight: float = Field(default=0.45, ge=0, le=1)
    candidate_relevance_weight: float = Field(default=0.35, ge=0, le=1)
    candidate_identity_weight: float = Field(default=0.15, ge=0, le=1)
    candidate_preferred_weight: float = Field(default=0.05, ge=0, le=1)
    candidate_relevance_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "likely": 1.0,
            "ambiguous": 0.5,
            "unrelated": 0.0,
        }
    )
    candidate_identity_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "full_name": 0.55,
            "organisation": 0.14,
            "university_name": 0.14,
            "job_title": 0.08,
            "subject": 0.05,
            "country": 0.02,
            "location": 0.02,
            "known_attribute": 0.05,
            "known_attributes_cap": 0.2,
        }
    )

    @model_validator(mode="after")
    def validate_weights(self):
        if set(self.authority) != set(SourceType) or set(self.field_weights) != set(ProfileField):
            raise ValueError("Scoring maps must include all source types / profile fields")
        if set(self.directness) != {"explicit", "implied", "ambiguous"}:
            raise ValueError("Directness map must include explicit, implied, ambiguous")
        if any(not 0 <= x <= 1 for x in [*self.authority.values(), *self.directness.values()]):
            raise ValueError("Signal weights must be in [0, 1]")
        if any(x <= 0 for x in self.field_weights.values()):
            raise ValueError("Field weights must be positive")
        if abs(self.authority_weight + self.directness_weight + self.recency_weight - 1) > 0.00001:
            raise ValueError("Base scoring weights must sum to one")
        if (
            abs(
                self.candidate_authority_weight
                + self.candidate_relevance_weight
                + self.candidate_identity_weight
                + self.candidate_preferred_weight
                - 1
            )
            > 0.00001
        ):
            raise ValueError("Candidate scoring weights must sum to one")
        if any(
            not 0 <= x <= 1
            for x in [*self.candidate_relevance_weights.values(), *self.candidate_identity_weights.values()]
        ):
            raise ValueError("Candidate signals must be in [0, 1]")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )
    APP_ENV: Literal["development", "test", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    API_ACCESS_TOKEN: SecretStr | None = None
    DATABASE_URL: str = "sqlite:///./research.db"
    STATIC_FETCH_WORKER_URL: str | None = None
    STATIC_FETCH_WORKER_SECRET: SecretStr | None = None
    FETCH_TIMEOUT_SECONDS: float = Field(default=30, gt=0, le=120)
    MAX_RESPONSE_BYTES: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    MAX_REDIRECTS: int = Field(default=5, ge=0, le=10)
    PLAYWRIGHT_ENABLED: bool = True
    BROWSER_TIMEOUT_SECONDS: float = Field(default=30, gt=0, le=120)
    BROWSER_MAX_REQUESTS: int = Field(default=80, ge=1, le=500)
    BROWSER_MAX_TOTAL_BYTES: int = Field(default=8_000_000, ge=1024)
    BROWSER_SANDBOX: bool = True
    RETAIN_RAW_CONTENT: bool = False
    RETAIN_COMPACTED_TEXT: bool = True
    RETRIEVAL_CACHE_TTL_SECONDS: int = Field(default=3600, ge=60, le=86400)
    MAX_CACHE_BYTES_PER_JOB: int = Field(default=20000000, ge=1024, le=200000000)
    STATIC_MIN_READABLE_CHARS: int = Field(default=250, ge=0)
    MAX_STRUCTURED_PAGES: int = Field(default=5, ge=1, le=20)
    STRUCTURED_SOURCES: list[dict] = Field(default_factory=list, max_length=10)
    OPENROUTER_API_KEY: SecretStr | None = None
    OPENROUTER_SOURCE_MODEL: str | None = None
    OPENROUTER_EXTRACTION_MODEL: str | None = None
    OPENROUTER_TIMEOUT_SECONDS: float = Field(default=45, gt=0, le=180)
    OPENROUTER_MAX_RETRIES: int = Field(default=2, ge=0, le=4)
    OPENROUTER_TEMPERATURE: float = Field(default=0, ge=0, le=1)
    OPENROUTER_MAX_TOKENS: int = Field(default=3000, ge=100, le=16000)
    OPENROUTER_RESPONSE_FORMAT: Literal["json_schema", "json_object"] = "json_schema"
    SEARCH_PROVIDER: Literal["brave"] = "brave"
    BRAVE_SEARCH_API_KEY: SecretStr | None = None
    SEARCH_TIMEOUT_SECONDS: float = Field(default=20, gt=0, le=120)
    SEARCH_MIN_INTERVAL_SECONDS: float = Field(default=1.1, ge=0, le=60)
    MAX_SEARCH_QUERIES_PER_PERSON: int = Field(default=6, ge=1, le=30)
    MAX_SEARCH_RESULTS_PER_QUERY: int = Field(default=8, ge=1, le=20)
    MAX_SOURCES_PER_PERSON: int = Field(default=6, ge=1, le=30)
    SOURCES_PER_ROUND: int = Field(default=2, ge=1, le=10)
    MAX_CONCURRENT_PEOPLE: int = Field(default=3, ge=1, le=20)
    MAX_CONCURRENT_FETCHES: int = Field(default=4, ge=1, le=30)
    PER_DOMAIN_CONCURRENCY: int = Field(default=1, ge=1, le=5)
    MAX_MARKDOWN_CHARS: int = Field(default=24000, ge=100, le=200000)
    CHUNK_SIZE: int = Field(default=6000, ge=100, le=30000)
    CHUNK_OVERLAP: int = Field(default=400, ge=0)
    MAX_CHUNKS_PER_SOURCE: int = Field(default=3, ge=1, le=10)
    MAX_LLM_CALLS_PER_PERSON: int = Field(default=30, ge=1, le=200)
    MAX_TOKENS_PER_PERSON: int = Field(default=100000, ge=1000)
    BUDGET_MESSAGE_OVERHEAD_TOKENS: int = Field(default=1024, ge=128)
    TARGET_FIELD_CONFIDENCE: float = Field(default=85, ge=0, le=100)
    MAX_SOURCES_WITHOUT_NEW_CLAIMS: int = Field(default=3, ge=1)
    PERSON_TIMEOUT_SECONDS: float = Field(default=600, gt=0, le=3600)
    MAX_BATCH_ROWS: int = Field(default=100, ge=1, le=1000)
    MAX_UPLOAD_BYTES: int = Field(default=5_000_000, ge=1024, le=20_000_000)
    MAX_XLSX_UNCOMPRESSED_BYTES: int = Field(default=30_000_000, ge=1024)
    MAX_INPUT_COLUMNS: int = Field(default=100, ge=1, le=1000)
    WORKER_POLL_SECONDS: float = Field(default=2, gt=0, le=60)
    WORKER_LEASE_SECONDS: int = Field(default=120, ge=10)
    WORKER_HEARTBEAT_SECONDS: float = Field(default=20, gt=0)
    WORKER_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    SCORING: ScoringPolicy = Field(default_factory=ScoringPolicy)

    @model_validator(mode="after")
    def valid_settings(self):
        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if self.WORKER_HEARTBEAT_SECONDS * 2 >= self.WORKER_LEASE_SECONDS:
            raise ValueError("Worker lease must exceed twice its heartbeat interval")
        if self.STATIC_FETCH_WORKER_URL:
            parsed = urlsplit(self.STATIC_FETCH_WORKER_URL)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Worker URL must be an HTTP(S) URL without credentials")
            if self.APP_ENV == "production" and parsed.scheme != "https":
                raise ValueError("Production Worker URL must use HTTPS")
        if self.APP_ENV == "production":
            if not self.DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
                raise ValueError("Production requires PostgreSQL")
            if not self.API_ACCESS_TOKEN or not self.API_ACCESS_TOKEN.get_secret_value().strip():
                raise ValueError("Production requires API_ACCESS_TOKEN")
        return self

    def missing_services(self) -> list[str]:
        required = (
            "STATIC_FETCH_WORKER_URL",
            "STATIC_FETCH_WORKER_SECRET",
            "OPENROUTER_API_KEY",
            "OPENROUTER_SOURCE_MODEL",
            "OPENROUTER_EXTRACTION_MODEL",
            "BRAVE_SEARCH_API_KEY",
        )
        missing = []
        for name in required:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not value or not str(value).strip():
                missing.append(name)
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
