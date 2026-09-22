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
| `queue_wait_ms` | Task creation to attempt start; retries include elapsed time since original creation |
| `discovery_ms` | Discovery requests and follow-up query planning |
| `source_validation_ms` | Source-advisor candidate validation |
| `retrieval_ms` | Retrieval work including cache/safety checks and waiting for fetch slots |
| `processing_ms` | Source-to-chunk conversion, fingerprint, metadata, authority and identity checks |
| `extraction_ms` | Structured claim extraction, including retries/checkpoints inside a call |
| `reconciliation_ms` | Deterministic profile reconciliation |
| `persistence_ms` | Checkpoint and terminal persistence calls |
| `total_person_ms` | Wall time for the attempt, excluding queue wait |

Stage durations measure accumulated work, not mutually exclusive wall-clock slices.
Overlapping retrieval/extraction and nested persistence mean their sum can exceed
`total_person_ms`. Direct orchestrator calls have no queue-wait information and report
zero for that field; the worker supplies the full attempt/persistence boundary.

| Counter | Meaning |
| --- | --- |
| `search_queries` | Issued discovery queries |
| `search_tool_calls` | Reserved search attempts, including provider retries |
| `candidates_discovered`, `candidates_selected` | Unique candidates discovered and ranked candidates consumed |
| `sources_requested`, `sources_retrieved` | Retrieval requests and returned pages, including cached pages |
| `retrieval_cache_hits`, `retrieval_cache_misses` | Validated cache lookup outcomes |
| `static_fetches`, `playwright_fallbacks` | Static attempts and eligible browser fallbacks |
| `extraction_calls`, `extraction_chunks` | Model extraction attempts including retries, and chunks started |
| `input_tokens`, `output_tokens` | Reported tokens across model attempts |
| `provider_cost` | Sum of available provider-reported costs; missing cost is not a claim of free usage |

Actual reported web-search executions remain available in model usage records under
`web_search_requests`; missing provider metadata remains unknown. Performance logs
contain no seed text, source bodies, request headers, credentials, or environment dump.

## Conservative concurrency

`MAX_CONCURRENT_PEOPLE` bounds people per worker. `MAX_CONCURRENT_FETCHES` bounds
active retrieval, and `PER_DOMAIN_CONCURRENCY` bounds each domain. Waiting for a busy
domain does not consume a global fetch slot needed by another domain. Cache hits are
resolved before acquiring network slots. These limits apply per worker process;
adding worker replicas increases deployment-wide concurrency.

Source prefetch uses a bounded window and consumes results in rank order. Fetching a
later source can overlap processing/extraction of an earlier one. Some speculative
fetches may finish before an early-stop decision cancels pending work; compare
`static_fetches` as well as duration when tuning. Ranked evidence decisions and source
caps are preserved, while unused pending tasks are cancelled and awaited.

`MAX_SOURCES_PER_PERSON` continues to cap processed source records. Redirect aliases
already known before a fetch starts are skipped. An alias already in flight can add
speculative I/O without consuming another evidence slot: selected-source retrieval
is bounded by `2 * MAX_SOURCES_PER_PERSON - 1` distinct requests per attempt in that
case. Operator-configured structured preloads keep their existing separate page
bounds. This preserves later evidence that a serial run would have processed.

`MAX_CONCURRENT_EXTRACTIONS` is the only new tuning setting: default `2`, allowed
range `1`–`20`, shared across one worker's orchestrator. Parallel chunks require enough
person budget for all remaining chunks and configured retries; otherwise extraction
falls back to serial order. Each actual attempt still obtains an atomic reservation
before transport. Usage and checkpoints are serialized safely when responses finish
out of order. Raising concurrency does not raise a person's token or attempt budget.

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

Redeploy the web and worker from the same revision. The extraction setting may be
omitted to use its conservative default. After deployment, one ordinary person run
can verify performance logs, retrieval/usage counts, source provenance, and terminal
status before a larger batch is considered.
