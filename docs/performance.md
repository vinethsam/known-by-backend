# Performance measurement and tuning

All runs use the production pipeline. Offline benchmarks replace external HTTP with
deterministic fixtures and do not spend API credits. The benchmark harness and its
commands are documented in [benchmarks/README.md](../benchmarks/README.md). Compare
fixture result equivalence along with elapsed time and work counts; a lower latency
alone does not establish an improvement in research quality.

## Person performance event

Each person attempt emits a `person_performance` JSON log with `job_id`, `person_id`,
and a compact `performance` object. It is emitted when the attempt exits, including
failure or cancellation, rather than written as a new database row for each stage.
Existing persisted model-attempt usage remains the durable provider ledger.

| Duration | Meaning |
| --- | --- |
| `queue_wait_ms` | Task creation to the first research start available to the worker |
| `prior_elapsed_ms` | Time from the stored research start to a resumed lease, including earlier work and recovery waits |
| `person_end_to_end_ms` | Initial queue wait, prior elapsed time, and current lease wall time |
| `discovery_ms` | Discovery requests and follow-up query planning |
| `source_validation_ms` | Source-advisor candidate validation |
| `retrieval_ms` | Retrieval work including cache/safety checks and waiting for fetch slots |
| `processing_ms` | Source-to-chunk conversion, fingerprint, metadata, authority and identity checks |
| `extraction_ms` | Structured claim extraction, including retries/checkpoints inside a call |
| `reconciliation_ms` | Deterministic profile reconciliation |
| `persistence_ms` | Checkpoint and terminal persistence calls |
| `provider_backoff_ms` | Time spent sleeping between retries inside OpenRouter transport |
| `total_person_ms` | Wall time for the current lease |

Stage durations measure accumulated work, not mutually exclusive wall-clock slices.
Overlapping retrieval/extraction and nested persistence mean their sum can exceed
`total_person_ms`. Direct orchestrator calls have no queue-wait information and report
zero for that field. A resumed lease separates earlier elapsed time when it has a
checkpoint. Without that checkpoint, the worker can only measure from the current lease.

| Counter | Meaning |
| --- | --- |
| `search_queries` | Issued discovery queries |
| `search_tool_calls` | Reserved search attempts, including provider retries |
| `candidates_discovered`, `candidates_selected` | Unique candidates discovered and ranked candidates consumed |
| `sources_requested`, `sources_retrieved` | Retrieval requests and returned pages, including cached pages |
| `retrieval_cache_hits`, `retrieval_cache_misses` | Validated cache lookup outcomes |
| `extraction_cache_hits`, `request_cache_hits` | Already-applied extraction chunks and exact completed provider receipts reused |
| `deterministic_validations` | Candidate metadata decisions made before model-assisted validation |
| `static_fetches`, `playwright_fallbacks` | Static attempts and eligible browser fallbacks |
| `extraction_calls`, `extraction_chunks` | Model extraction attempts including retries, and chunks started |
| `input_tokens`, `output_tokens` | Reported tokens across model attempts |
| `provider_cost` | Lease-only subtotal of available provider-reported costs; see cumulative accounting for completeness |

Actual reported web-search executions remain available in model usage records under
`web_search_requests`; missing provider metadata remains unknown. Performance logs
contain no seed text, source bodies, request headers, credentials, or environment dump.

The nested `performance.research` object and retained `metrics.accounting` contain
cumulative accounting across recovered leases. Usage IDs are deduplicated before
counting model attempts, operation/model breakdowns, input/output tokens and reported
cost. Budget reservations remain separate and unchanged. Reserved attempts without a
durable reply make accounting incomplete. Proven pre-write connection/pool failures
are excluded from model-call counts and do not create unknown provider charges.

`reported_api_cost` is the known subtotal. `actual_api_cost` is null when any attempt
has unknown cost; `cost_complete` and `unknown_cost_calls` make that distinction
explicit. A null provider charge is never estimated or treated as free usage.
Search accounting similarly retains a known request subtotal and null actual request
count when provider tool metadata is incomplete.

Independent supported/unresolved fields are measured across all credential records,
requiring a researched selection, positive independent confidence and evidence
support. Supplied input certainty cannot fill the metric. Existing confidence
formulas and thresholds do not change. An enriched person has at least one supported
professional or education field; name/profile-link-only results remain distinguishable.
The independent field coverage rate uses all seven required fields and is separate
from the existing six-researched-field profile coverage metric.

`aggregate_person_summaries` calculates actual cost per attempted person, per enriched
person and per independently supported field; calls per person; independent coverage;
and retrieval reuse (`hits / (hits + misses)`). Rates use 0–1. Cost-per-enriched-person
includes expenditure on unsuccessful attempts in its numerator. Unmeasured attempts
make averages unknown instead of improving them through an artificial zero. Rejected
input rows and cancellations before research are excluded from attempted-person
denominators. Aggregation uses retained compact summaries rather than rereading all
evidence or performing historical backfills.

`by_model` groups durable attempts by the returned model when available and the
requested model otherwise. It lists requested aliases and observed inference providers.
The same report tracks extraction success, invalid structured replies, output limits,
fallbacks, and retries. Attempt latency totals can overlap under concurrency and
exclude retry backoff and checkpoint time. Reported spend on failed, retried, and
fallback attempts can overlap. Each subtotal describes its own category.

Grounded claims carry their extraction model or deterministic rule. Model credit
for a supported field requires a matching accepted or corroborating claim for
that person and field. One field can receive support from multiple models. In the
job report, `supported_fields` counts people with that model's support for each
field. Historical summaries without model detail contribute only to the overall
totals. Check `model_performance_people_measured` before comparing model totals.

Terminal job logs also report `batch_end_to_end_ms` from creation to terminal commit,
`batch_queue_wait_ms` before first processing, and `batch_processing_wall_ms` from
the first processing start to terminal commit. `completed_people_per_minute` counts
finalized completed and review-required people over batch end-to-end time.

Repository fixture costs are synthetic. November 1–2 credit-exhaustion/HTTP 403
batches are failed-provider diagnostics, not a successful cost or coverage baseline.
The November 5 five-person job `e79e7948-464b-45ad-9911-9d548e4fc864` provides a
successful baseline: $0.11215195 actual cost, $0.02243039 per attempted person,
60 model calls (45 extraction, 5 discovery, 10 candidate validation), and 23/35
independently supported fields. Four people were enriched. Every person exhausted
the six-source budget after one discovery query; no deterministic candidate
validations were recorded. This baseline does not establish savings from later
changes. A 50% cost reduction remains a target pending a comparable live run.

## Phase 2 extraction and field objectives

Named schema.org Person/ProfilePage facts and scoped Person microdata can now use
the existing claim grounding, source identity and reconciliation gates before model
extraction. Method, source, literal structured evidence, location and relationship
group remain on each claim. Only fully interpreted, grounded fact projections are
removed from subsequent extraction; rich properties, contradictory relationships,
unmatched identities and ordinary biography/education prose still reach the model.
Consecutive exact long evidence blocks can be deduplicated; intervening headings,
dates and institution context remain distinct. Education sections within the target
person's heading scope receive selection priority within the unchanged chunk budget.

After an initial source round, unresolved identity, paired employment or education
can trigger a deterministic targeted query before broad pending candidates consume
all retrieval slots. Pending candidates and their validation remain reusable if the
targeted search is empty or ambiguous. Search, source and provider budgets are
unchanged. Target-confidence exits use independently researched values and scores.

Internal cumulative accounting additionally records deterministic claims extracted,
grounded and accepted in selected field support; extraction calls/chunks avoided;
duplicate blocks/characters; actual model extraction escalations; field-specific
searches; and grounded versus selected claims. Avoided calls describe the difference
in eligible chunk counts, not an estimated provider charge. Grounded claim counts
include deterministic normalization of compound qualifications. Actual costs still
come exclusively from the provider ledger. No diagnostic export columns are added.

Source URLs now use accepted field attribution or explicit selected/supporting
claim references. Unused retrieved candidates stay internal; missing historical
attribution is left blank rather than promoted to confirmed evidence. Original saved
files and evidence are retained, including their historical explicit source lists.

## Phase 4 observations

The recovered five-person reference used $0.03861 per attempted person and
supported 29 of 35 fields. It made 78 model calls, including 47 extraction,
18 validation and 13 web-search calls. Web search accounted for about 53% of the
reported spend. The five original attempt logs averaged 133 seconds per person
after queue wait. Extraction accounted for about 75 seconds of accumulated work,
validation 27 seconds and discovery 15 seconds. Stage durations can overlap.
The earlier $0.02243 reference is useful context for a controlled comparison.

Compatible same-page JSON-LD fragments now carry explicit university, degree and
subject links across script blocks. The focused fixture moved from three to six
supported fields and from one remaining model chunk to zero. Structured
projections and Markdown are prepared once per page and reused after grounding.
Model prompts omit internal job, person and source IDs while the durable attempt
ledger retains them.

Workers refill a freed slot as soon as a task finishes. In three offline repeats
of the same five short tasks, median batch wall time went from 2,118 ms to 397 ms.
This fixture isolates the idle poll delay. Production latency and cost still
need a comparable live run.

DeepSeek keeps its 6,000-token extraction allowance and bounded Luna fallback.
`OPENROUTER_DEEPSEEK_EXTRACTION_REASONING` defaults to false and applies to the
supported DeepSeek snapshot. Keep source discovery and validation on their
configured model during extraction comparisons. Verify the worker key's
permitted allowance before any approved live comparison.

## Phase 1 paid-call audit and deterministic work

| Operation | Calling path | Configured model | When a request remains necessary |
| --- | --- | --- | --- |
| `web_search` | `BudgetedModel.search` → `OpenRouterSearchProvider.search` | `OPENROUTER_SOURCE_MODEL` | Each fresh search-index query; Python query construction does not replace search access |
| `plan_search_queries` | `SourceAdvisor.plan` → `BudgetedModel.complete` | `OPENROUTER_SOURCE_MODEL` | Changed unresolved evidence after useful deterministic objectives are exhausted |
| `validate_candidates` | `SourceAdvisor.validate` → `BudgetedModel.complete` | `OPENROUTER_SOURCE_MODEL` | Uncertain candidate metadata, including initials/name variations without sufficient institutional context |
| `extract_claims` | `extract_chunks` → `BudgetedModel.complete` | `OPENROUTER_EXTRACTION_MODEL` | Each unapplied relevant text chunk after fetched-body identity admission |

Request counts include provider retries; all retain existing per-person query/tool,
source, token, time and model-attempt budgets. Provider-internal passes are not
separately observable; token/cost totals use the provider's reported usage.

Missing education now receives a deterministic targeted query, using supplied or
independently selected context, before optional planning. Existing strong unpaired
role completion stays first. Straightforward name/organisation/title/university
queries and supplied profile URLs use normalized existing input. Equivalent queries
are deduplicated. Planner input fingerprints survive recovery so unchanged unresolved
inputs do not incur fresh planning. Difficult/name-only discovery retains the existing
advisor and Wikipedia fallback. A definitive 401/402/403 stops new provider calls for
that person attempt; a later resumed attempt can recover after credits/auth are fixed.

Candidate validation can be deterministic only with an operator-configured domain
classification and directly associated name/institution metadata without a detected
contradiction. This is admission to retrieval, not identity confirmation. The fetched
body, namesake checks, claim grounding, reconciliation and confidence remain required.

Applied extraction chunks are skipped before submission, preserving original chunk
indexes. Keys bind person, canonical source URL, exact text, source, model, prompt and
rule version. Completed exact-request receipts also work without a checkpoint callback.
Already retrieved pages still use the existing job cache and freshness/safety policy.
A completed, identity-admitted redirect alias can reuse exactly equal processed text
only at the same canonical URL, rules and publication metadata with no stronger new
authority/identity signal. No claims are copied across people or independent domains.
Existing Python boilerplate removal and relevant-section selection remain in use.

Small mocked-provider checks demonstrate one suitable two-source case reducing total
requests from four to three by removing its metadata-validation request with identical
field values/confidence. A two-chunk recovery submits one remaining chunk, and an exact
redirect alias needs one extraction instead of two. These are offline work-count checks,
not measured live cost savings or proof of the 50% target.

## Conservative concurrency

`MAX_CONCURRENT_PEOPLE` bounds people per worker. `MAX_CONCURRENT_FETCHES` bounds
active retrieval, and `PER_DOMAIN_CONCURRENCY` bounds each domain. Waiting for a busy
domain does not consume a global fetch slot needed by another domain. Cache hits are
resolved before acquiring network slots. These limits apply per worker process;
adding worker replicas increases deployment-wide concurrency.

Source prefetch uses a bounded window and consumes results in rank order. Fetching a
later source can overlap processing/extraction of an earlier one. Target confidence
is evaluated after the selected discovery set is drained so corroboration and another
credential are not lost. Hard source/no-new-evidence limits can still cancel pending
work. Compare `static_fetches` as well as duration when tuning; unused tasks are always
cancelled and awaited.

An empty fully constrained search can enqueue deterministic single-clue fallbacks.
These queries do not add a new budget or unbounded retry path: the existing query,
server-tool, aggregate-result, token, source, and person-time limits still apply.
The same queue is admitted once after an initial selected source set yields no
identity-eligible grounded claims. Failed canonical URLs remain seen and cannot loop
back into retrieval. Recovery stays inside the existing query, result, tool, source,
token, and person-time budgets. Retrieval and identity failures do not advance the
no-new-claims stop; only a completed extraction with no new grounded claims does.

Each eligible static result receives at most one browser attempt. A browser failure is
the final retrieval outcome for that source; it does not create an unbounded retry path.

`MAX_SOURCES_PER_PERSON` continues to cap processed source records. Redirect aliases
already known before a fetch starts are skipped. An alias already in flight can add
speculative I/O without consuming another evidence slot: selected-source retrieval
is bounded by `2 * MAX_SOURCES_PER_PERSON - 1` distinct requests per attempt in that
case. Operator-configured structured preloads keep their existing separate page
bounds. This preserves later evidence that a serial run would have processed.

`MAX_CONCURRENT_EXTRACTIONS` remains the extraction-concurrency limiter: default `2`,
allowed range `1`–`20`, shared across one worker's orchestrator. Parallel chunks require
enough person budget for all remaining chunks and configured retries; otherwise
extraction falls back to serial order. Each actual attempt still obtains an atomic
reservation before transport. Usage and checkpoints are serialized safely when
responses finish out of order. Raising concurrency does not raise a person's token or
attempt budget.

For a controlled Phase 3 comparison, rerun the five reference inputs under the same
provider, model and budget settings. Compare the terminal `research_cost_summary`
against $0.02243 per person, 65.7% independent field coverage and 12 model calls per
person. Review `person_performance` and decision summaries for changes in accepted
evidence and review outcomes.

The job summary includes calls by operation and targeted searches with accepted
support. A search counts toward this numerator when a newly discovered source from
that search supports a final decision for one of its target fields. Use
`field_specific_searches_measured` as the denominator. The same summary separates
identity failures and review outcomes and retains reported provider costs.

## Reuse and durability

Canonical URL locks and the existing job cache avoid duplicate simultaneous fetches.
Cache TTL, job byte caps, cleanup, and URL/content safety validation remain active.
Only retrieved public content is shared between people. Extraction and identity checks
run separately for each person, including people using the same structured directory.
Identical page bodies retain their source records while avoiding duplicate extraction
and independent-corroboration credit.

HTTP pools and the reusable browser process already existed and remain tied to worker
lifecycle. Browser contexts stay isolated by source. Completed-result reads remain
batched at six queries; checkpoint evidence/usage writes use batched upserts and short
transactions, skipping updates to unchanged ledger rows. Lease renewal and fencing
remain intact; renewal failures also cancel and drain owned research work. There is no new infrastructure,
database migration, or telemetry persistence service.

The 2026-09-26 normalization-version-5 run is recorded in the
[accuracy and retrieval-recovery benchmark table](../benchmarks/README.md#accuracy-and-retrieval-recovery-pass-measurement).
It preserved the two-fetch cache pattern, configured concurrency peaks, version-4
work counts, and six-query completed-result reads. Recovery is covered by separate
failure fixtures and stays inactive for healthy benchmark batches. Wall-clock timings
remain non-gating and vary with local machine load.

Redeploy the web and worker from the same revision. The extraction setting may be
omitted to use its conservative default. After deployment, one ordinary person run
can verify performance logs, retrieval/usage counts, source provenance, and terminal
status before a larger batch is considered.
