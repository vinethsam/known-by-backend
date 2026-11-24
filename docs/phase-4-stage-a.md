# KnownBy Phase 4 · Stage A

Reviewed backend `6b39846` and frontend `117342e` on 10 November 2026. The proposed handoff is the source-ordered claim proposal stream just before `validate_claims`. The offline result measures the downstream pipeline on six synthetic cases with separately authored evidence labels. The available historical records do not support a fresh estimate of live extraction accuracy or per-operation cost.

## 1. Verified architecture

Queried the combined Graphify graph for the pipeline and tests, then verified the call sites below. Graphify's call paths included import edges. The source inspection resolved the nested callbacks, service injection and deferred structured-extraction import.

| Stage | Runtime path |
| --- | --- |
| Lease and discovery | `worker.run_worker` constructs the provider, search adapter, cached retrieval service and `ResearchOrchestrator`. `process_lease` passes the seed, checkpoint and resume state to `.research()`. `_research` handles preferred URLs and operator plans, builds and plans queries, and invokes `BudgetedModel.search` → `OpenRouterSearchProvider.search`, which parses provider citations. `app/worker.py:208,355`, `app/research/orchestrator.py:183,837,1215,1295`, `app/providers/search.py:75`. |
| Source admission and retrieval | `validate_and_process` applies source policy, canonical URL deduplication, metadata identity checks, `SourceAdvisor.validate` for uncertain candidates and candidate ranking. Prefetch overlaps retrieval while application remains ranked. `RetrievalService.retrieve` checks the job cache, uses `StaticFetcher` through the static HTTP worker, then invokes guarded Playwright for eligible failures or unusable content. Structured bulk plans can locally filter fetched JSON into `captured_json`. `app/research/orchestrator.py:643`, `app/research/discovery.py:23`, `app/retrieval/service.py:166,402,483,610`, `app/retrieval/browser.py:344`. |
| Page to evidence | `process_candidate` checks redirect policy and HTTP status, then calls `prepare_content` and `_process_source`. BeautifulSoup and markdownify remove chrome and preserve links, headings and table text. The processor compacts adjacent duplicate evidence and prioritizes person-relevant text before bounded chunking. `_process_source` hashes the body, captures metadata, assigns authority and assesses identity. Duplicate bodies and rejected or sub-threshold identities skip extraction. `app/processing/__init__.py:73,189,295,381`, `app/research/orchestrator.py:144,384`. |
| Existing deterministic and LLM extraction | `structured_evidence` projects owned JSON-LD, microdata and JSON facts, resolves bounded references, and assigns credential groups only where relationships are explicit. `process_candidate` grounds those batches first. Fully grounded structured projections alone are dropped from the residual text. `extract_chunks` then returns responses in chunk order, with bounded concurrency, and the orchestrator calls the same grounding function. `app/research/extraction.py:99,207,435`, `app/research/orchestrator.py:478,528,552`. |
| Grounding and identity | `validate_claims` checks source policy, subject identity, literal quote and value, disputed local context and observed link URLs. It strips unsupported dates, expands literal compound qualifications and derives explicitly supported subjects. It creates normalized `EvidenceClaim`s. Deduplication includes source, value, date, currentness and fact group. `eligible_identity_source_ids` rechecks eligibility for selection using seed anchors and constrained corroboration across independent sources. `app/research/claims.py:38,192`, `app/research/identity.py:288,424,759`. |
| Selection, confidence and provenance | `reconcile` selects employment pairs with temporal evidence and clusters education by source-local group and compatible credentials across sources. Each `ProfileRecord` has one qualification scope. The legacy fields mirror a primary record. `_representative_link` ranks supporting source URLs. Confidence combines authority, identity, directness, quality, temporal strength, corroboration and conflict. Review, evidence coverage and supplied-value projection are separate. `app/research/reconciliation.py:192,425,644,931,1395,1669`, `app/research/confidence.py:114,315`, `app/research/seed_provenance.py:102`. |
| Durable results and output | The worker saves checkpoints and commits the profile, grounded claims, sources and usage ledger. The API strips private recovery state. Terminal jobs clear the retrieval cache. The frontend renders seven fields. Standard CSV/XLSX has seven value columns, six confidence columns and Source URLs. Compact exports can combine credentials, so relationship evaluation uses durable record scopes. `app/worker.py:208`, `app/db/store.py:374,813,928,1129`, `app/export/schema.py:8`, `app/export/formats.py:114`, `knownby/src/data.js:2`, `knownby/src/App.jsx:809`. |

## 2. Extraction contract and proposed handoff

- `RetrievedPage` holds the requested and final URLs, HTTP status, content type, raw body, retrieval method and optional captured JSON. `PreparedContent` holds base text and `(evidence_text, ExtractionResponse)` structured batches. `SourceRecord` binds the evidence to the person, URL, source identity, authority and content hash. `app/retrieval/contracts.py:82`, `app/processing/__init__.py:381`, `app/schemas/__init__.py:285`.
- `extract_chunks` builds the `claims-v9` request from sanitized seed context, final source URL, chunk index and exact evidence. `BudgetedModel.complete` handles receipts and limits around `OpenRouterClient.complete`, which parses and validates the reply. `ExtractionResponse` accepts up to 60 `ExtractedClaim`s. Each requires a seven-field enum, literal value of 1–500 characters, literal evidence of 3–800 characters and a `subject_name`. Location, directness, claim type, temporal context, dates, currentness and `fact_group` are optional. `app/research/extraction.py:435`, `app/prompts/extraction.py:3`, `app/research/budget.py:152`, `app/providers/openrouter.py:164,656`, `app/schemas/__init__.py:310`.
- At `app/research/orchestrator.py:528–581`, a deterministic producer could emit the existing `(exact chunk, ExtractionResponse, producer version)` tuple. The consumer can continue to bind the current `SourceRecord`, ground the proposal and merge the resulting `EvidenceClaim`s. Use distinct source-local groups for each credential and employer/title pair. Keep unsupported components and dates empty. Let reconciliation choose Profile Link and retain the existing structured pass.
- An empty batch means the rules found no candidate. An invalid or interrupted batch needs its own outcome so one failure cannot discard grounded siblings or falsely mark residual text as processed. Scope applied-chunk keys to person, source, content and producer/rule version. A later optional fallback should keep its provider receipts and budgets separate and receive unresolved evidence in stable order. `app/research/extraction.py:414,435`, `app/research/orchestrator.py:543,582`, `app/research/budget.py:56`.

## 3. Evidence inventory

No matching records or original evidence for **Lisa Su, Jeff Horwitz, Marina Tabassum, Carrie Mae Weems or Tobias Bingold** were found in the inspected local checkouts, research artifacts or scoped Git history. No local production database snapshot or raw source capture was found. For those five people, original identifiers and seed context, source URLs and types, source bodies and metadata, claim ledgers, final decisions and per-person telemetry are unavailable here. The historical cohorts below cannot be attributed to those names from the available records.

| Artifact | Available locally or retained by design |
| --- | --- |
| [October 26 research summary](../benchmarks/october26-corrective-research.json) | A different cohort has seed context, processed domains, claim/eligibility counts, selected relationships and provider failure aggregates. Five attempts in the identity-gate example had 173 grounded claims and zero eligible sources or selected claims. This local summary cannot establish which grounded claims were correct. The before/after cases described in [corrective research](corrective-research.md) are reconstructions. |
| Durable job records, if supplied as an approved snapshot | Store persists the seed, latest checkpoint or final profile, grounded claims including structured/model producer, source URLs/types/hash/identity/metadata, decision links and review, and usage. Rejected proposal bodies are absent from the result ledger. Counters and diagnostic reason summaries can survive. Successful typed replies can survive as private hash-keyed receipts. The public result projection drops those receipts and there is no explicit, reversible chunk-to-reply ledger. `app/db/store.py:374,813,948`, `app/research/budget.py:56`, `app/schemas/__init__.py:528`. |
| Evidence bodies and exports | Raw HTML defaults to off and compacted text defaults to on. The latter joins initial bounded chunks before fully grounded structured projections are suppressed. Source records have no separate captured-JSON field. The retrieval cache holds exact page data with a one-hour default TTL and is cleared at terminal status. The result library retains export bytes with no evidence ledger. `app/config.py:164`, `app/research/orchestrator.py:154,512`, `app/retrieval/service.py:447`, `app/db/store.py:1129`, [result library](library.md). |

Exact historical replay requires approved local copies of original page or projections, the exact submitted chunks and associated parsed responses, source and seed context, and the original policy and clock. A compacted body or an export alone cannot reproduce the extraction inputs and outputs. No new discovery, retrieval or provider requests were made for this inventory.

## 4. Offline dataset and method

The [v1 cases](../benchmarks/stage_a_cases.json) contain original synthetic HTML for eight URLs, supplied context, source types, and 26 scripted proposals across seven recorded replies. Labels were authored directly from the fixture evidence before replay. A null label means unverified and is excluded from accuracy scoring. An empty list means the fixture contains no qualifying evidence for that field or relationship. BSc and MSc labels use the existing standardized display vocabulary.

| Case | Scope and observed behavior |
| --- | --- |
| A01 · Same institution | University prose, two degrees at one university. The scripted reply omits Geology. Both degrees are selected, and the MSc row remains incomplete. |
| A02 · Linked issuers | Cross-script JSON-LD references, two issuers and two distinct subjects. Both full qualification tuples survive. A residual heading has no recorded reply and is reported as such. |
| A03 · Award bio | Short award biography supports attendance only. The proposed MFA is rejected as `UNGROUNDED_VALUE`. |
| A04 · Adjacent namesake | Corporate directory with a similar adjacent name. The neighbor's university is rejected as `CLAIM_SUBJECT_MISMATCH`. The target's employer/title pair survives. |
| A05 · Old coverage | Corporate bio and archived news, current title, older title and former employer. Four historical or stale claims remain grounded outside selected support. |
| A06 · Simultaneous titles | Two equally dated employer biographies disagree on the current title. The replay flags `CURRENT_ROLE_CONFLICT` and `MAJOR_CONTRADICTORY_EVIDENCE`. The title, employment pair and link remain unverified for scoring. |

Run from the backend root with the normal backend dependencies installed.

```powershell
New-Item -ItemType Directory -Force output/phase-4-stage-a
python -m benchmarks.stage_a_replay --output output/phase-4-stage-a/replay.json
python -m benchmarks.stage_a_replay --timing --output output/phase-4-stage-a/replay-timed.json
```

The validation run used the local Python 3.12 runtime and an ignored dependency directory populated from the existing package cache. On this workspace, the same environment can be used by setting `$env:PYTHONPATH = (Resolve-Path .test-tmp/stage-a-deps).Path` and using `$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` in place of `python`.

The [isolated runner](../benchmarks/stage_a_replay.py) uses code defaults and a fixed 10 November 2026 clock. It applies production evidence preparation, source identity and policy checks, structured extraction, claim grounding, deduplication and reconciliation. It binds recorded replies to exact residual chunks by SHA-256 and fails on drift. Output retains proposals, evidence spans, rejection diagnostics, grounded claims, source links, scoped decisions, confidence and review. Local IDs are stable and operational timestamps are excluded. No workers, databases or HTTP/model clients are opened. The replay exercises saved proposals through downstream logic. It provides no measurement of live LLM proposal quality and omits discovery, retrieval, provider fallback, job orchestration and persistence.

## 5. Synthetic baseline results

| Field or relationship | Scored cases | TP | FP | FN | Precision | Recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Full Name | 6 | 6 | 0 | 0 | 100% | 100% |
| Organisation | 6 | 3 | 0 | 0 | 100% | 100% |
| Job Title | 5 | 2 | 0 | 0 | 100% | 100% |
| University Name | 6 | 4 | 0 | 0 | 100% | 100% |
| Degree Type | 6 | 4 | 0 | 0 | 100% | 100% |
| Subject | 6 | 3 | 0 | 1 | 100% | 75% |
| Profile Link | 3 | 3 | 0 | 0 | 100% | 100% |
| Exact university–degree–subject tuple | 6 | 4 | 1 | 1 | 80% | 80% |
| Current employer–title pair | 5 | 2 | 0 | 0 | 100% | 100% |

Field scoring uses unique researched values per person across scoped records. Tuple scoring counts complete row matches, including reviewed partial rows when no further facts are supported. The incomplete A01 MSc row creates one extra partial tuple and one missing full tuple. The exact chunk contains the missing subject, and the scripted input contains no proposal for it. The replay therefore isolates a proposal-stage omission in this fixture. No reviewed current/former or identity association was incorrectly selected. Three Profile Links and one job title remain unverified.

- **Evidence coverage.** Production telemetry's evidence-linked definition marks 25/42 person-field slots supported (59.5%), including slots with unverified accuracy labels. Across the reviewed values, 25/26 supported values were selected (96.2% recall). Full Name counts only with researched corroboration. Legacy profile coverage uses six non-name fields and has a different denominator.
- **Claim flow.** 33 proposed, 31 grounded, two rejected, and five grounded outside selected support. Four of the latter are A05 former/stale-role claims. One is the competing current title in A06. All 34 researched decisions across scoped records resolve to a stored quote and source URL in the replay. One person is flagged for review. Profile confidence spans 67.71–91.32 and is recorded separately from factual scoring.
- **Local speed.** On this Windows workspace using Python 3.12, 20 in-process replays after one warm-up had median 48.79 ms for the six cases (47.75–65.09 ms range). Median summed preparation, grounding and reconciliation were 12.08, 2.39 and 28.55 ms respectively. Total includes fixture parsing and evaluation overhead. This is local downstream work, with zero live model calls and zero API spend. LLM latency, queue time and live throughput cannot be measured with this replay.

## 6. Historical production speed and cost

| Reference | Telemetry-supported slots | Model calls | Reported cost | Active processing |
| --- | ---: | --- | --- | --- |
| November 5, five attempted and four enriched | 23/35 (65.7%) | 60, including 45 extraction, 10 validation and 5 discovery | $0.11215195 actual total, $0.02243/attempted, $0.02804/enriched and $0.00488/telemetry-supported slot | Unavailable |
| Recovered five-person reference | 29/35 (82.9%) | 78, including 47 extraction, 18 validation and 13 search | $0.03861/attempted. Search was about 53% of reported spend. | Mean 133 s per person after queue wait. Mean accumulated extraction about 75 s, validation 27 s and discovery 15 s. Stages can overlap. |

These are historical references with different workloads. The source summaries support 9.0 and 9.4 extraction calls per attempted person respectively. They lack the underlying attempt ledgers and comparable reviewed labels. Consequently, model latency, exact extraction/fallback/retry costs, prompt and completion totals, cost per independently reviewed fact, individual queue times, separate processing/persistence durations and live batch throughput remain unavailable for the cohorts. The October 26 diagnostics separately document seven schema failures at 3,000 completion tokens each, with missing finish reasons and bodies. [Historical telemetry](performance.md#phase-4-observations) and [October 26 diagnostics](corrective-research.md).

`UsageRecord` and `summarize_person` preserve actual provider cost when known and keep incomplete accounting separate. They distinguish operation, provider/model, tokens, latency, retries, fallback and failed attempts. `person_performance` logs queue, prior elapsed and active attempt separately. Batch terminal logs can measure wall time and throughput when retained. `app/providers/openrouter.py:704`, `app/schemas/__init__.py:485`, `app/research/telemetry.py:96,144,416,502`, `app/worker.py:152`, `app/db/store.py:1057`.

## 7. Regression gates and Stage B design

| Required protection | Existing regression anchors |
| --- | --- |
| Separate degrees and issuers, explicit degree/subject and missing information | `tests/test_reconciliation_refinement.py:162,327`, `tests/test_processing.py:357,400`, `tests/test_evidence.py:247,373,782`. |
| Current/former roles, title conflicts and namesakes | `tests/test_relationship_selection_regressions.py:78,102,132,154,175`, `tests/test_evidence.py:843,894`, `tests/test_identity_admission.py:159,198,227,270`, `tests/test_identity_components.py:117`. |
| Grounding, source provenance, confidence and review | `tests/test_evidence.py:439,487,542,606,688,1248`, `tests/test_partial_failure_salvage.py:184,225`, `tests/test_processing.py:335`. |
| Seven frontend fields and 14 export columns | `knownby/tests/fields.test.js:192,227`, `knownby/tests/results-state.test.js:84`, `tests/test_corrective_output.py:169,241,285,382,444`, `tests/test_batch_export.py:678`. |
| Durable checkpoints, retry, deduplication and ordered application | `tests/test_durable_edges.py:138,220`, `tests/test_concurrency.py:456,496,521,1094`, `tests/test_pipeline_api.py:710,761`. |

1. Start with a local qualification-only producer over the exact bounded chunks. Match explicit earned, received or completed degree/subject/institution syntax in a named person's sentence, section or education row. Use a small phrase dictionary and existing qualification normalization. Keep distinct groups for distinct degrees, including when institutions repeat. Leave unsupported dates, ambiguous lists and inferred affiliations unset.
2. Reuse JSON-LD and microdata. A scoped CSS selector using the existing HTML parser can help stable layouts when saved pages demonstrate a gap. Defer XPath and Trafilatura-style main-content recovery until real snapshots show evidence loss in processing. Broader recovery may change headings, links, tables, attribution and neighboring-person text. Phrase and local entity matching can follow spaCy-style patterns without introducing a new dependency. Defer role dictionaries and temporal employment rules until qualification behavior is measured.
3. Run the new producer and recorded proposals separately against the same frozen evidence, policy and labels. Compare exact tuple recall, reviewed precision, and preservation of identity, provenance and current-role decisions. Keep A01's missing subject as the smallest zero-provider target. Check that equivalent proposals preserve confidence/review and export behavior. Verify on approved held-out real evidence before claiming a quality gain.
4. Before activation, exercise an orchestrator scenario with both paths. New claims can change `stopped()` and gap queries even when discovery and retrieval are left intact (`app/research/orchestrator.py:349,939`). Avoid chunk suppression after a single match. Preserve identity admission, checkpoint recovery, request budgeting and extraction key scope. Separate extraction savings from model-assisted discovery and validation spend. Keep production activation and optional fallback for review after Stage A.

## 8. Implementation and remaining evidence gaps

Added this report, the versioned synthetic fixture, the isolated runner and [two focused replay tests](../tests/test_stage_a_replay.py). The tests guard repeatability with network/client/storage tripwires, preserve rejection and historical-role audit details, check conflict review and reject a mismatched chunk. The fixture fingerprint is `fdd34ebda48b115badd3207d37b60bad822cc67b45312d680b62e0ee26a72a06`. Two process runs with different hash seeds produced identical replay JSON (`2e11c4ff5bf22b79e6f3660f4e98487ed642bf2e0d56c39aa01ee4e934ab5b00`). The replay and timing samples are in the Git-ignored `output/phase-4-stage-a/` folder.

Validation passed for 261 targeted synchronous backend tests (including the two replay tests), all 70 frontend tests and Ruff on the new Python files. The broader backend suite and existing mocked workload harness were stopped when Windows asyncio stalled opening its local event-loop socket. Async and PostgreSQL integration coverage remains unverified in this workspace. The watcher refreshed the combined local Graphify graph. No production modules were edited.

The next comparison requires an approved evidence snapshot and independently reviewed labels to measure real accuracy, timing and cost on comparable people. Stage B has not started.
