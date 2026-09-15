# Implementation contracts

Architecture and integration decisions are centralized around person research.
Providers, retrieval, evidence, persistence and export have bounded responsibilities.

The existing `/process` contract and root `main:app` entrypoint remain supported.
Production uses PostgreSQL + SQLAlchemy synchronous short transactions, called from
thread pools in async work. No session spans a network await. A separate async
worker leases person tasks with fencing tokens and renewals. Alembic owns schema.

Shared data contracts are in `app/schemas/__init__.py`; settings use uppercase
attributes. Credentials are Pydantic SecretStr and must never be logged.
All IDs are UUID strings. Timestamps are UTC. Stored JSON is `model_dump(mode="json")`.
All profiles retain seven decisions, including explicit missing decisions.

Provider integration: Brave web search supplies URLs; OpenRouter role A supplies
queries and decisions keyed by candidate IDs, never invented URLs. Role B supplies
claims and concise literal evidence. Identity is scored separately from authority.
Final confidence uses deterministic Python. Raw claims survive flat selection.

Retrieval: Worker POST uses configured full endpoint, body `{"url": ...}` and
`X-Worker-Secret`. Playwright only falls back for successful static application
shells; blocked pages are errors. All target URLs, redirects and browser requests
must pass public-address validation. A local HTTP Worker is allowed
in development; this never permits private research targets.

Within-job cache stores bounded retrieved content independent of person identity.
No extracted person claims are reused for a different person. Structured acquisition
configuration is operator supplied, generic, and bounded (filtered, bulk, paginated).

Trial runs and deployment use the same production pipeline and settings. Automated
tests replace external providers at their normal interfaces, without any separate
source restriction, site-specific acquisition code or alternate research pipeline.
