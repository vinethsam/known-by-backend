# Validation record

Trial jobs use the production API, queue, worker, providers, and settings; offline
fixtures replace external network responses without a second research pipeline or
paid provider calls. Docker and CI target Python 3.13.

## Eight-field export and Country of Origin, 2026-10-23

| Check | Result |
| --- | --- |
| Full offline suite | **792 passed, 6 skipped, 2 warnings**; six PostgreSQL runtime cases require `TEST_POSTGRES_URL` |
| Origin-specific regressions | **35 passed**, including grounded/rejected origin, mixed-page exclusion, conflicts, multi-record propagation, older JSON, and unchanged provider/search call counts |
| Ruff lint | `ruff check app tests` passed |
| Ruff formatting | All 14 changed Python files passed `ruff format --check` |
| Compile/import | Application modules compiled and API, schema, research, and export imports passed |
| Alembic | Single head `202609290001`; full-suite fresh SQLite upgrade, schema-drift check, downgrade, and PostgreSQL SQL compilation passed |
| Git diff whitespace check | Passed |

The export checks compare the fixed standard prefix across CSV/XLSX, name-only and
context-rich input, provenance modes, and Shared Library saves. Country of Origin
uses externally grounded source claims; an input country alone remains unverified.
Missing origin is a null decision. Older JSON profiles load with recalculated
seven-researched-field `coverage` and comparable six-field `legacy_coverage`.
This addition uses existing JSON persistence, so no database migration was added.
The offline pipeline comparison confirms no additional provider or search calls.
No live or paid provider request was made.

## Latest targeted quality refinement, 2026-10-22

| Check | Result |
| --- | --- |
| Focused changed-area suite | **77 passed**; same fixtures at prior HEAD passed **67** |
| Golden plus affected pipeline suite | **114 passed** |
| Additional mixed-page identity controls | **2 passed** in the complete suite; not part of the controlled 77-case comparison |
| Complete offline suite | **755 passed, 6 skipped, 2 warnings**; PostgreSQL runtime checks require `TEST_POSTGRES_URL` |
| Offline production-pipeline benchmark | Prior vs modified at 1, 5, and 25 people: normalized output hashes and work counts match; browser fallback was not exercised |

These are offline regression results, not measured live research accuracy. The
[refinement record](quality-refinement.md) distinguishes observed Forbes/YALI export
symptoms from synthetic same-fixture before/after tests and explains the remaining
unverified cases. No live provider call was made.
Benchmark timings are single non-gating local measurements; details are in the
[benchmark record](../benchmarks/README.md#targeted-quality-refinement-measurement).

## Prior partial-failure salvage checks, 2026-10-08

The earlier pass was validated locally on Windows with Python **3.14.7**:

| Check | Result |
| --- | --- |
| Final full test suite | **739 passed, 6 skipped, 2 warnings** in **32.99 seconds**; PostgreSQL integration requires `TEST_POSTGRES_URL` |
| Frozen golden panel | **14 passed** across all ten requested people; the same panel at `359202a` passed 2 and failed 12 |
| Focused checks | Partial-failure, pipeline, decision-trace, and job-outcome set: **47 passed**; identity, confidence, role, normalization, export, persistence, and recovery cases also pass in the full suite |
| Offline production-pipeline benchmark | 1, 5, 25, and 100 people with mocked HTTP; before/after search, model, extraction, SQL, cache, and result-read counts unchanged |
| Ruff lint | `ruff check .` passed |
| Ruff formatting | All Python files formatted |
| Python compile/import | Application and compatibility entry points passed |
| Alembic | Single head `202609290001`; fresh SQLite upgrade/check/downgrade passed with no schema drift; PostgreSQL BYTEA DDL compiled offline |
| Git diff whitespace check | Passed |

Two upstream deprecation warnings concern Starlette's TestClient transport and its
AnyIO portal alias. Neither failed a test. No paid provider requests were made.

## Partial-failure salvage coverage

The [salvage report](partial-failure-salvage.md) records the failure scope, selection
gate, outcome taxonomy, decision trace, and benchmark. Deterministic fixtures cover
three failed plus three successful sources, ten mixed claims, `MAX_SOURCES`, unsupported
dates, mixed valid/invalid fields, failed recovery after valid evidence, true retrieval
failure, grounding failure, identity failure, and disconnected namesakes. A partial
source failure now reduces only the affected coverage. Zero-coverage records are failed
with the stage that prevented selection; they are not returned as ordinary completed
profiles or represented through `review_required`.

The 1/5/25/100-person before/after benchmark produced identical normalized result hashes
and unchanged provider/database work counts. No search, source, model, extraction,
browser, or token budget changed. Compact results are in
[`partial-failure-salvage.json`](../benchmarks/partial-failure-salvage.json).

## Regression recovery coverage

The [recovery report](quality-recovery.md) records specific historical function
changes, all ten named golden fixtures, and their outcomes. The same synthetic panel
ran against isolated archives of all four requested historical revisions. It measures
downstream behavior with frozen evidence, not the accuracy of live biographies.
Additional guards verify genuinely dated news/archive evidence still ages, multiple
roles in one quote cannot create an invented pair, final CSV/XLSX normalization keeps
input/provenance intact, disconnected namesakes abstain, partial education is grounded,
and duplicate credentials remain suppressed. Plaintext log fixtures validate useful
decision detail, final identity eligibility, bounded payloads, and sensitive-data
exclusion. Mixed job outcomes retain completed records and expose the new display label.

The 100-person offline benchmark took 55.161 seconds before and 61.779 seconds after;
single-run timings are non-gating and do not establish a speedup. Provider/search and
database work counts are identical. The [benchmark record](../benchmarks/README.md#regression-recovery-measurement)
contains all four sizes and explains the intentional confidence/hash differences.

## Current refinement and shared-library coverage

Source-policy fixtures reject LinkedIn and social host trees, redirect destinations,
and `social` classifications before evidence admission. They verify extraction makes
no model call for blocked URLs, blocked evidence cannot affect selected fields or
confidence, and Wikipedia and official sources remain eligible. Legacy persisted
results are re-reconciled from eligible retained evidence before JSON results,
ordinary exports, and library saves, without rewriting their historical ledgers.

Display fixtures cover common Spanish/French/Portuguese English equivalents, controlled
degrees, subjects, conservative name/title/institution casing, acronyms, particles,
apostrophes, hyphens, and unchanged raw evidence. Filename fixtures cover stable UTC
job-start naming, original row-order list selection, fallback names, Unicode, unsafe
characters, length limits, and Content-Disposition without job IDs.

Library fixtures exercise explicit CSV/XLSX saves, identical normal-export content,
metadata-only listing, authenticated shared downloads/deletes, tokenless fail-closed
access, rejected formats/arbitrary uploads/incomplete jobs, file-size limits, concurrent
count/byte quotas, and saved-file survival after job deletion. Completing or exporting
a job saves nothing automatically. Provider construction/calls are forbidden in the
save fixture; no new research tables or model work are introduced.

## Current coverage

Flexible-input fixtures cover Unicode header normalization, snake/kebab/camel/punctuation
variants, strong and generic person-name aliases, field-specific precedence, contextual
organisation/role/country/location/university/subject/year mappings, unknown columns,
explicit overrides, ambiguous name candidates, missing-name diagnostics, and numeric,
URL, email, and empty false positives. The production batch API fixture now exercises
automatic inference for CSV and XLSX without a manual mapping.

The multi-source fixtures cover the requested product-model cases A–K:
multi-source field assembly and provenance, independent university corroboration,
conflicts, three credentials, same-credential merging, latest paired employment,
government offices, contribution-based representative links, record-specific links,
name-only cross-source identity, and LinkedIn exclusion before advisor/retrieval.
Additional checks cover legacy/new profile JSON, multi-row rich exports, source-column
isolation, weak-source link gating, mirrored evidence, and nested benchmark
normalization.

Accuracy fixtures cover seed-aware identity anchors, local name/clue association,
explicit affiliation contradictions retained for corrective corroboration, and bounded
cross-source bridges, including provisional unanchored namesakes and specific
known-attribute anchors. They also verify that Wikipedia is forced to `encyclopedia`
authority regardless of labels or domain overrides. Retrieval checks cover one browser
attempt for each eligible static result and one bounded search-recovery queue when
initial retrieval fails or produces no identity-eligible grounded claims.

Data-quality fixtures cover the eight controlled degree types, English and multilingual
aliases, derived subjects from grounded degree titles, qualification-label suffixes,
generic-degree metadata,
compound-level splitting, non-degree filtering, and ambiguous education retained as
alternatives without manufacturing credentials. They also cover compatible and
distinct same-type credentials, Unicode/mojibake, the 10% selected-value floor,
field-versus-record review, current-role freshness, executive/board and
government/party priority, the public-office taxonomy, country and public-residence
rejection, collision-safe XLSX highlighting, and bounded empty-search fallbacks.

## Hardening regression coverage

The focused pre-edit baseline was **219 passed**. Added checks cover bounded
retrieval and extraction, domain fairness, out-of-order responses, atomic retry/token
budgets, cancellation, lease-renewal failures, frozen checkpoints, redirect aliases,
JSON-only content fingerprints, cache/job isolation, browser context cleanup/restart,
HTTP connection ownership, safe input/Unicode, malicious prompt text, and malformed
spreadsheets. Grounded values, confidence, provenance and terminal states remain
equivalent in the offline before/after fixtures. Prompt-version metadata is recorded
accurately and excluded from that material-evidence comparison.

Repeated checkpoints use nine SQL statements for both one and 100 claims; unchanged
ledger rows are not rewritten. Completed-result reads retain six SELECTs. Explicit
navigation removal preserves literal biography/education evidence in a fixture while
reducing its context from 772 to 172 characters. These are fixture measurements,
not predictions of live provider or Railway performance.

## Focused discovery failure-path coverage

The 2026-09-20 source-discovery regression fixtures exercise the production provider
and orchestration path with network responses replaced locally. They cover:

| Case | Expected contract |
| --- | --- |
| Valid `url_citation` | Canonical citation URLs become source candidates and valid advisor output proceeds to retrieval. |
| No citation | Model prose URLs remain ignored; bounded deterministic fallback queries run when budget remains, then the zero-coverage person fails with `research_status=insufficient_evidence` while retaining `NO_SEARCH_CITATIONS`. |
| Malformed citation metadata | Invalid annotations do not become candidates or cause an opaque exception. |
| Advisor provider or HTTP failure | The zero-coverage task preserves `SOURCE_ADVISOR_PROVIDER_ERROR`; its attempt record retains the HTTP category and status. |
| Advisor invalid JSON | The provider attempt records `OPENROUTER_SCHEMA_ERROR` and retains its safe diagnostic reason. |
| Advisor schema mismatch | The task records a validation/schema code without logging the raw response. |
| Empty candidates after filtering | The zero-coverage person fails with `research_status=insufficient_evidence` while retaining `NO_ELIGIBLE_CANDIDATES`. |
| Budget exhausted before advisor | No request is sent and the bounded budget outcome remains distinguishable from a zero-token provider attempt. |
| OpenRouter 4xx/5xx | HTTP status, retry number, request/body flags, and safe terminal error code are retained. |
| Valid discovery with no selected source | The zero-coverage person fails with `research_status=insufficient_evidence` while retaining `NO_SELECTED_SOURCES`. |

Schema fixtures also verify that strict OpenRouter schemas recursively omit Pydantic
`default` annotations while retaining required fields and closed objects. Usage
fixtures verify `usage.server_tool_use.web_search_requests`, and attempt fixtures
verify the safe diagnostic fields used to explain zero-token records. These checks are
offline and make no paid OpenRouter calls.

## Migration coverage

The partial-failure salvage pass adds no migration or schema change. Alembic logging setup
preserves application loggers; the single migration head remains `202609290001`.
The source/display refinement uses existing profile/source JSON and adds no export
columns. The shared file library adds migration `202609290001` with one independent
artifact table and no research-table changes or job foreign keys. Fresh upgrade,
schema-drift check, downgrade, and PostgreSQL SQL compilation are tested. Deployment
must apply the migration before starting the updated web and worker services.

- Real OpenRouter request/response adapters under HTTP fixtures: server-tool syntax,
  citation-only discovery, canonical deduplication, missing or malformed results,
  response-size limits, safe errors, retry accounting, unknown usage, model-ID
  guards, and immediate stopping when upstream reports exceeding the tool cap.
- Production orchestration: shared source/extraction model IDs, bounded follow-up
  queries (preserving search exclusions and quoted phrases), pre-request
  tool/result/token reservations, pending-candidate reuse,
  persisted usage, and public-address validation before retrieval.
- Existing API/authentication, jobs, leases, fencing, cancellation, cache, migrations,
  static/structured/browser retrieval, grounding, deterministic confidence, and
  CSV/XLSX ingestion/export checks remain in the suite.
- Completed batch results retain identical serialized evidence and usage while
  requiring six SELECTs for both one-person and eight-person fixtures.

## Deployment checks still required

- **PostgreSQL runtime:** six tests skipped because `TEST_POSTGRES_URL` is unset.
  CI supplies a disposable PostgreSQL database; these tests use isolated schemas.
- **Live OpenRouter and Cloudflare:** configure the production variables and run one
  person plus a small batch through the normal API. Verify source-model tool support,
  both roles' structured JSON support, citation metadata, evidence, and usage. Ensure
  OpenRouter workspace settings permit Exa and no forced legacy web plugin adds
  implicit search. Search costs are additional to model tokens.
- **Cloudflare protections:** the separately deployed Worker must enforce upstream
  public-address, redirect, and social-source host-policy checks before every hop; its code
  is outside this repository. Backend final-URL validation cannot undo a request that
  an external Worker already followed.
- **Chromium:** local browser fixtures do not establish real launch/rendering or
  sandbox compatibility. Verify both on the deployment host when fallback is enabled.
- **Docker and Railway:** no local Docker build or Railway deployment was executed.
  The GitHub workflow includes a Docker build/import check and PostgreSQL tests;
  consult its run for the pushed revision before deployment.

The [README deployment overview](../README.md#deployment) lists the web/worker
commands and migration owner. Run both services against the same database and
configuration, apply migrations, and check `/health` then `/ready`. Readiness checks
configuration and runtime prerequisites without making paid calls; it does not
prove live model compatibility or successful Chromium rendering.

No live provider request was made during this recovery pass. The smallest useful
next quality panel is Andrew Marsh + Entergy, Anutin Charnvirakul, Jafar Hassan,
Tshering Tobgay, Elizabeth Adams without context, and Isaac Lungu through the normal
production API/settings. Inspect actual retrieved evidence and the new decision
traces for identity, current roles, institution specificity, links, and credentials.
