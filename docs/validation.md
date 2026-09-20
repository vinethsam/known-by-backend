# Validation record

Validated locally for the focused discovery fix on **2026-09-20**, Windows, Python
**3.14.7**. Docker and CI target Python 3.13. Trial jobs use the production API, queue,
worker, providers, and settings; automated tests replace external network responses
without a second research pipeline.

## Executed checks

| Check | Result |
| --- | --- |
| Full test suite | **180 passed, 5 skipped**; PostgreSQL integration requires `TEST_POSTGRES_URL` |
| Ruff lint and formatting | Passed |
| Python compilation and application/compatibility imports | Passed |
| Dependency consistency | No broken requirements |
| Real Uvicorn process startup and localhost `GET /health` | HTTP 200, `{"status":"ok"}` |
| SQLite Alembic upgrade/downgrade and readiness | Passed in the suite |
| PostgreSQL migration DDL and locking SQL compilation | Passed offline |
| Git diff whitespace check | Passed |

Two upstream deprecation warnings concern Starlette's TestClient transport and its
AnyIO portal alias. Neither failed a test. No paid provider requests were made.

## Focused discovery failure-path coverage

The 2026-09-20 source-discovery regression fixtures exercise the production provider
and orchestration path with network responses replaced locally. They cover:

| Case | Expected contract |
| --- | --- |
| Valid `url_citation` | Canonical citation URLs become source candidates and valid advisor output proceeds to retrieval. |
| No citation | Model prose URLs remain ignored; the person ends `review_required` with `NO_SEARCH_CITATIONS`. |
| Malformed citation metadata | Invalid annotations do not become candidates or cause an opaque exception. |
| Advisor provider or HTTP failure | The zero-coverage task preserves `SOURCE_ADVISOR_PROVIDER_ERROR`; its attempt record retains the HTTP category and status. |
| Advisor invalid JSON | The provider attempt records `OPENROUTER_SCHEMA_ERROR` and retains its safe diagnostic reason. |
| Advisor schema mismatch | The task records a validation/schema code without logging the raw response. |
| Empty candidates after filtering | The person ends `review_required` with `NO_ELIGIBLE_CANDIDATES`. |
| Budget exhausted before advisor | No request is sent and the bounded budget outcome remains distinguishable from a zero-token provider attempt. |
| OpenRouter 4xx/5xx | HTTP status, retry number, request/body flags, and safe terminal error code are retained. |
| Valid discovery with no selected source | The person ends `review_required` with `NO_SELECTED_SOURCES`. |

Schema fixtures also verify that strict OpenRouter schemas recursively omit Pydantic
`default` annotations while retaining required fields and closed objects. Usage
fixtures verify `usage.server_tool_use.web_search_requests`, and attempt fixtures
verify the safe diagnostic fields used to explain zero-token records. These checks are
offline and make no paid OpenRouter calls.

## Migration coverage

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

- **PostgreSQL runtime:** five tests skipped because `TEST_POSTGRES_URL` is unset.
  CI supplies a disposable PostgreSQL database; these tests use isolated schemas.
- **Live OpenRouter and Cloudflare:** configure the production variables and run one
  person plus a small batch through the normal API. Verify source-model tool support,
  both roles' structured JSON support, citation metadata, evidence, and usage. Ensure
  OpenRouter workspace settings permit Exa and no forced legacy web plugin adds
  implicit search. Search costs are additional to model tokens.
- **Cloudflare protections:** the separately deployed Worker must enforce upstream
  public-address and redirect checks; its code is outside this repository.
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

No OpenRouter key is available in the local validation environment for the focused
2026-09-20 fix, so no live provider request is claimed. The next deployment check
should use one bounded person through the normal production API and confirm citation
counts, selected-source counts, persisted attempt diagnostics, and the terminal code.
