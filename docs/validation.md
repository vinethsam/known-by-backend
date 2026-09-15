# Validation record

Validated locally on **2026-09-15**, Windows, Python **3.14.7**. Docker/CI targets
Python 3.13. This record separates checks executed here from checks prepared for CI
or a configured deployment.

## Executed checks

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **122 passed, 5 skipped** |
| `python -m ruff check .` | Passed |
| `python -m ruff format --check .` | Passed |
| `python -m compileall -q app main.py worker.py config.py schemas.py fetcher.py processing.py` | Passed |
| `python -m pip check` | No broken requirements |
| Uvicorn process startup with a temporary development database path | Passed |
| Actual localhost HTTP `GET /health` from that process | HTTP 200, `{"status":"ok"}` |
| SQLite Alembic upgrade/downgrade and readiness | Passed in the suite |
| PostgreSQL migration DDL and locking SQL compilation | Passed offline |
| `git diff --check` | Passed; Git reported normal Windows newline-conversion notices |

The suite reported two upstream deprecation warnings from Starlette's TestClient
and its AnyIO portal alias. They did not fail any test. Production provider adapters
use the explicitly constrained HTTPX 0.28 client; browser transport internals are
constrained to HTTPcore 1.0.9.

## What the suite exercises

- API authorization, input validation, upload size limits, immediate durable job
  creation, status/results/cancellation/export, readiness and the existing `/process`.
- The production research orchestrator, actual Brave/OpenRouter/Worker adapters with
  HTTP fixture transports, separate worker execution, both model roles, source reuse,
  grounded output, persisted provenance and bounded retries/token reservations.
- URL canonicalization, public-address checks, private redirects, DNS rebinding
  protection through pinned connections, TLS hostname preservation, browser route
  limits, popup/context routing and main-document provenance in the presence of iframes.
- Conservative HTML conversion, late-page person content, relevant bounded chunks,
  structured filtered/bulk/paginated acquisition and explicit publication metadata.
- Source hierarchy, identity ambiguity, literal claim grounding, dates, normalization,
  corroboration, mirrored content, recency, conflicts, coherent degree/employment
  selection, missing values and confidence independent of coverage.
- Concurrent leases/completions, stale-worker fencing, cancellation races, exhausted
  lease recovery, checkpoint retention, usage/evidence retention across attempts,
  and temporary cache size, expiry and cleanup.
- CSV/XLSX parsing, original-row preservation, malicious worksheet dimensions,
  enrichment-header collisions, formula-safe exports and invalid XML characters.

Automated tests substitute external network responses at normal provider interfaces.
There is no alternate research pipeline, temporary source allowlist or separate set
of trial settings. Real trial jobs use the same API, queue, worker and configuration
as production. HTTP fixture tests do not measure real model quality or web coverage.

## Checks not executed locally

1. **Five PostgreSQL runtime checks** skipped because `TEST_POSTGRES_URL` is unset and
   no local PostgreSQL service was available. They exercise production Store methods
   and migrations inside a uniquely named schema, removed after each test. CI supplies
   a disposable PostgreSQL service. Use a dedicated test database when running them
   yourself; the test account needs permission to create schemas.
2. **Real Brave, OpenRouter and Cloudflare requests** were not made. Provider keys,
   model IDs and a verified Worker endpoint/secret must be configured. No API credits
   were spent. The external Worker code is outside this repository, so its own
   redirect/public-destination protections could not be inspected here.
3. **Real Chromium launch/rendering** was not tested locally. Playwright is installed;
   its browser runtime must be installed and checked on the intended host. Routing
   tests use browser fixtures and do not prove Chromium sandbox compatibility.
4. **Docker build and Railway deployment** were not run. The Dockerfile and CI build
   job are supplied. CI was not dispatched because changes have not been committed
   or pushed. A successful local test run is not evidence of a successful deployment.

## Deployment verification

Follow the [README setup and manual checklist](../README.md). Configure both model
roles, Brave and the existing Worker; migrate PostgreSQL; start web and worker from
the same revision and environment settings. Confirm `/health`, then `/ready`. Run
one real person followed by a small CSV/XLSX batch, inspect evidence and usage, and
verify a real dynamic page can launch Chromium safely before relying on fallback.

Jobs and output retain public personal information, so the operator must choose
appropriate retention and backups. Confidence remains a documented evidence heuristic;
review flags and preserved source evidence are part of the intended workflow.

No commit, push, remote change or deployment was performed.
