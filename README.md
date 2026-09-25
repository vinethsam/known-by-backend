# KnownBy Backend

A general-purpose people research and profile enrichment backend. Submit a person or
a CSV/XLSX batch to discover public evidence, reconcile factual claims, and return
persisted profiles with provenance, explainable confidence, and separate coverage.

## Architecture

```text
Person / Batch → Durable job → OpenRouter discovery → Source validation
  → Cloudflare static retrieval / Playwright fallback → Markdown
  → Claim extraction → Deterministic normalization/classification
  → Multi-source reconciliation → Deterministic confidence
  → Education-scoped records with field provenance
  → Persisted profile → JSON / CSV / XLSX
```

The source model plans searches and evaluates real candidates. The extraction model
reads processed evidence and returns schema-validated claims. Both roles use one
OpenRouter key and may use the same model ID. Choose a tool-compatible source model
and models supporting the configured structured JSON format. Before sending a strict
JSON schema, the adapter recursively removes Pydantic `default` annotations that are
not accepted by OpenRouter's strict structured-output validators; required fields and
`additionalProperties: false` remain enforced.

Discovery uses OpenRouter's `openrouter:web_search` server tool with its Exa engine.
Only search `url_citation` metadata supplies discovered URLs; ordinary model prose
cannot introduce candidates. Supplied preferred URLs and reviewed structured-source
configuration are also supported. Every retrieval target passes URL-safety checks.
Search uses existing OpenRouter credits and incurs tool charges in addition to model
tokens; no separate search account or key is required. The
[server-tool API is currently beta](https://openrouter.ai/docs/guides/features/server-tools/web-search).
When search returns no usable citations, or filtering/advisor selection leaves no
sources, the person finishes without human review and uses
`research_status=insufficient_evidence` plus `NO_SEARCH_CITATIONS`,
`NO_ELIGIBLE_CANDIDATES`, or `NO_SELECTED_SOURCES`. Provider and retrieval failures
remain separate retry/research failures with their specific error codes.

## Core capabilities

- Single-person research and CSV/TSV/XLSX batch enrichment with original rows preserved.
  Batch headers are inferred deterministically across common space, snake-case,
  kebab-case, camelCase, and punctuation variants; only a usable person-name column
  is required.
- Iterative public-source discovery, identity checks, canonical URL deduplication,
  bounded retrieval, and within-job content caching.
- Multi-source selection of seven profile fields: name, current organisation, current
  job title, university, degree, subject, and a representative profile link.
- Field-level supporting URLs, claim IDs, conflicts, confidence, and review reasons.
  Distinct supported education credentials produce separate records while shared
  identity and current-employment decisions repeat safely.
- Central deterministic normalization keeps literal claims for provenance while
  exporting controlled English degree levels, repaired Unicode, and conservatively
  formatted subject, institution, organisation, and title values. Certifications,
  honorary awards, ongoing study, postdoctoral work, and training stay in the evidence
  ledger without becoming earned-degree rows.
- Deterministic field/profile confidence, alternatives, conflicts, and review reasons;
  coverage remains distinct from confidence. The default field-review threshold is
  50%; record review is reserved for serious identity, relationship, grouping,
  contradiction, or representative-source issues. See [scoring rules](docs/confidence.md).
- Durable database jobs, separate workers, renewable leases, checkpoints, cancellation,
  bounded retries, and recorded model/tool usage with safe attempt diagnostics.
- Rich JSON results and CSV/XLSX exports with spreadsheet formula protection. The
  default export stays compact; an additive [field-provenance mode](docs/exports.md)
  includes the supporting URLs for every selected field. XLSX marks only populated
  fields below the configured review threshold, and their confidence cells, in pale yellow.

Trial runs use the production pipeline and settings. No dataset or sample-source
allowlist restricts discovery. Automated tests replace external calls with fixtures.

## Backend structure

| Path | Responsibility |
| --- | --- |
| `app/api/`, `app/schemas/` | FastAPI endpoints and validated contracts |
| `app/providers/`, `app/prompts/` | OpenRouter discovery, model roles, and prompts |
| `app/research/` | Budgets, orchestration, identity, evidence, and confidence |
| `app/research/concurrency.py`, `extraction.py`, `telemetry.py` | Bounded I/O, ordered extraction, and person performance logs |
| `app/retrieval/`, `app/processing/` | Safe acquisition, Markdown, and bounded chunks |
| `app/db/`, `alembic/` | Persistence, job leases, cache, and migrations |
| `app/export/`, `app/worker.py` | Batch input/output and background execution |
| `app/input_validation.py` | Shared text bounds and Unicode/control validation |
| `tests/` | Offline tests and optional PostgreSQL integration checks |
| `benchmarks/` | Offline production-pipeline benchmarks using mocked providers |

Shared interfaces and persistence rules are in [architecture contracts](docs/architecture.md).

## Deployment

The intended deployment is **GitHub → Railway web + worker + PostgreSQL**, connected
to the existing deployed Cloudflare Worker and OpenRouter.

Build both services from this repository's Dockerfile. It installs matching Chromium
and system dependencies and runs as a non-root user. Both services share PostgreSQL
and research configuration; keep secrets in Railway Variables.

| Service | Command / check |
| --- | --- |
| Web | `python -m app.serve`; binds Railway's `PORT` |
| Worker | `python worker.py`; continuously running, no public domain required |
| Migration owner: web pre-deploy | `python -m alembic upgrade head` |
| Web deployment healthcheck | `/health` |
| Full backend readiness | `/ready` |

Readiness checks database schema, configuration, worker heartbeat, and browser
installation. It does not make paid provider calls or prove Chromium can launch.
Verify the existing Cloudflare Worker's upstream URL/redirect protections and
Chromium sandbox support on the target runtime.

The multi-source/education-record contract is stored in the existing profile JSON;
it adds no Alembic migration. Keep the normal `alembic upgrade head` pre-deploy step
and redeploy the web and worker from the same revision.

## Configuration

Environment-variable names are grouped below. [.env.example](.env.example) is the
complete reference for bounds, timeouts, concurrency, retention, and scoring policy.

- **Runtime and access:** `APP_ENV`, `PORT`, `LOG_LEVEL`, `API_ACCESS_TOKEN`, `DATABASE_URL`.
- **OpenRouter:** `OPENROUTER_API_KEY`, `OPENROUTER_SOURCE_MODEL`,
  `OPENROUTER_EXTRACTION_MODEL`, `OPENROUTER_RESPONSE_FORMAT`.
- **Static retrieval:** `STATIC_FETCH_WORKER_URL`, `STATIC_FETCH_WORKER_SECRET`.
- **Optional acquisition:** `PLAYWRIGHT_ENABLED`, `BROWSER_SANDBOX`, `STRUCTURED_SOURCES`.
- **Research bounds:** `MAX_SEARCH_QUERIES_PER_PERSON`, `MAX_SEARCH_RESULTS_PER_QUERY`,
  `MAX_TOTAL_SEARCH_RESULTS_PER_PERSON`, `MAX_SOURCE_MODEL_TOOL_CALLS`,
  `MAX_SOURCES_PER_PERSON`, `MAX_LLM_CALLS_PER_PERSON`, `MAX_TOKENS_PER_PERSON`,
  `PERSON_TIMEOUT_SECONDS`, `WORKER_MAX_ATTEMPTS`.
- **Concurrency:** `MAX_CONCURRENT_PEOPLE`, `MAX_CONCURRENT_FETCHES`,
  `PER_DOMAIN_CONCURRENCY`, `MAX_CONCURRENT_EXTRACTIONS` (default `2` per worker).
- **Review policy:** `SCORING__REVIEW_THRESHOLD` (default `50`). Remove or update an
  older Railway value such as `75` if the deployment should use the new default.

Search attempts cap server-tool execution to one call per request. The aggregate
result budget reserves requested slots, including retries with unknown usage.
Model calls share a conservative token reservation budget; this is not a dollar cap.
With `OPENROUTER_TEMPERATURE=0`, the adapter omits the parameter so reasoning models
that do not accept explicit temperature remain eligible under `require_parameters`;
positive configured values are forwarded.
Worker crash recovery is separately attempt-limited. Use plain model IDs and ensure
OpenRouter workspace settings permit Exa without forced legacy web plugins.

LinkedIn and LinkedIn-owned redirect/content hosts are excluded before source
validation and again at the network boundary. They never enter automated static or
browser retrieval, extraction, or evidence records. The separately deployed static
Worker must enforce the same deny policy before each redirect hop.

Provider, transport, retrieval, and structured-response failures that leave zero
coverage remain failed tasks. Their specific safe error code is stored on the terminal
task instead of being collapsed to a generic research failure; a profile with partial
evidence can still complete for review. Structured attempt records and logs include
the operation, HTTP status when available, exception class, retry number, and whether
a request, response, and response body were observed. Stage-boundary logs add
candidate, citation, and selected-source counts. Credentials and raw model bodies are
excluded.

The diagnostic fields are additive JSON fields and require no database migration.
Deploy or restart the web and worker services from the same revision so an older
process does not read attempt records written by the newer contract.

## Performance and input safety

Selected sources use bounded fetch overlap and are consumed in their ranked order.
Once a bounded discovery round selects a set, that set is consumed before a
confidence stop so later pages can corroborate fields or establish another credential.
Independent extraction chunks can overlap when their complete retry reservations fit
the person budget; otherwise they run serially. Canonical URL caching shares retrieved
public content within a job, while claims remain specific to each person. Existing
HTTP pools and the reusable Chromium process retain explicit shutdown and isolated
browser contexts.

If the fully constrained first search returns no citations, deterministic fallback
queries relax supplied organisation, education, geography, role, subject, year, and
known-attribute clues one at a time. They remain inside the existing per-person query,
tool, result, token, source, and time budgets.

Each person attempt emits one structured `person_performance` log containing stage
durations, cache/fetch counts, model attempts, tokens, and reported cost. See
[performance and tuning](docs/performance.md) for counter meanings, cancellation,
speculative-fetch tradeoffs, and the offline benchmark procedure.

Input validation preserves Unicode names and original spreadsheet text while bounding
cells, headers, and URLs. A narrow UTF-8-as-Latin-1/CP1252 repair fixes obvious mojibake
without transliterating or changing already-correct Unicode. XLSX archives are checked
before workbook parsing, including
entity/DTD rejection. CSV/XLSX formula escaping remains enabled. Model prompts keep
user fields and retrieved content inside a JSON data envelope; extraction has no tools
and every returned claim still passes deterministic grounding checks.

## API

Send the configured bearer token on research and `/process` requests. API schemas are
available at `/docs`; health and readiness are public.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health`, `/ready` | Liveness and readiness |
| POST | `/v1/research/person` | Queue one person; return job ID |
| POST | `/v1/research/batch` | Queue multipart CSV/TSV/XLSX upload |
| GET | `/v1/jobs/{job_id}` | Status and progress |
| GET | `/v1/jobs/{job_id}/results` | Profiles, evidence, and original rows |
| POST | `/v1/jobs/{job_id}/cancel` | Cancel outstanding work |
| GET | `/v1/jobs/{job_id}/export` | Export using `format=csv|xlsx`; add `provenance=field` for field source URLs |
| POST | `/process` | Preserved static URL-to-Markdown interface |

Batch uploads are seed data and may use headers such as `alumni_full_name`,
`memberName`, `company`, `government_office`, `role`, `country_of_origin`, or
`university_name`. The importer infers one mapping per file and passes recognized
context into the normal research seed. Unknown columns remain in the preserved input
row but do not influence research. Equally strong name candidates return an explicit
ambiguity error; a missing-name error lists the observed headers. The optional legacy
`name_column` form field remains available as an explicit override.

## Status and limitations

Confidence is an evidence heuristic, not a calibrated probability; ambiguous identity
and conflicting facts require review. Public source availability and coverage vary.
Retrieval enforces public-network/redirect checks, byte limits, and timeouts.
Playwright handles eligible JavaScript shells only; no login, CAPTCHA bypass, or
browser account state is supported. The Cloudflare Worker remains separately managed.

Default tests spend no API credits. CI validates tests, PostgreSQL behavior, and the
Docker build; live provider compatibility and deployment still require runtime checks.
See [validation status](docs/validation.md) for recorded results and unverified checks.
