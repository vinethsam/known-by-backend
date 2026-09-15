# People Research Backend

A general person research and enrichment engine. Submit a person or a CSV/XLSX batch;
the backend discovers public sources, preserves factual evidence, checks identity,
reconciles claims and returns profiles with deterministic field confidence. The product
name is provisional; domain models and configuration are deliberately generic.

The existing Cloudflare Worker → HTML → Markdown flow remains available at `/process`.
The main product API starts with people and durable research jobs.

## Architecture

```text
Person / CSV / XLSX batch
  → FastAPI: validate input and enqueue ResearchJob + PersonResearchTasks
  → PostgreSQL durable queue (SQLite for local development)
  → Separate worker: atomically lease a person task
  → Source discovery: Brave search + supplied URLs + configured public datasets
  → Model A: targeted queries, source classification and relevance decisions
  → Retrieval: structured data / Cloudflare static fetch / Playwright fallback
  → Conservative HTML → Markdown → relevant bounded chunks
  → Model B: structured evidence claims with literal excerpts
  → Identity checks / normalization / reconciliation
  → Deterministic field confidence and separate profile confidence / coverage
  → Persist sources, claims, decisions, profiles and model usage
  → Poll results / export CSV or XLSX
```

No Redis or Celery is needed at this scale. SQLAlchemy uses short synchronous database
transactions from thread pools; no session remains open across network awaits.
PostgreSQL locking and lease tokens fence concurrent workers and expired attempts.
Lease renewal keeps active work claimed; interrupted work becomes eligible after lease expiry.
Checkpoints before model attempts and after usage reports preserve reservations,
evidence and recorded usage across attempts. Research is bounded by
queries, sources, chunks, model attempts, token reservations, time and concurrency.

### Two runtime models

- **Model A**, `OPENROUTER_SOURCE_MODEL`: search planning and candidate judgments.
  Its decisions reference candidate IDs supplied by actual discovery. It cannot create
  factual source URLs or assign final profile fields.
- **Model B**, `OPENROUTER_EXTRACTION_MODEL`: concise structured claims from processed
  content. Pydantic validates each response and deterministic code checks the excerpts
  and raw values against the supplied text. Missing information stays missing.

Both roles use OpenRouter. Set model IDs yourself; none is hard-coded. Select models
supporting structured JSON output. `OPENROUTER_RESPONSE_FORMAT=json_schema` is the
default; `json_object` is available for compatible models without strict schema support.
Malformed JSON, empty output and schema failures have bounded retries. Prompt versions,
models, latency, tokens and provider-reported cost are retained. See
[OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs).

### Why Brave search

The initial adapter uses ordinary Brave Web Search: one HTTP endpoint with URL, title
and snippet results and an environment-configured key. This avoids a separate SDK and
keeps replacement behind the small `SearchProvider` protocol. The current integration
uses web results, not generated answers. Check your plan's rate and usage limits before
a batch; no pricing or free quota is assumed. See the
[Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search).

### Confidence and output

The seven output fields are `full_name`, `organisation`, `job_title`, `university_name`,
`degree_type`, `subject`, and `profile_link`. Each field has a selected value or null,
confidence 0–100, supporting sources/claims, alternatives/conflicts, review codes and
numeric scoring components. All original claims survive flat selection.

Confidence combines authority, identity, directness, independent corroboration, recency,
normalization and conflicts in Python. It is an evidence heuristic, not a calibrated
probability. **Profile confidence is the weighted mean of populated field confidences;
coverage is populated fields divided by seven.** Thus a highly reliable but incomplete
profile stays distinguishable from a complete weak one. The complete formula, weights,
selection rules and examples are in [docs/confidence.md](docs/confidence.md).

## File structure

| Path | Responsibility |
| --- | --- |
| `app/main.py`, `app/api/` | ASGI app, authenticated job APIs, request limits, health/readiness |
| `app/config.py`, `.env.example` | Central environment settings, limits and scoring policy |
| `app/schemas/` | Person seeds, source records, evidence claims, decisions, profiles and API contracts |
| `app/db/`, `alembic/` | SQLAlchemy persistence, durable leases, cache, explicit migrations |
| `app/providers/`, `app/prompts/` | Brave, OpenRouter, source adviser, compact versioned prompts |
| `app/research/` | Person orchestration, budgets, identity, grounding, normalization, reconciliation, confidence |
| `app/retrieval/` | URL safety, static Worker, guarded browser, generic structured acquisition |
| `app/processing/` | Conservative Markdown conversion and relevance-aware compaction/chunking |
| `app/export/` | CSV/TSV/XLSX ingestion and CSV/XLSX export |
| `app/worker.py`, `worker.py` | Background worker and command entrypoint |
| `tests/` | Offline unit/integration tests; optional PostgreSQL concurrency checks |
| `Dockerfile`, `.github/workflows/tests.yml` | Browser-capable container and test/build CI |
| Root `main.py`, `fetcher.py`, `processing.py`, `config.py`, `schemas.py` | Preserved compatibility interfaces |

## Local setup (Windows PowerShell)

Use Python 3.13 or 3.14. The Docker image uses Python 3.13. Work in this existing repository:

```powershell
cd C:\Vineth\KnownBy\knownby-backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m playwright install chromium
```

Edit `.env`. Put `OPENROUTER_API_KEY`, `BRAVE_SEARCH_API_KEY`, and
`STATIC_FETCH_WORKER_SECRET` there, plus both model IDs and the complete Worker endpoint.
Never put keys into Python files or commit `.env`. Process environment values override
`.env`. Run all commands from the repository root so settings and migrations are found.

On Linux/macOS use `python3 -m venv .venv` and `.venv/bin/python`. On Linux, browser
installation also needs system libraries:

```sh
.venv/bin/python -m playwright install --with-deps chromium
```

Playwright browsers must match the installed Python package; the Dockerfile installs
both during the same build. See [Playwright browser installation](https://playwright.dev/python/docs/browsers).

### Database and migrations

The default `DATABASE_URL=sqlite:///./research.db` is sufficient for local development.
For local PostgreSQL, create a database and put its connection URL in `.env`:
`postgresql+psycopg://user:password@localhost:5432/research`. Railway-provided
`postgres://` and `postgresql://` URLs are normalized to the psycopg driver.

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Alembic, not server startup, creates the schema. To develop a later migration, update
the ORM and run `python -m alembic revision --autogenerate -m "describe change"`, review
the generated operations, then upgrade. `python -m alembic downgrade -1` rolls back the
last migration and can remove data; use only on an appropriate development database.

Persisted tables include jobs, person tasks, sources, claims, field decisions, profiles,
usage, retrieval cache and worker heartbeats. JSON columns store validated domain
payloads; relational keys and indexes support job traversal and provenance. Source HTML
is not archived by default. `RETAIN_COMPACTED_TEXT` and `RETAIN_RAW_CONTENT` control source
record retention. A bounded **temporary raw retrieval cache** supports within-job reuse;
it expires using `RETRIEVAL_CACHE_TTL_SECONDS`, is removed when the job ends, and is capped
by `MAX_CACHE_BYTES_PER_JOB`. Expired records are cleaned by worker heartbeats.

### Run the two processes

Terminal 1 (web):

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Terminal 2 (worker):

```powershell
.\.venv\Scripts\python.exe worker.py
```

The alternative `python -m app.serve` binds `0.0.0.0` and reads `PORT`; this is the
container/Railway web command. The old `uvicorn main:app` import still works.

`/health` checks that the process is alive. `/ready` returns 200 only when database and
migrations are available, provider configuration is present, a fresh worker heartbeat
exists, and Chromium is installed when enabled. It does **not** spend API credits,
validate key balances or guarantee remote provider availability. A worker refuses to
start with missing required service settings. Job submission refuses missing database
schema or provider configuration with a useful 503 response.

## Existing Cloudflare Worker setup

The Worker implementation is outside this repository and was not replaced. It performs
generic static public retrieval. The backend sends the configured full endpoint:

```http
POST <STATIC_FETCH_WORKER_URL>
X-Worker-Secret: <STATIC_FETCH_WORKER_SECRET>
Content-Type: application/json

{"url":"https://example.com"}
```

Expected JSON: `requested_url`, `final_url`, integer HTTP `status`, optional `content_type`,
and string `html`. A discovered JSON source can use the same `html` body field with a
JSON content type. The backend does not append `/fetch` automatically.

In the **existing Worker project directory**, inspect the actual secret binding name
used by its `X-Worker-Secret` check. Do not create another Worker just for this backend.

1. Install that project's existing dependencies and authenticate Wrangler as needed.
2. Set its secret in an ignored `.dev.vars` file using the binding name the Worker reads.
   Set the identical value in backend `.env` as `STATIC_FETCH_WORKER_SECRET`.
3. Run `npx wrangler dev`. For the `/fetch` route, backend local configuration is
   `STATIC_FETCH_WORKER_URL=http://localhost:8787/fetch` (use the actual port/route).
4. Deploy the existing Worker with `npx wrangler deploy` when you are ready.
5. Set its deployed secret using `npx wrangler secret put ACTUAL_WORKER_BINDING_NAME`.
   If its binding is named `STATIC_FETCH_WORKER_SECRET`, use that exact name; the Worker
   code, not this repository, defines the binding name.
6. Copy the `workers.dev` URL from Wrangler output or Workers & Pages → your Worker →
   Settings → Domains & Routes. Set the complete deployed endpoint, for example
   `https://your-worker.your-subdomain.workers.dev/fetch`, in backend configuration.

Cloudflare documents [local/deployed secrets](https://developers.cloudflare.com/workers/configuration/secrets/)
and [Wrangler dev/deploy commands](https://developers.cloudflare.com/workers/wrangler/commands/workers/).

**Worker safety contract:** before exposing arbitrary research targets, confirm that
the existing Worker rejects non-public destinations and validates each redirect before
fetching it, with redirect, timeout, byte and content-type limits. Backend prechecks and
returned-final-URL validation cannot undo an unsafe redirect already followed remotely.
A local HTTP Worker endpoint is allowed in development; private/localhost *research
targets* remain blocked. Production Worker endpoints must use HTTPS.

## API and examples without a frontend

Interactive API contracts are at `http://127.0.0.1:8000/docs`. If `API_ACCESS_TOKEN` is
set, send `Authorization: Bearer <token>` on `/v1/*` and `/process`. Production requires
a token; local development may leave it blank. Health/readiness remain public.

| Method | Endpoint | Result |
| --- | --- | --- |
| GET | `/health` | Liveness |
| GET | `/ready` | Database/configuration/worker/browser readiness |
| POST | `/v1/research/person` | HTTP 202 with queued job ID |
| POST | `/v1/research/batch` | Multipart CSV/TSV/XLSX; HTTP 202 with job ID |
| GET | `/v1/jobs/{job_id}` | Status, counts and timestamps |
| GET | `/v1/jobs/{job_id}/results` | Original rows and rich profiles/evidence |
| POST | `/v1/jobs/{job_id}/cancel` | Cancel outstanding work and fence late results |
| GET | `/v1/jobs/{job_id}/export?format=csv` | Flat export with original columns |
| GET | `/v1/jobs/{job_id}/export?format=xlsx` | Excel export |
| POST | `/process` | Preserved development static URL → Markdown contract |

### Single person

With the default local blank access token:

```powershell
$body = @{
  full_name = "Jane Example"
  organisation = "Example Foundation"
  country = "Ghana"
} | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/research/person -ContentType application/json -Body $body
$job.job_id
Invoke-RestMethod "http://127.0.0.1:8000/v1/jobs/$($job.job_id)"
Invoke-RestMethod "http://127.0.0.1:8000/v1/jobs/$($job.job_id)/results"
```

The example name is fictional; use a real public professional seed for a live test.
Additional optional clues: `location`, `university_name`, `job_title`, `subject`,
`program_year`, a small explicit `known_attributes` map, and `preferred_urls`.
Preferred URLs are public-source hints, not instructions to trust their content.

For token-protected calls, add `-Headers @{Authorization="Bearer <your token>"}` to
PowerShell requests, or `-H "Authorization: Bearer <your token>"` to curl commands.
Do not put real tokens into versioned example files or shell history.

### Batch CSV/XLSX

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/research/batch -F "file=@examples/people.csv" -F "name_column=full_name"
curl.exe -X POST http://127.0.0.1:8000/v1/research/batch -F "file=@C:/path/to/people.xlsx" -F "name_column=Person Name"
```

Use `name_column` for an exact header match. Otherwise only `name`, `full_name`, and
`full name` are recognized; ambiguous names are rejected. CSV must be UTF-8; XLSX uses
the active sheet and treats formulas as literal text. Original columns and row order
are preserved. Recognized clue columns can populate the seed; unrelated columns are
kept without becoming model identity clues. Missing names, blank/duplicate headers,
extra unnamed cells, corrupt files and configured limits produce 422 or 413 errors.
Default batch limit is 100 people. Files do not need a frontend or a predefined sample
layout. `.xls` is unsupported; save it as `.xlsx` or CSV.

### Polling and exports

Job states: `queued`, `running`, `completed`, `partial`, `failed`, `cancelled`.
Person states: `queued`, `researching`, `completed`, `review_required`, `failed`,
`cancelled`. Review-required results count as successfully processed tasks; job
completion does not imply all fields are complete or verified. A mixture of successful
and failed/cancelled people is a partial job. Poll every few seconds; no WebSockets.

```powershell
curl.exe "http://127.0.0.1:8000/v1/jobs/JOB_ID/export?format=csv" -o results.csv
curl.exe "http://127.0.0.1:8000/v1/jobs/JOB_ID/export?format=xlsx" -o results.xlsx
```

Exports append snake_case enrichment names, seven field confidences, profile confidence,
coverage, review status, task status/error code and compact source URLs. Colliding input
headers get distinct enrichment suffixes. Full evidence remains in JSON. Formula-like
strings are escaped so spreadsheet software does not execute untrusted values. Exports
are allowed while a job runs; pending fields are blank. Poll results for the complete
supporting, conflicting and alternative claim objects.

## Trial runs use the production pipeline

Trial runs submit people through the same API, durable queue, worker, search provider,
retrieval, models, evidence validation and scoring used in production. There is no
separate testing source list, domain restriction switch or special trial parameter set.
Research can discover any eligible public source. Standard URL/SSRF and access-control
guards apply equally to every run.

Automated tests replace network providers with deterministic fixtures to avoid paid
API calls. They exercise the production modules; there is no alternative test research
implementation. A live trial uses your real credentials and the same .env/Railway
settings described above. Start with a small batch and inspect coverage and evidence.

## Retrieval, structured sources and limits

Static requests always use the existing Worker. Successful thin JavaScript shells can
fall back to Chromium; ordinary readable HTML and JSON do not render. Authentication,
CAPTCHA and access-denied pages are not bypassed. Static and rendered content share the
same Markdown/chunk pipeline. The renderer uses isolated contexts, guarded HTTP routes,
public-address-pinned connections and checked redirects. It blocks downloads, WebSockets,
service workers and non-read-only methods. No browser account/login state is supplied.

Known public structured endpoints can be configured by an operator through
`STRUCTURED_SOURCES`. No endpoint is inferred or invented for the Fellowship directory.
Examples for a **hypothetical endpoint you have actually inspected**:

```json
[
  {"mode":"filtered","url":"https://public.example.org/api/people","params":{"name":"$full_name"}},
  {"mode":"bulk","url":"https://public.example.org/api/directory","record_path":"people","filter_fields":["name"]},
  {"mode":"paginated","url":"https://public.example.org/api/directory","record_path":"people","page_param":"page","start_page":1,"max_pages":3}
]
```

Use one applicable plan rather than configuring all examples. Plans use the same Worker
GET-target contract. Bulk data is cached once within a job and locally filtered before
model extraction; person claims are never reused across people. Dynamic sources requiring
POST-based APIs or login are not automatically supported by this read-only renderer.
Add an explicit reviewed acquisition strategy if a real public source requires it.

Defaults: six queries, eight results/query, six selected sources/person, two sources per
round, three people concurrently, four page fetches concurrently, one page per domain,
three 6,000-character chunks/source, thirty model attempts/person, a 100,000-token
reservation ceiling and a ten-minute person deadline. Query planning refines searches
when new strong clues emerge. Model retry attempts share the person budget. Token
reservations use conservative UTF-8 sizes and output caps before each call; actual
reported tokens/cost are tracked separately. Unknown usage retains the reservation.
Missing or malformed usage fields retain the conservative reservation. Search retries
are bounded independently. Provider limits still apply across multiple
worker processes; start with one worker replica for the POC. Limits apply to each
worker attempt; crash recovery can run up to `WORKER_MAX_ATTEMPTS`. The durable usage
ledger preserves all recorded attempts, while profile metrics describe the latest attempt.

## Railway deployment

No deployment is performed by this repository change. Use the existing GitHub repository
when you decide to publish the reviewed changes. Railway detects the Dockerfile, which
installs Chromium/system libraries and runs as a non-root user. See
[Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles).

1. Create a Railway project and attach PostgreSQL.
2. Create **two services from the same backend repository**: web and worker. Use the
   repository root as build context and the supplied Dockerfile for each.
3. In both services' **Variables**, set `APP_ENV=production`, PostgreSQL `DATABASE_URL`,
   a strong `API_ACCESS_TOKEN`, both OpenRouter model IDs/key, Brave key, deployed Worker
   endpoint/secret. Share the same database and evidence policy.
   Keep production secrets in Railway Variables, never Docker build arguments or files.
4. Configure a single migration owner: run `python -m alembic upgrade head` as the web
   service pre-deploy command, or a deliberate one-off migration before starting either
   service. Do not run schema-changing migrations independently in concurrent replicas.
5. Set web start command to `python -m app.serve`. It reads Railway's `PORT` and binds
   `0.0.0.0`. Set web healthcheck path to `/health` so worker startup ordering does not
   prevent a healthy web deployment; inspect `/ready` for whole-service readiness.
6. Set worker start command to `python worker.py`. It needs no public domain or HTTP
   healthcheck. Keep it continuously running with an appropriate restart policy.
7. Generate a public domain for the web service only. Verify `/health`, then `/ready`.
8. Submit one real person, inspect evidence and cost, then try a small batch before 100.
9. Configure backups and retention appropriate to your public research data.

Chromium sandboxing defaults to enabled. Verify Chromium can launch on the target
Railway runtime. If its kernel restricts user namespaces, use a suitable isolated
browser runtime; `PLAYWRIGHT_ENABLED=false` is a static-only diagnostic mode.
`BROWSER_SANDBOX=false` is an explicit weaker isolation option, not an automatic fallback.
Playwright's [Docker guidance](https://playwright.dev/python/docs/docker) discusses
non-root browser isolation. Browser launch/system libraries must be smoke-tested on
the deployed environment; an installed binary alone does not prove its sandbox works.

See Railway's [healthchecks](https://docs.railway.com/deployments/healthchecks) and
[pre-deploy commands](https://docs.railway.com/deployments/pre-deploy-command) for platform configuration.

## Tests and validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q app main.py worker.py
```

Default tests prohibit external HTTP transports; they do not use real keys or spend
credits. They cover configuration, URL/SSRF guards, browser routing, processing,
source hierarchy, source/model response validation, identity, evidence normalization,
reconciliation, confidence, leases/recovery/cancellation, migrations,
batch/export, request contracts and the complete fake-provider worker pipeline.

SQLite migration upgrade/downgrade runs locally. PostgreSQL DDL and lock SQL can be
compiled offline. To run actual PostgreSQL concurrency tests, set `TEST_POSTGRES_URL`
to an **isolated test database** and run pytest; never point it at production. CI supplies
a disposable PostgreSQL service and builds the Docker image. These checks make no paid
API calls. Local validation details and remaining environment checks are recorded in
[docs/validation.md](docs/validation.md).

## Manual Setup Checklist

- [ ] Obtain an OpenRouter key and choose both runtime model IDs with JSON support.
- [ ] Obtain a Brave Search API key and check account rate/usage limits.
- [ ] Locate the existing Cloudflare Worker project and verify its secret binding and
      upstream URL/redirect/size protections.
- [ ] Run/deploy that Worker, copy its full endpoint and set matching secrets.
- [ ] Fill backend `.env` locally; install Chromium if browser fallback is enabled.
- [ ] Run database migrations and start both local processes.
- [ ] Submit one person and inspect provenance/confidence before a real batch.
- [ ] Supply and upload your real CSV/XLSX files; the included names are synthetic.
- [ ] For deployment, create Railway project/PostgreSQL and both services.
- [ ] Set Railway Variables, migration owner, start commands and healthcheck.
- [ ] Verify Chromium sandbox launch, provider credentials, Worker behavior and a full
      live job in the deployment environment.

## Troubleshooting and practical limits

| Symptom | Check |
| --- | --- |
| `/ready` returns 503 | Inspect its checks: migrate database, configure providers, start worker, install Chromium |
| Job stays queued | Worker must use the same database and have all required settings; inspect heartbeat/logs |
| Worker dies | It restarts/reclaims expired leases; attempts are capped. Inspect safe error codes |
| Worker timeout/502 | Verify full Worker endpoint, secret value, Worker deployment, returned JSON schema and public target |
| 401 from API | Send the configured bearer token; worker secret and API token are different secrets |
| 429 from provider | Reduce concurrency/rate, verify quota; retries and person budgets are bounded |
| Model schema failures | Choose JSON-capable models or documented json_object mode; inspect prompt version and provider errors |
| Empty/low confidence profiles | Check exact seed name/clues, authoritative source availability, grounding rejection and budgets |
| Old role not selected | Explicit historical facts are retained as alternatives; current output remains null if unsupported |
| XLSX rejected | Active sheet needs a unique header/name column and bounded rows/columns; use CSV to inspect layout |
| Browser blocked | No authentication/CAPTCHA bypass; verify public asset availability, GET-only behavior, runtime sandbox support |
| Linux browser launch fails | Reinstall package-matching Chromium and system libraries or use the supplied Docker build |

Logs are structured JSON with job/person/source IDs, stage, timing and model usage;
they exclude keys, Worker secrets, environment dumps and raw exception messages.
API failures return safe error messages instead of stack traces. Raw source text is
untrusted input to the models and never grants tools or execution privileges.

The POC does not implement account management, billing, frontend integration, login
automation, CAPTCHA bypass, an unlimited crawler or complete historical career modeling.
Exact-name identity and source independence are conservative heuristics requiring human
review where flagged. Live source availability and coverage are not guaranteed.
