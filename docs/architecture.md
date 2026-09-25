# Architecture contracts

KnownBy starts with a person seed, not a target website. FastAPI validates input and
creates durable jobs; a separate async worker discovers, retrieves, and reconciles
public evidence. Trial runs use these same production modules and settings.

CSV/TSV/XLSX uploads are flexible seed schemas. After existing file, archive, row,
column, cell, Unicode, and control-character validation, the importer normalizes each
header once and applies a centralized deterministic alias/pattern table. A usable
person-name mapping is required; organisation, role, country/location, university,
subject, and program-year mappings populate existing `PersonSeed` clues. Unknown or
ambiguous optional columns are not guessed. Original rows and row indexes remain the
traceability mechanism. Spreadsheet values are seed clues only and never become web
evidence, provenance, or confidence inputs by themselves.

## Data and persistence

Shared contracts live in `app/schemas/__init__.py`. IDs are UUID strings, timestamps
are UTC, and JSON payloads use `model_dump(mode="json")`. Every profile has seven
field decisions, including explicit missing values. Raw claims and source provenance
survive flat field selection. Identity is assessed separately from source authority;
final field/profile confidence is deterministic, with coverage reported separately.

Reconciliation is multi-source. A field decision can cite supporting claims and URLs
from several independent sources. Distinct supported education credentials become
separate `ProfileRecord` objects; shared name and selected current-employment decisions
repeat on each record, while education claims stay scoped to their credential. The
existing flat `PersonProfile.fields` is the deterministic primary-record projection.
This additive record list is stored in the existing profile JSON, so old profile rows
remain readable and no database migration is required.

PostgreSQL is the production database; SQLite supports development. SQLAlchemy
transactions are short and synchronous, dispatched through thread pools during async
work; no session spans a network await. Alembic owns schema changes. Worker leases,
renewals, and fencing tokens prevent expired or cancelled attempts from publishing
late results. Checkpoints retain evidence and recorded usage.

Completed job results use six batched reads independent of the number of people;
source bodies are not fetched through a per-person query loop. Evidence and usage
ledger writes use batched upserts within each short checkpoint transaction. Field
decisions are inserted together. Lease fencing and durable checkpoint boundaries
remain in place; concurrent requests never share a SQLAlchemy session.

## Discovery and models

A small discovery interface isolates research orchestration from OpenRouter HTTP.
The source role plans bounded queries and evaluates existing candidate IDs. Its web
discovery requests use the `openrouter:web_search` server tool with the Exa engine,
`max_uses=1`, and top-level `max_tool_calls=1`. This uses the existing OpenRouter key
and credits; tool charges are additional to model tokens. Both model roles may share
one model ID, while extraction remains a separate structured request.

Structured requests use OpenRouter's `response_format.type=json_schema` contract.
The adapter derives schemas from Pydantic, recursively removes generated `default`
annotations that OpenRouter's strict validators do not accept, and retains the strict
object requirements: every property is required and every object sets
`additionalProperties: false`. Runtime Pydantic validation remains the final contract
check for a returned payload.

Only valid `url_citation` annotations from search responses become discovered
candidates. Model-prose URLs never enter the candidate pool. Preferred seed URLs and
operator-reviewed structured endpoints are separate eligible inputs. Canonical URLs
and equivalent queries are deduplicated before repeated work. The first query uses
the exact person name and supplied clues. Candidate decisions are reused within a
person attempt, and pending candidates are processed before paying for another
search. Follow-up planning runs only when unresolved fields justify it.

Every source selected in the current bounded discovery round is consumed before a
target-confidence stop. This allows corroboration and additional credentials without
changing the concurrent fetch window, extraction semaphore, budgets or source caps.

LinkedIn and its redirect/content host trees are excluded by a shared hostname policy.
The policy runs while parsing search citations, while admitting supplied candidates,
and again at the network boundary. Exact hosts and subdomains are blocked; hostname
lookalikes remain ordinary candidates. LinkedIn candidates therefore never reach the
source advisor, retrieval, browser rendering, or claim extraction.

OpenRouter citations are read from
`choices[*].message.annotations[*].url_citation`; the nested `url` is required, while
title and content are optional context. Malformed annotations are ignored, valid URLs
are canonicalized and deduplicated, and URLs that fail safety checks never become
candidates. Ordinary assistant content is never parsed for URLs. If no citations are
available, or filtering or advisor selection leaves no source, orchestration records
`NO_SEARCH_CITATIONS`, `NO_ELIGIBLE_CANDIDATES`, or `NO_SELECTED_SOURCES` and returns
a `review_required` profile with explicit missing fields. These expected empty states
do not become infrastructure-level failed jobs.

Per-query and aggregate result, query, tool-attempt, model-attempt, token, source,
and time budgets bound each person attempt. Aggregate results reserve requested
slots before each HTTP attempt, including retries with unknown usage. Shared model
token reservations estimate both search model passes and bounded result content;
reported usage updates the ledger. These estimates are not a monetary guarantee.
Reported tool-cap violations stop further research. Worker crash recovery has a
separate attempt limit and retains earlier usage records. Plain model IDs prevent
presets and online variants from adding implicit search; deployment must also avoid
forced legacy web plugins in the OpenRouter account settings.

Budget reservations and usage adjustments are serialized per person before a paid
attempt starts. Concurrent extraction is permitted only when the remaining chunks
and their configured retries fit the current call/token headroom. Near either cap,
chunks remain serial so scheduling cannot choose a different evidence subset.
`MAX_CONCURRENT_EXTRACTIONS` caps active extraction requests across one worker's
orchestrator. Completed chunks are consumed in input order before grounding and
reconciliation; usage is retained even when requests finish out of order.

Usage parsing reads model tokens and cost from `usage` and web-search counts from
`usage.server_tool_use.web_search_requests`. Missing optional usage metadata remains
unknown and does not itself fail research. A zero-token attempt therefore carries
diagnostic state rather than implying a budget failure: operation, safe error code,
HTTP status, exception class, retry number, and flags for request sent, response
received, and response body received are persisted with the attempt.

Provider, transport, and schema failures remain terminal when they prevent research.
The worker preserves the specific safe failure code instead of replacing it with a
generic result. Structured logs repeat that code with job, person, pipeline stage,
model, and the safe attempt fields above. Discovery/advisor boundaries also log
candidate, citation, and selected-source counts. Credentials, authorization headers,
secrets, and raw model response bodies are never included.

The extraction role receives bounded processed text and returns schema-validated
claims with literal evidence. Deterministic grounding and identity checks run before
reconciliation. Search citations identify acquisition targets; they do not replace
retrieved evidence for extracted profile claims.

Source-local `fact_group` values link education and employment components but are not
global identifiers. Reconciliation merges compatible bundles across sources by their
normalized values. It ranks current organisation/title as one relationship, computes
confidence and coverage per record, and derives a representative URL from selected
field contributions. The representative choice is deterministic and requires no
additional model request.

Source/advisor prompts treat seed fields, candidate snippets, and known clues as
untrusted data. Extraction also treats source text and metadata as untrusted. JSON
serialization separates these values from the system message; embedded role claims
or delimiter text do not create messages or tools. Extraction and advisor requests
have no tools. Only discovery receives the bounded web-search tool. Prompt guidance
complements schema validation and grounding; it does not replace those checks.

## Retrieval and compatibility

The static Worker receives a POST to its configured full endpoint, body
`{"url": ...}`, and `X-Worker-Secret`. Its response contains `requested_url`,
`final_url`, integer `status`, optional `content_type`, and string `html`.
The same body field can carry structured JSON with the appropriate content type.

Playwright is a fallback for successful thin application shells, never access-denied
pages. All research targets, redirects, and browser requests require public-address
validation. The separately deployed Worker must enforce its own upstream protections.
Development may use a local HTTP Worker endpoint; private research targets stay blocked.
Static and rendered content share downstream cleaning, Markdown, and chunk selection.

A bounded window overlaps selected-source fetches with processing/extraction of
earlier sources. Results are consumed in candidate rank order, retaining deterministic
evidence decisions. A target-confidence result does not discard the rest of its
already-selected discovery round; hard source/no-new-evidence limits can still close
the window. `MAX_CONCURRENT_FETCHES` and `PER_DOMAIN_CONCURRENCY` remain authoritative,
and every unused task is cancelled and awaited. A source failure is handled at its
normal position without cancelling unrelated successful fetches.

Within-job cache stores bounded retrieved content independent of person identity.
Extracted person claims are never reused for another person. Structured acquisition
is operator supplied and bounded: filtered, bulk, or paginated public data.

Cache lookups and in-flight URL locks are keyed by job and canonical URL. Cache hits
still validate stored requested/final URLs, content type, and byte limits. Concurrent
people requesting the same URL share retrieval, never extracted claims. Identical
page bodies retain distinct source records and skip repeated extraction without
becoming independent corroboration. Structured bulk/paginated acquisition uses the
same retrieval cache; it does not introduce a person-claim cache.

OpenRouter and static retrieval keep lifecycle-managed HTTP connection pools. The
browser renderer reuses one Chromium process with a fresh isolated context per
source, restarts disconnected runtimes, and closes contexts and route resources
on completion or cancellation. Static retrieval remains first choice, with the
existing route/DNS, redirect, request, byte, and timeout checks enforced for fallback.

## Input and performance boundaries

Person identity text is normalized with Unicode NFC, preserving non-Latin names and
joiners. Unsupported controls/surrogates and excessive values are rejected. Preferred
URLs are bounded to 4096 characters before URL parsing and network validation. Original
CSV/TSV/XLSX cells remain unchanged; cells allow tabs and line breaks and are bounded
to 32767 characters, headers/name-column selectors to 200, and filenames to 1024.
Canonically equivalent duplicate headers are rejected. Filename text is never used
as a filesystem destination.

XLSX preflight retains compressed/expanded-size, part-count, row, column, and forged
dimension checks. It rejects encrypted/duplicate parts and XML document types/entities
before openpyxl can allocate workbook content. XML validation streams without building
trees. Export keeps formula escaping and replaces characters XML cannot represent.
API job IDs remain UUIDs, and export formats use an explicit CSV/XLSX allowlist.

`app/research/telemetry.py` collects one `person_performance` structured log per
attempt, including persistence and final status work at the worker boundary. It adds
no telemetry table or tiny per-stage writes and leaves result contracts unchanged.
See [performance measurement](performance.md) for timing/counter definitions and
the offline benchmark harness.

The existing `/process` contract and root `main:app` entrypoint remain supported.
Settings use uppercase attributes; credentials are `SecretStr` values and must never
be logged. Automated tests mock external interfaces without alternate source lists
or a separate research implementation.
