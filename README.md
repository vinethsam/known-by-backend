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
sources, the person ends with zero coverage and `research_status=insufficient_evidence`.
The task is unsuccessful rather than an ordinary completed profile, while diagnostics
retain `NO_SEARCH_CITATIONS`, `NO_ELIGIBLE_CANDIDATES`, or `NO_SELECTED_SOURCES`.
Provider and genuine retrieval failures remain separate retry/research failures with
their specific error codes.

## Core capabilities

- Single-person research and CSV/TSV/XLSX batch enrichment with original rows preserved.
  Batch headers are inferred deterministically across common space, snake-case,
  kebab-case, camelCase, and punctuation variants; only a usable person-name column
  is required. A name-only row follows the normal broad but bounded research path.
- Iterative public-source discovery, seed-aware identity gating, canonical URL
  deduplication, bounded static-to-browser recovery, and within-job content caching.
- Seven output field decisions: the validated input name plus six evidence-derived
  fields for current organisation, current job title, university, degree, subject,
  and a representative profile link. The input name keeps its identity with display
  casing normalized for output, carries
  deterministic 100% input certainty, and is excluded from research confidence and
  coverage; web identity assessment remains separate.
- Spreadsheet organisation, title, country/location, university, subject, year, and
  known attributes narrow initial searches, rank candidates, and constrain identity
  resolution. They are hypotheses to verify and never become claims, provenance,
  confidence, or final facts without retrieved evidence. Current-employer and
  government-office headers carry a stronger current-role hypothesis; alumni and
  former-employer headers remain useful identity/history clues without being assumed
  current. When the seed supplies an identity anchor, same-name sources that do not
  connect to it remain provisional. An explicit contradiction also remains provisional
  so reliable current evidence can correct a stale or inaccurate seed; it
  becomes field-eligible only through the bounded identity-corroboration rules.
- Source-list metadata such as `LIST_NAME`, `source_list`, `source_dataset`,
  `dataset_name`, `cohort`, and `source_cohort` stays passive. It is preserved exactly
  on every derived output row, including separate education records, but never enters
  queries, prompts, identity scoring, evidence selection, provenance, or confidence.
- Organisation and title stay paired. Freshness bands and a compact public-office
  taxonomy prioritize current primary, executive, academic, and government roles over
  stale employment, board/advisory work, parties, countries, and official buildings.
  An explicitly current undated role retains a full currentness signal. A strong
  unpaired current role can trigger one bounded official institution search before
  unresolved important fields receive one Wikipedia completion query within the
  existing search budget.
- Field-level supporting URLs, claim IDs, conflicts, confidence, and review reasons.
  Distinct supported education credentials produce separate records while shared
  identity and current-employment decisions repeat safely.
- Partial failures are contained at source, claim, or field scope. A failed page does
  not erase claims from successful pages; a rejected claim does not invalidate its
  grounded siblings; and a failed recovery attempt cannot remove evidence already
  accumulated. Reaching `MAX_SOURCES` finalizes whatever valid evidence is available.
  An unsupported extracted date is removed while the otherwise grounded claim remains.
- Central deterministic normalization keeps literal claims for provenance while
  exporting `Bachelor's Degree`, `Master's Degree`, `Doctoral Degree`, `Medical Degree`,
  `Law Degree`, `Diploma`, `Postgraduate Diploma`, or `Postgraduate Degree`. Subjects
  explicitly encoded in degree titles are split into the existing Subject field.
  Credentials split only with positive evidence such as different degree levels,
  qualification labels, dates, or materially distinct institutions. Institution
  location suffixes and alternate subject wording remain grouped when they do not
  prove another qualification. Reliable university-only evidence can remain a partial
  record with no invented degree; compatible quoted context can attach partial facts
  to one credential. Weak or ambiguous fragments, subject-only evidence, vague
  qualifications, certifications, honorary awards, ongoing study, postdoctoral work,
  and training stay in the evidence ledger without becoming invented earned-degree rows.
- Small exact-phrase mappings render familiar Spanish, French, and Portuguese titles,
  subjects, and institutional terms in English. Display casing preserves recognized
  acronyms, name particles, and internal capitals. Unknown proper names retain their
  grounded wording; raw input, claims, quotations, and source titles stay unchanged.
  These transformations add no model calls.
- Deterministic field/profile confidence, alternatives, conflicts, and review reasons;
  coverage remains distinct from confidence. The default field-review threshold is
  50%. Selected values below the separate 10% floor become null while their evidence,
  alternatives, provenance, and reason codes remain available. Record review is
  reserved for serious identity, relationship, grouping, contradiction, or
  representative-source issues. See [scoring rules](docs/confidence.md).
- Wikipedia is an explicit missing-field fallback when bounded first-party discovery
  remains incomplete. It stays deterministic `encyclopedia` evidence at authority
  `0.60`, below first-party, government, employer, university, and publication sources,
  so stronger official evidence wins conflicts.
- Durable database jobs, separate workers, renewable leases, checkpoints, cancellation,
  bounded retries, and recorded model/tool usage with safe attempt diagnostics.
- Rich JSON results and CSV/XLSX exports with spreadsheet formula protection. The
  default export stays compact; an additive [field-provenance mode](docs/exports.md)
  includes the supporting URLs for every selected field. XLSX marks only populated
  fields below the configured review threshold, and their confidence cells, in pale yellow.
- Human-readable CSV/XLSX filenames use the first nonempty source-list value and the
  job start time in UTC, such as `Forbes-2000_2026-09-29_0241.xlsx`; repeated exports
  retain that filename. A shared internal [results library](docs/library.md) retains
  completed exports only after an explicit save. All authorized team members can list,
  download, and delete saved files; job cleanup does not delete them.

Trial runs use the production pipeline and settings. No dataset or sample-source
allowlist restricts discovery. Automated tests replace external calls with fixtures.
The [quality recovery record](docs/quality-recovery.md) documents evidence-selection
regressions and their offline golden fixtures. Compact decision summaries appear at
INFO; DEBUG adds selection reasons that remain readable in plaintext Railway exports.

## Backend structure

| Path | Responsibility |
| --- | --- |
| `app/api/`, `app/schemas/` | FastAPI endpoints and validated contracts |
| `app/providers/`, `app/prompts/` | OpenRouter discovery, model roles, and prompts |
| `app/research/` | Budgets, orchestration, identity, evidence, and confidence |
| `app/research/concurrency.py`, `extraction.py`, `telemetry.py` | Bounded I/O, ordered extraction, and person performance logs |
| `app/retrieval/`, `app/processing/` | Safe acquisition, Markdown, and bounded chunks |
| `app/db/`, `alembic/` | Persistence, job leases, cache, and migrations |
| `app/library.py`, `app/db/library.py` | Shared file contracts and replaceable artifact storage |
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

The shared library adds migration `202609290001` for retained CSV/XLSX bytes and
minimal file metadata. Run `alembic upgrade head` in the web pre-deploy step and
redeploy web and worker from the same revision. Existing research tables and export
columns are unchanged. Back up saved files with the PostgreSQL database.

## Configuration

Environment-variable names are grouped below. [.env.example](.env.example) is the
complete reference for bounds, timeouts, concurrency, retention, and scoring policy.

- **Runtime and access:** `APP_ENV`, `PORT`, `LOG_LEVEL`, `API_ACCESS_TOKEN`, `DATABASE_URL`.
- **OpenRouter:** `OPENROUTER_API_KEY`, `OPENROUTER_SOURCE_MODEL`,
  `OPENROUTER_EXTRACTION_MODEL`, `OPENROUTER_RESPONSE_FORMAT`.
- **Static retrieval:** `STATIC_FETCH_WORKER_URL`, `STATIC_FETCH_WORKER_SECRET`,
  `STATIC_MIN_READABLE_CHARS` (default `250`).
- **Optional acquisition:** `PLAYWRIGHT_ENABLED`, `BROWSER_SANDBOX`, `STRUCTURED_SOURCES`.
- **Research bounds:** `MAX_SEARCH_QUERIES_PER_PERSON`, `MAX_SEARCH_RESULTS_PER_QUERY`,
  `MAX_TOTAL_SEARCH_RESULTS_PER_PERSON`, `MAX_SOURCE_MODEL_TOOL_CALLS`,
  `MAX_SOURCES_PER_PERSON`, `MAX_LLM_CALLS_PER_PERSON`, `MAX_TOKENS_PER_PERSON`,
  `PERSON_TIMEOUT_SECONDS`, `WORKER_MAX_ATTEMPTS`.
- **Concurrency:** `MAX_CONCURRENT_PEOPLE`, `MAX_CONCURRENT_FETCHES`,
  `PER_DOMAIN_CONCURRENCY`, `MAX_CONCURRENT_EXTRACTIONS` (default `2` per worker).
- **Shared library:** `LIBRARY_MAX_FILE_BYTES` (10 MB), `LIBRARY_MAX_TOTAL_BYTES`
  (250 MB), and `LIBRARY_MAX_FILES` (500); byte limits use decimal bytes.
- **Selection and review policy:** `SCORING__SELECTION_THRESHOLD` (default `10`) omits
  extremely weak selected values; `SCORING__REVIEW_THRESHOLD` (default `50`) keeps
  questionable populated values visible and reviewable.

Search attempts cap server-tool execution to one call per request. The aggregate
result budget reserves requested slots, including retries with unknown usage.
Model calls share a conservative token reservation budget; this is not a dollar cap.
With `OPENROUTER_TEMPERATURE=0`, the adapter omits the parameter so reasoning models
that do not accept explicit temperature remain eligible under `require_parameters`;
positive configured values are forwarded.
Worker crash recovery is separately attempt-limited. Use plain model IDs and ensure
OpenRouter workspace settings permit Exa without forced legacy web plugins.

LinkedIn and ordinary social-media hosts are excluded through the central source
policy before candidate admission and at retrieval and evidence boundaries. This
includes LinkedIn redirect/content hosts, Facebook, Instagram, X/Twitter, TikTok,
Threads, and Snapchat; Wikipedia remains permitted. The separately deployed static
Worker must enforce the same deny policy before each redirect hop.

Zero-coverage results are failed tasks and retain the stage that actually prevented a
usable profile: provider/extraction failure, genuine retrieval failure, grounding
failure, unresolved identity, no eligible evidence, or insufficient evidence. A
profile with one or more valid enrichment fields succeeds with limited coverage even
when another source failed. Structured attempt records and logs include
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
tool, result, token, source, and time budgets. The same bounded recovery queue is used
once when selected sources all fail retrieval or yield no identity-eligible grounded
claims; already failed URLs remain deduplicated.

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

Send the configured bearer token on research, library, and `/process` requests. The
library requires `API_ACCESS_TOKEN` even in development and is unavailable without it.
The existing single-token access model gives authorized team members the same library
permissions. API schemas are available at `/docs`; health and readiness are public.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health`, `/ready` | Liveness and readiness |
| POST | `/v1/research/person` | Queue one person; return job ID |
| POST | `/v1/research/batch` | Queue multipart CSV/TSV/XLSX upload |
| GET | `/v1/jobs/{job_id}` | Status and progress |
| GET | `/v1/jobs/{job_id}/results` | Profiles, evidence, and original rows |
| POST | `/v1/jobs/{job_id}/cancel` | Cancel outstanding work |
| GET | `/v1/jobs/{job_id}/export` | Export using `format=csv|xlsx`; add `provenance=field` for field source URLs |
| POST | `/v1/jobs/{job_id}/library` | Explicitly save a completed export; body `{"format":"xlsx","provenance":"none"}` |
| GET | `/v1/library/files` | List saved file metadata; bounded `limit` and `offset` |
| GET | `/v1/library/files/{file_id}` | Download retained CSV/XLSX bytes |
| DELETE | `/v1/library/files/{file_id}` | Delete a saved file and release its quota |
| POST | `/process` | Preserved static URL-to-Markdown interface |

Batch uploads are seed data and may use headers such as `alumni_full_name`,
`memberName`, `company`, `current_employer`, `alumni_organisation`, `former_employer`,
`government_office`, `role`, `country_of_origin`, or `university_name`. The importer
infers one mapping per file and passes recognized person context into the normal
research seed; no context field is required. Context improves discovery and namesake
resolution but remains unverified input until public evidence corroborates it. Newer,
stronger current evidence can therefore correct and replace a seeded
affiliation as the selected current organisation. The seed is retained only as an
identity/history clue unless retrieved evidence independently supports it. Passive
source-list metadata and unknown columns remain in the preserved input row and do not
influence research. Two otherwise identical people in different source lists remain
separate input rows and results. Equally strong name candidates return an explicit
ambiguity error; a missing-name error lists the observed headers. The optional legacy
`name_column` form field remains available as an explicit override.

## Status and limitations

Status and result responses include `status_label` and additive `outcome_counts` with
terminal attempted, successful, and unsuccessful people. A terminal job with successful
and failed records keeps its compatible `partial` code and displays **Completed with
issues**; completed records and progress counts remain available. See the
[partial-failure salvage report](docs/partial-failure-salvage.md) and
[quality recovery report](docs/quality-recovery.md) for the outcome rules, historical
comparison, golden outcomes, confidence changes, and internal decision logs.

Confidence is an evidence heuristic, not a calibrated probability; ambiguous identity
and conflicting facts require review. Public source availability and coverage vary.
Retrieval enforces public-network/redirect checks, byte limits, and timeouts.
Playwright handles eligible transient HTTP failures, challenges, empty/thin pages,
chrome-only pages, and JavaScript shells after static retrieval. It does not bypass
login or CAPTCHA controls and has no browser account state. The Cloudflare Worker
remains separately managed.

Default tests spend no API credits. CI validates tests, PostgreSQL behavior, and the
Docker build; live provider compatibility and deployment still require runtime checks.
See [validation status](docs/validation.md) for recorded results and unverified checks.
