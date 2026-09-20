# KnownBy Backend

A general-purpose people research and profile enrichment backend. Submit a person or
a CSV/XLSX batch to discover public evidence, reconcile factual claims, and return
persisted profiles with provenance, explainable confidence, and separate coverage.

## Architecture

```text
Person / Batch → Durable job → OpenRouter discovery → Source validation
  → Cloudflare static retrieval / Playwright fallback → Markdown
  → Claim extraction → Reconciliation → Deterministic confidence
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
sources, the person finishes as `review_required` with a specific empty-result code
(`NO_SEARCH_CITATIONS`, `NO_ELIGIBLE_CANDIDATES`, or `NO_SELECTED_SOURCES`) instead
of being reported as an infrastructure failure.

## Core capabilities

- Single-person research and CSV/TSV/XLSX batch enrichment with original rows preserved.
- Iterative public-source discovery, identity checks, canonical URL deduplication,
  bounded retrieval, and within-job content caching.
- Seven profile fields: name, organisation, job title, university, degree, subject,
  and profile link, with claim-level evidence and source provenance.
- Deterministic field/profile confidence, alternatives, conflicts, and review reasons;
  coverage remains distinct from confidence. See [scoring rules](docs/confidence.md).
- Durable database jobs, separate workers, renewable leases, checkpoints, cancellation,
  bounded retries, and recorded model/tool usage with safe attempt diagnostics.
- Rich JSON results and CSV/XLSX exports with spreadsheet formula protection.

Trial runs use the production pipeline and settings. No dataset or sample-source
allowlist restricts discovery. Automated tests replace external calls with fixtures.

## Backend structure

| Path | Responsibility |
| --- | --- |
| `app/api/`, `app/schemas/` | FastAPI endpoints and validated contracts |
| `app/providers/`, `app/prompts/` | OpenRouter discovery, model roles, and prompts |
| `app/research/` | Budgets, orchestration, identity, evidence, and confidence |
| `app/retrieval/`, `app/processing/` | Safe acquisition, Markdown, and bounded chunks |
| `app/db/`, `alembic/` | Persistence, job leases, cache, and migrations |
| `app/export/`, `app/worker.py` | Batch input/output and background execution |
| `tests/` | Offline tests and optional PostgreSQL integration checks |

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

Search attempts cap server-tool execution to one call per request. The aggregate
result budget reserves requested slots, including retries with unknown usage.
Model calls share a conservative token reservation budget; this is not a dollar cap.
With `OPENROUTER_TEMPERATURE=0`, the adapter omits the parameter so reasoning models
that do not accept explicit temperature remain eligible under `require_parameters`;
positive configured values are forwarded.
Worker crash recovery is separately attempt-limited. Use plain model IDs and ensure
OpenRouter workspace settings permit Exa without forced legacy web plugins.

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
| GET | `/v1/jobs/{job_id}/export` | Export using `format=csv` or `format=xlsx` |
| POST | `/process` | Preserved static URL-to-Markdown interface |

## Status and limitations

Confidence is an evidence heuristic, not a calibrated probability; ambiguous identity
and conflicting facts require review. Public source availability and coverage vary.
Retrieval enforces public-network/redirect checks, byte limits, and timeouts.
Playwright handles eligible JavaScript shells only; no login, CAPTCHA bypass, or
browser account state is supported. The Cloudflare Worker remains separately managed.

Default tests spend no API credits. CI validates tests, PostgreSQL behavior, and the
Docker build; live provider compatibility and deployment still require runtime checks.
See [validation status](docs/validation.md) for recorded results and unverified checks.
