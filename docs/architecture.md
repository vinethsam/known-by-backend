# Architecture contracts

KnownBy starts with a person seed, not a target website. FastAPI validates input and
creates durable jobs; a separate async worker discovers, retrieves, and reconciles
public evidence. Trial runs use these same production modules and settings.

CSV/TSV/XLSX uploads are flexible seed schemas. After existing file, archive, row,
column, cell, Unicode, and control-character validation, the importer normalizes each
header once and applies a centralized deterministic alias/pattern table. A usable
person-name mapping is required; organisation, role, country/location, university,
subject, and program-year mappings populate existing `PersonSeed` clues. Semantic
headers retain useful intent: a current employer or government office is a stronger
current-role hypothesis, while an alumni organisation or former employer is an
identity/history clue rather than an assumed current role. Name-only rows remain valid.
Unknown or ambiguous optional columns are not guessed. Source-list fields such as
`LIST_NAME`, `source_list`, `source_dataset`, `dataset_name`, `cohort`, and
`source_cohort` are recognized as passive row metadata and are never converted into
person clues. Original rows and row indexes remain the traceability mechanism. The
validated person name is the immutable identity: every output record repeats it with
conservative display casing, 100% input certainty and no web provenance or name-field
review. Raw seed text and original input cells remain unchanged. Other spreadsheet
person values are hypotheses only and never become web evidence, provenance, confidence,
or final enriched facts by themselves.

## Data and persistence

Shared contracts live in `app/schemas/__init__.py`. IDs are UUID strings, timestamps
are UTC, and JSON payloads use `model_dump(mode="json")`. Every profile has seven
field decisions, including explicit missing values: one immutable input-name decision
and six evidence-derived decisions. Raw claims and source provenance survive flat
field selection. Web identity is assessed separately from both the name's input
certainty and source authority. Final enriched-field/profile confidence is
deterministic, with evidence-derived coverage reported separately.

Reconciliation is multi-source. A field decision can cite supporting claims and URLs
from several independent sources. Distinct supported education credentials become
separate `ProfileRecord` objects; the display-normalized seed name and selected
current-employment decisions repeat on each record, while education claims stay scoped to their
credential. Export projection starts from the complete original row, so passive
source-list metadata repeats unchanged on every derived record. Identical people from
different input rows or source lists remain separate tasks and results. The existing
flat `PersonProfile.fields` is the deterministic primary-record projection.
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

The shared results library is independent of that research lifecycle. Only an explicit
authorized request for a completed job generates a retained artifact through the normal
export renderer. `saved_result_files` stores the resulting CSV/XLSX bytes and minimal
file metadata, with no foreign key to temporary jobs and no copied research entities.
`ResultFileStorage` isolates the API from the SQL implementation. PostgreSQL transaction
advisory locking and SQLite write reservations serialize shared quota checks; metadata
list queries never load file bytes. Migration `202609290001` creates this separate
table. All library operations require the configured bearer token, including in
development; the existing single-token model grants the authorized team shared list,
download, save, and delete permissions. Saving adds no research or provider call.
See [library API and limits](library.md).

Normal downloads and saved artifacts use the same export renderer and filename helper.
The filename is `<List-Name>_<YYYY-MM-DD>_<HHmm>.<csv|xlsx>`, using the first nonempty
source-list value in original input order and the job start in UTC (creation time for
an unstarted job). Missing list names use `KnownBy`; safe Unicode names and encoded
download headers preserve readability. Internal job IDs never form the visible filename.
The original list-name cells remain unchanged.

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
the exact person name and available person-context clues; a name-only seed uses the
same path without context terms. Supplied affiliation, role, education, and geography
also inform deterministic candidate ranking, namesake rejection, and clue-preserving
follow-up queries. Passive source-list metadata is absent from the research seed and
therefore absent from queries, source/advisor payloads, identity decisions, and field
selection. Candidate decisions are reused within a person attempt, and pending
candidates are processed before paying for another search. Follow-up planning runs
only when unresolved fields justify it.

When a supported current government, executive, or primary role has no paired
institution, orchestration may queue one deterministic completion query containing
the exact name, title, available geography, and `official`. If important role or
education fields remain unresolved after normal planning, one `site:wikipedia.org`
query is reserved within the existing per-person search budget, unless Wikipedia was
already considered. This is a completion path, not a required source for every job.

Seed organisation, university, title, geography, subject, year, and known attributes
guide discovery and identity resolution only. They never enter the claim ledger or
final fields without retrieved, grounded evidence. Corroborating web evidence can turn
a seeded affiliation into a strong identity signal through the normal authority,
grounding, freshness, and confidence rules. If reliable newer evidence establishes a
different current organisation, that evidence wins; the seed remains search/history
context and does not become a selected or historical fact unless evidence supports it.
When the seed supplies an identity anchor, same-name sources that do not connect to an
anchor may be extracted provisionally, but their claims cannot populate the final
profile without corroboration. A source that explicitly contradicts the seed is also
retained as provisional evidence instead of being discarded: reliable exact-name,
explicitly current evidence can establish a corrective organisation when it provides
a paired role or comes from a primary institutional source; weaker or unpaired
contradictions remain ineligible. Current-employer,
government-office, and other strong institutional clues can provide anchors.
Alumni/former affiliations remain supporting historical identity clues, and job title
is the fallback when no stronger anchor is present.

Every source selected in the current bounded discovery round is consumed before a
target-confidence stop. This allows corroboration and additional credentials without
changing the concurrent fetch window, extraction semaphore, budgets or source caps.

LinkedIn and ordinary social-media sources are excluded by one deterministic source
policy. The hostname denylist includes redirect/content host trees and blocks exact
hosts and their subdomains; hostname lookalikes remain ordinary candidates. A `social`
classification also excludes less common platforms outside that list. The policy runs
at candidate, retrieval, extraction, reconciliation, confidence, and export boundaries,
including requested and final redirect URLs. Excluded sources cannot supply claims,
representative links, or exported source provenance. Wikipedia remains eligible under
its existing encyclopedia authority. The separately deployed static Worker must apply
the same deny policy before each upstream redirect hop.

At the database read boundary, historical results containing forbidden evidence are
re-reconciled using only their eligible retained sources and original seed. This
removes stale social-derived values and confidence from JSON results, exports, and
new library saves without another search/model call or rewriting raw stored ledgers.
Clean results retain their stored decisions and the existing six-query result read.

OpenRouter citations are read from
`choices[*].message.annotations[*].url_citation`; the nested `url` is required, while
title and content are optional context. Malformed annotations are ignored, valid URLs
are canonicalized and deduplicated, and URLs that fail safety checks never become
candidates. Ordinary assistant content is never parsed for URLs. If no citations are
available, or filtering or advisor selection leaves no source, orchestration records
`NO_SEARCH_CITATIONS`, `NO_ELIGIBLE_CANDIDATES`, or `NO_SELECTED_SOURCES` and returns
a completed zero-coverage profile with `research_status=insufficient_evidence` and
explicit missing fields. These expected empty states are neither human-review items nor
infrastructure-level failed jobs. An empty first query may use deterministic clue-relaxing
fallbacks, always within the existing hard budgets. If selected sources all fail or
produce no identity-eligible grounded claims, one deterministic recovery queue uses
the same clue-based queries. Seen URLs remain excluded, and the existing query,
result, tool, source, token, and person-time limits remain authoritative.

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
global identifiers. Before grouping, centralized deterministic normalization maps common
English and multilingual degree terms to a controlled eight-type vocabulary, classifies
non-degree education, safely expands recognizable compound qualifications, and derives
an explicit subject embedded in a degree title. Literal claims remain unchanged in the
evidence ledger. Reconciliation merges compatible bundles across sources by normalized
values. Education credentials split only when positive evidence establishes distinct
qualifications. A comma- or semicolon-appended institution location is ignored for
grouping while the raw value is retained, and same-institution/same-level subject
variants remain one credential unless dates, qualification wording, or other
relationship evidence proves otherwise. Bundles without a degree cannot create a
credential; a fragment may corroborate one unambiguous existing credential or remain
alternative evidence. Reconciliation ranks current organisation/title as one
relationship using freshness, currentness, primary-role type, and evidence strength;
public roles receive a compact office subtype. It computes confidence and coverage per
record, applies the selected-value floor, and derives a representative URL from selected
field contributions. All steps are deterministic and require no additional model request.

Selected display values also use small exact-phrase English mappings for common Spanish,
French, and Portuguese titles, subjects, and generic institutional terms. Unknown proper
names keep their grounded wording. Conservative casing preserves recognized acronyms,
name particles, apostrophes, hyphens, Roman numerals, and deliberate internal capitals.
These presentation rules leave raw seed text, claim values, evidence excerpts, source
titles, and URLs intact and do not add a translation service or model call.

Source/advisor prompts treat non-name seed fields as unverified hypotheses and allow
newer retrieved evidence to disagree with them. Passive source-list metadata is
removed before research payloads are built. Candidate snippets, known clues, source
text, and source metadata are otherwise treated as untrusted data. JSON serialization
separates these values from the system message; embedded role claims or delimiter text
do not create messages or tools. Extraction and advisor requests have no tools. Only
discovery receives the bounded web-search tool. Prompt guidance complements schema
validation and grounding; it does not replace those checks.

## Retrieval and compatibility

The static Worker receives a POST to its configured full endpoint, body
`{"url": ...}`, and `X-Worker-Secret`. Its response contains `requested_url`,
`final_url`, integer `status`, optional `content_type`, and string `html`.
The same body field can carry structured JSON with the appropriate content type.

Static retrieval remains first. One Playwright attempt may follow a timeout or other
recoverable static error, status 403/408/425/429, redirect/transient server response,
challenge page, empty or thin body, chrome-only page, or JavaScript shell. Permanent
client errors, unsupported types, size-limit failures, configuration errors, and URL
policy failures do not use browser fallback. Rendered output must itself be successful
and usable. `STATIC_MIN_READABLE_CHARS` (default `250`) controls the readable-text
threshold used to identify thin static content. Login/CAPTCHA controls are not
bypassed. All research targets, redirects, and browser requests require public-address
validation. The separately deployed Worker must enforce its own upstream protections.
Development may use a local HTTP Worker
endpoint; private research targets stay blocked. Static and rendered content share
downstream cleaning, Markdown, and chunk selection.

A bounded window overlaps selected-source fetches with processing/extraction of
earlier sources. Results are consumed in candidate rank order, retaining deterministic
evidence decisions. A target-confidence result does not discard the rest of its
already-selected discovery round; hard source/no-new-evidence limits can still close
the window. `MAX_CONCURRENT_FETCHES` and `PER_DOMAIN_CONCURRENCY` remain authoritative,
and every unused task is cancelled and awaited. A source failure is handled at its
normal position without cancelling unrelated successful fetches.
Retrieval, duplicate, and identity-rejected sources do not consume the no-new-claims
streak; only a completed extraction with no new grounded claim does so.

Within-job cache stores bounded retrieved content independent of person identity.
Extracted person claims are never reused for another person. Structured acquisition
is operator supplied and bounded: filtered, bulk, or paginated public data.

Cache lookups and in-flight URL locks are keyed by job and canonical URL. Cache hits
still validate stored requested/final URLs, content type, and byte limits. Concurrent
people requesting the same URL share retrieval, never extracted claims. Identical
page bodies retain distinct source records and skip repeated extraction without
becoming independent corroboration. Structured bulk/paginated acquisition uses the
same retrieval cache; it does not introduce a person-claim cache.
An older cached static page that is now classified as unusable can take the same one
browser fallback and replace its cache entry; all cached URLs and byte/type limits are
still revalidated first.

OpenRouter and static retrieval keep lifecycle-managed HTTP connection pools. The
browser renderer reuses one Chromium process with a fresh isolated context per
source, restarts disconnected runtimes, and closes contexts and route resources
on completion or cancellation. Static retrieval remains first choice, with the
existing route/DNS, redirect, request, byte, and timeout checks enforced for fallback.

## Input and performance boundaries

Person identity text is normalized with Unicode NFC and narrow mojibake repair, preserving non-Latin names and
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
