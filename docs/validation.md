# Validation record

Validated locally on **2026-09-18**, Windows, Python **3.14.7**. Docker and CI target
Python 3.13. Trial jobs use the production API, queue, worker, providers, and settings;
automated tests replace external network responses without a second research pipeline.

## Executed checks

| Check | Result |
| --- | --- |
| Full test suite | **165 passed, 5 skipped** |
| Ruff lint and formatting | Passed |
| Python compilation and application/compatibility imports | Passed |
| Dependency consistency | No broken requirements |
| Real Uvicorn process startup and localhost `GET /health` | HTTP 200, `{"status":"ok"}` |
| SQLite Alembic upgrade/downgrade and readiness | Passed in the suite |
| PostgreSQL migration DDL and locking SQL compilation | Passed offline |
| Git diff whitespace check | Passed |

Two upstream deprecation warnings concern Starlette's TestClient transport and its
AnyIO portal alias. Neither failed a test. No paid provider requests were made.

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
