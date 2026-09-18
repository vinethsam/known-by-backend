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

Only valid `url_citation` annotations from search responses become discovered
candidates. Model-prose URLs never enter the candidate pool. Preferred seed URLs and
operator-reviewed structured endpoints are separate eligible inputs. Canonical URLs
and equivalent queries are deduplicated before repeated work. The first query uses
the exact person name and supplied clues. Candidate decisions are reused within a
person attempt, and pending candidates are processed before paying for another
search. Follow-up planning runs only when unresolved fields justify it.

Per-query and aggregate result, query, tool-attempt, model-attempt, token, source,
and time budgets bound each person attempt. Aggregate results reserve requested
slots before each HTTP attempt, including retries with unknown usage. Shared model
token reservations estimate both search model passes and bounded result content;
reported usage updates the ledger. These estimates are not a monetary guarantee.
Reported tool-cap violations stop further research. Worker crash recovery has a
separate attempt limit and retains earlier usage records. Plain model IDs prevent
presets and online variants from adding implicit search; deployment must also avoid
forced legacy web plugins in the OpenRouter account settings.

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
