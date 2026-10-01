# Validation record

Validated locally after the regression recovery pass on **2026-10-01**, Windows,
Python **3.14.7**. Docker and CI target Python 3.13. Trial jobs use the production API,
queue, worker, providers, and settings; fixtures replace external network responses
without a second research pipeline or paid provider calls.

## Current checks

| Check | Result |
| --- | --- |
| Final full test suite | **730 passed, 6 skipped, 2 warnings** in **29.38 seconds**; PostgreSQL integration requires `TEST_POSTGRES_URL` |
| Frozen golden panel | **14 passed** across all ten requested people; the same panel at `359202a` passed 2 and failed 12 |
| Focused checks | Final database/migration, decision-trace, and education check: **34 passed**; identity, confidence, role, normalization, export, and outcome cases also pass in the full suite |
| Offline production-pipeline benchmark | 1, 5, 25, and 100 people with mocked HTTP; before/after search, model, extraction, SQL, cache, and result-read counts unchanged |
| Ruff lint | `ruff check .` passed |
| Ruff formatting | All Python files formatted |
| Python compile/import | Application and compatibility entry points passed |
| Alembic | Single head `202609290001`; fresh SQLite upgrade/check/downgrade passed with no schema drift; PostgreSQL BYTEA DDL compiled offline |
| Git diff whitespace check | Passed |

Two upstream deprecation warnings concern Starlette's TestClient transport and its
AnyIO portal alias. Neither failed a test. No paid provider requests were made.

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
| No citation | Model prose URLs remain ignored; bounded deterministic fallback queries run when budget remains, then the person ends `completed` with `research_status=insufficient_evidence` and `NO_SEARCH_CITATIONS`. |
| Malformed citation metadata | Invalid annotations do not become candidates or cause an opaque exception. |
| Advisor provider or HTTP failure | The zero-coverage task preserves `SOURCE_ADVISOR_PROVIDER_ERROR`; its attempt record retains the HTTP category and status. |
| Advisor invalid JSON | The provider attempt records `OPENROUTER_SCHEMA_ERROR` and retains its safe diagnostic reason. |
| Advisor schema mismatch | The task records a validation/schema code without logging the raw response. |
| Empty candidates after filtering | The person ends `completed` with `research_status=insufficient_evidence` and `NO_ELIGIBLE_CANDIDATES`. |
| Budget exhausted before advisor | No request is sent and the bounded budget outcome remains distinguishable from a zero-token provider attempt. |
| OpenRouter 4xx/5xx | HTTP status, retry number, request/body flags, and safe terminal error code are retained. |
| Valid discovery with no selected source | The person ends `completed` with `research_status=insufficient_evidence` and `NO_SELECTED_SOURCES`. |

Schema fixtures also verify that strict OpenRouter schemas recursively omit Pydantic
`default` annotations while retaining required fields and closed objects. Usage
fixtures verify `usage.server_tool_use.web_search_requests`, and attempt fixtures
verify the safe diagnostic fields used to explain zero-token records. These checks are
offline and make no paid OpenRouter calls.

## Migration coverage

The regression recovery adds no migration or schema change. Alembic logging setup
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
