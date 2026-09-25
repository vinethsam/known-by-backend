# Offline production-pipeline benchmark

Run from the repository root:

```powershell
python -m benchmarks.offline --output benchmark-report.json
python -m benchmarks.offline --compare benchmark-report.json --output benchmark-comparison.json
```

The default sizes are 1, 5, 25 and 100 different people. `--sizes 1 5` runs a
smaller check. `--latency-ms 20` changes only the artificial HTTP response delay;
it does not change production research limits or budgets. Timings are reported,
never asserted as CI thresholds.

The harness invokes the production OpenRouter client, search parser, source
advisor, retrieval service, processing, extraction, reconciliation, worker lease
handler and migrated SQLite store. It dispatches leased people with
`MAX_CONCURRENT_PEOPLE`; it does not measure the outer worker's idle polling loop.
Settings use their normal defaults and process environment, with fixed dummy
credentials, mocked service addresses, a fresh temporary database and browser
fallback disabled for static HTML fixtures. Both HTTP clients always use
`httpx.MockTransport`. There is no live mode and no API credits can be spent.
Public fixture DNS answers still pass the production URL validation code.

Two public directory pages are shared within each batch, while each person has
different grounded claims. This measures content-cache reuse without sharing
person-specific extraction. Larger directory bodies exercise normal chunking.
The full pipeline still records its usual durable checkpoints.

Reports include elapsed time, average time per person, SQL statement counts,
constant-query result reads, search/fetch/extraction calls, cache hits, logical
HTTP/person concurrency and aggregated `person_performance` events when supported
by the revision under test. SQL counts include job creation, lease acquisition,
checkpoints, cache operations, completion and the final result read; migrations
are excluded. An `executemany` call counts as one statement, regardless of row
count. SQLite timings include local disk and operating-system variability and
are not a prediction of Railway PostgreSQL latency. Stage durations can overlap
and include awaited persistence, so their sums must not be treated as wall time.

`--compare` checks normalized result hashes and exits unsuccessfully on a
difference. It retains selected values, evidence, every claim's source URL,
support/conflict/alternative sets, confidence, coverage, review reasons and
terminal status for the legacy projection and every education-specific record.
It excludes generated IDs, timestamps, operational prompt-version labels, usage
metrics and retained processing buffers. Record lists and their nested references
are normalized deterministically. Equal-scoring copies of an identical fact may
choose a different internal representative UUID; only that representative is
compared by its complete fact content, while the full source-linked
evidence/support sets are still compared. Full normalized results are available
in each generated report for investigating differences. Reports produced before
normalization version 4 intentionally cannot be compared: version 3 added nested
education-record normalization, while version 4 records the controlled degree
vocabulary and revised review/status decisions from the data-quality pass.

The checked-in measurement summary records one before/after run. It contains
counts and hashes, not multi-megabyte duplicated result bodies. To compare another
revision, run the same harness and fixture version with the same environment and
latency, then use `--compare` against its saved report. `tests/test_offline_benchmark.py`
checks repeatability, cache reuse, separate per-person claims and constant result
read queries without wall-clock assertions.

## Recorded before/after measurements

The baseline is revision `8492635`; the final run uses the hardening changes and
the same fixtures, production defaults and 2 ms mocked HTTP delay. Every batch's
normalized output hash matches. These are individual local measurements, not
statistical latency guarantees.

| People | Before elapsed (s) | After elapsed (s) | Before SQL statements | After SQL statements |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 0.661 | 0.358 | 633 | 191 |
| 5 | 2.378 | 1.609 | 3,049 | 839 |
| 25 | 11.001 | 7.256 | 15,129 | 4,079 |
| 100 | 68.221 | 49.417 | 116,245 | 26,828 |

The 100-person run uses 76.9% fewer SQL statements and takes 27.6% less wall time
in this measurement. Checkpoint writes are batched, unchanged ledger rows are
not rewritten, and each completed result read still takes six queries.

Searches remain one per person. Total model attempts remain 5, 25, 125 and 900;
extraction attempts remain 2, 10, 50 and 600. Both revisions fetch exactly two
directory pages per batch, with cache hit rates of 0%, 80%, 96% and 99%. Those
already-efficient cache counts were preserved. The largest fixture has three
chunks per page; each person's claims are still extracted independently.

The final run records one performance event per person and a maximum of three
active people. HTTP fetch concurrency reaches two in the smaller fixtures;
extraction concurrency reaches two in the 100-person fixture. Concurrency peaks
depend on mocked response duration and disk scheduling; explicit regression
tests enforce the configured limits and overlapping requests.

Machine-readable counts, hashes and final stage totals are in
[`baseline.json`](baseline.json) and [`after.json`](after.json). A comparison
requires matching fixture and normalization versions. When a checked-in report
predates the current normalization, first generate a new reference report and
then compare another run against it:

```powershell
python -m benchmarks.offline --output benchmark-reference.json
python -m benchmarks.offline --compare benchmark-reference.json --output benchmark-report.json
```

## Data-quality pass measurement

Normalization version 4 was run on 2026-09-25 with the same 2 ms mocked latency.
The controlled output vocabulary intentionally changes result hashes, so hashes are
not compared with the older version-2 reports. Cache reuse, two physical fetches per
batch, three-person concurrency, two-extraction concurrency at size 100, and the
six-query completed-result read remained intact.

| People | Elapsed (s) | SQL statements | Model attempts | Searches | Extractions |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.502 | 173 | 4 | 1 | 2 |
| 5 | 2.050 | 749 | 20 | 5 | 10 |
| 25 | 10.035 | 3,629 | 100 | 25 | 50 |
| 100 | 77.090 | 25,028 | 800 | 100 | 600 |

Compared with the prior checked-in after-run, SQL statements fell by 6.7%–11.0%
and model attempts fell from 5/25/125/900 to 4/20/100/800 because completed fixture
profiles no longer enter unnecessary review-driven follow-up planning. Wall-clock
results were slower on this individual run and remain non-gating machine measurements.
