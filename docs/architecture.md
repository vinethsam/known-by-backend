# Architecture contracts

KnownBy starts with a person seed, not a target website. FastAPI validates input and
creates durable jobs; a separate async worker discovers, retrieves, and reconciles
public evidence. Trial runs use these same production modules and settings.

## Data and persistence

Shared contracts live in `app/schemas/__init__.py`. IDs are UUID strings, timestamps
are UTC, and JSON payloads use `model_dump(mode="json")`. Every profile has seven
field decisions, including explicit missing values. Raw claims and source provenance
survive flat field selection. Identity is assessed separately from source authority;
final field/profile confidence is deterministic, with coverage reported separately.

PostgreSQL is the production database; SQLite supports development. SQLAlchemy
transactions are short and synchronous, dispatched through thread pools during async
work; no session spans a network await. Alembic owns schema changes. Worker leases,
renewals, and fencing tokens prevent expired or cancelled attempts from publishing
late results. Checkpoints retain evidence and recorded usage.

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

Within-job cache stores bounded retrieved content independent of person identity.
Extracted person claims are never reused for another person. Structured acquisition
is operator supplied and bounded: filtered, bulk, or paginated public data.

The existing `/process` contract and root `main:app` entrypoint remain supported.
Settings use uppercase attributes; credentials are `SecretStr` values and must never
be logged. Automated tests mock external interfaces without alternate source lists
or a separate research implementation.
