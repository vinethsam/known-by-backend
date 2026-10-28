# KnownBy corrective release — October 28, 2026

This release corrects the demonstrated research, seed-output, frontend and saved-file problems within the existing architecture. No runtime dependency, configured model, confidence threshold, provider budget or Railway configuration was changed. No live paid research was performed.

## Confirmed causes and corrections

1. **Professional relationships:** historical filtering and role priority allowed early employment or secondary academic appointments to displace a principal practice. Grounded founding/practice context now informs selection, with historical/current/unspecified timing retained. Organisation and title stay within a compatible relationship. An unnamed relative's business and its paired title cannot support the selected practice.
2. **Identity:** PDL institutional aliases reached discovery but not institutional identity anchors. Semicolon-separated employers and a descriptive newspaper suffix also prevented legitimate matches. Institutional aliases now anchor identity without asserting current employment; equivalent anchors are deduplicated. Existing namesake and claim-grounding protections remain.
3. **Provider failures:** seven HTTP 200 parsing failures used exactly the configured 3,000 completion tokens. The response bodies were unavailable, so truncation is a supported diagnosis, not proven for every original attempt. Explicit length termination or cap-reaching parse failures now stop identical retries with an actionable diagnostic and retained usage. Transient failures retain bounded retries.
4. **Seed output:** supplied fields were retained internally but omitted from normal displayed values. All eight mapped fields now project after independent scoring, including tasks with no result. Input certainty remains 100%; research confidence and coverage count only independently selected evidence. Conflicts preserve both values and review reasons. Role/education fragments are not attached to an incompatible researched context.
5. **Exports:** raw input columns were appended to a variable export layout. One central 73-column schema now serves CSV, XLSX, history, saves and previews. Display casing is centralized; raw evidence and input remain intact. Yellow XLSX fills apply to actual review conditions. Person status and education-record status are separate.
6. **Frontend:** a broad span rule broke the shared horizontal badge layout. Terminal job status also preceded result hydration, creating the false empty flash. Shared styling and explicit loading/ready/empty/error transitions correct those defects. Save eligibility now includes finished partial jobs with useful successful results, and repeated clicks do not create duplicate submissions.
7. **Database:** existing saved artifacts lacked an in-app reader. The authenticated records endpoint adds bounded CSV/XLSX pagination and search. The frontend reuses its results table and field drawer. Legacy generated columns are recognized before appended administrative input, including the real October 26 duplicate `status`/`STATUS` headers.

## Research evidence

The original logs establish five grounded-but-ineligible cases: Achille Mbembe 65 grounded claims, Elahe Mohammadi 14, Jonathan Anderson 11, Zanele Muholi 35 and Gamini Wijesuriya 48; all had zero eligible sources. They do not contain the full claim ledger, page bodies or per-source identity signals. Exact original individual-source validity therefore remains unverified.

Focused offline reconstructions reproduce the defects against backend baseline `83cd7386f76ca6db832a6fee3071dbf16e3c9340`. Each of the five identity fixtures changes from **0 to 1 eligible source and 0 to 2 selected claims**; unrelated namesakes remain rejected. The separate multiple-employer fixture preserves legitimate affiliations without cross-pairing roles.

| Person | Before organisation | After organisation |
| --- | --- | --- |
| Renzo Piano | His Father's Construction Company | Renzo Piano Building Workshop |
| Zaha Hadid | Office for Metropolitan Architecture | Zaha Hadid Architects |
| I. M. Pei | Webb & Knapp | Pei Cobb Freed & Partners |
| Liu Jiakun | Central Academy of Fine Arts | Jiakun Architects |
| Jacques Herzog | ETH Zurich | Herzog & de Meuron |
| Frank Gehry | Yale University | Gehry Partners |

The Zaha Hadid and I. M. Pei cases are explicitly historical and require review; they are not fabricated current jobs. Other fixtures retain unspecified timing when present employment is not established. The complete comparison includes confidence, selected claims, field outcomes and temporal labels: [research report](corrective-research.md) and [machine-readable evidence](../benchmarks/october26-corrective-research.json). These are deterministic fixtures, not claims of improved live accuracy.

## Seed handling and export contract

A failed rich input still displays its mapped name, organisation, role, origin, education and profile link where semantically appropriate, with supplied provenance, 100% input certainty, zero independent research confidence and zero research coverage. Unmapped attributes remain in record details. Nationality, citizenship, birthplace and residence are retained separately and are never silently converted to Country of Origin. Conflicting or context-incompatible values remain available without manufacturing a combined professional or educational record.

The central contract is [app/export/schema.py](../app/export/schema.py), documented fully in [exports.md](exports.md):

- First eight columns: `full_name`, `country_of_origin`, `organisation`, `job_title`, `university_name`, `degree_type`, `subject`, `profile_link`.
- Next eight: each core field's `_confidence`, meaning independent research confidence.
- Next 48: for each core field, `_input_confidence`, `_provenance_status`, `_supplied_values`, `_source_urls`, `_review_required`, `_review_reasons`, in that order.
- Final nine: `profile_confidence`, `coverage`, `legacy_coverage`, `review_required`, `review_reasons`, `status`, `record_status`, `error_code`, **`source_urls`**.

Nothing follows `source_urls`. Administrative input and processing IDs are excluded. The old provenance query option remains accepted but no longer changes the layout. JSON supplied-value arrays retain raw input; display normalization is deterministic across API, frontend and exports. Both formats reject a standardized cell exceeding the spreadsheet limit with an actionable 422 rather than silently truncating or creating variable continuation columns; original input remains accessible in details.

XLSX tests inspect actual fills: review fields and the review indicator receive yellow, ordinary fields remain unfilled and values remain equivalent to CSV. Tests cover the unchanged 50% threshold, conflicts and meaningful non-confidence review reasons.

## Frontend and saved-file workflow

Desktop and 390×844 mobile browser checks verify compact horizontal 23px badges, long values, themed search controls and no horizontal page overflow. The loading orb is unchanged. The original hydration race is reproduced with an empty first response followed by valid records; the UI stays loading and makes two bounded result requests without a false empty message. Genuine empty results and API failures have separate tested states.

The real backend integration flow verifies **save → list from another authorized client → paginated/searchable records → field confidence and sources → unchanged original download → delete → unavailable** for both formats. Frontend browser tests independently exercise the corresponding controls, field inspection, search, pagination, return navigation, success/error handling and exactly one save/delete request. Missing/incorrect authentication and disabled-library access remain enforced. This is still a shared file collection, not a new people database.

The actual original October 26 CSV and XLSX each preview **65 rows**, with a two-row page, working name search and unchanged original hashes. See [legacy preview verification](../benchmarks/october26-legacy-preview.json). Legacy exports cannot supply field provenance they never stored; aggregate source URLs are labeled as whole-record information and are never attributed to individual fields.

Server parsing retains only the requested page and is bounded by configured byte/uncompressed-byte limits, 512 columns and 100,000 rows. The browser receives at most 100 records. Spreadsheet formulas remain inert text; external links and macros are not executed. Malformed/oversized previews return an actionable error without changing or removing the original file. See [library API and limits](library.md).

## Validation and provider work

The final local backend suite passed **954 tests**, including all **14 golden quality cases**, with **7 PostgreSQL-only skips** because `TEST_POSTGRES_URL` was unavailable. Two existing Starlette/httpx deprecation warnings remain. Ruff checks, formatting and dependency validation passed. The frontend passed **53 unit/rendering tests**, its production build and the offline real-browser workflow. The unchanged graphics bundle produces Vite's existing size warning. Supporting reports: [research](corrective-research.md), [seed/output](corrective-output.md) and the frontend repository's `docs/corrective-release.md`. Remote CI outcomes and delivery revisions are reported after pushing.

The baseline backend suite passed 894 tests with seven PostgreSQL-only skips. No golden fixture was deleted. Existing tests that asserted appended administrative columns or invisible supplied fields were migrated to the intentionally changed output contract while preserving raw data, evidence, confidence and namesake assertions.

The offline benchmark uses MockTransport only, fixture version 1, normalization version 6 and 2ms simulated latency. Counts below are identical before and after:

| People | Model requests | Searches | Extractions | Actual fetches | Database statements | Result-read queries |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4 | 1 | 2 | 2 | 173 | 6 |
| 5 | 20 | 5 | 10 | 2 | 749 | 6 |
| 25 | 100 | 25 | 50 | 2 | 3,629 | 6 |
| 100 | 800 | 100 | 600 | 2 | 25,028 | 6 |

The existing normalized result contract matches exactly after removing only the additive `researched_value` and `relationship_temporal_status` keys. Selected values, confidence, review, claims, sources and statuses remain compared. Timing observations are machine-dependent and do not establish production speed. See [workload comparison](../benchmarks/october26-corrective-workload.json).

## Delivery and limitations

Both repositories are released to their existing `origin/main`; final hashes, matching remote heads, outstanding-commit checks and CI outcomes are reported separately after pushing. A Git push is not evidence of Railway deployment. The available Railway browser session shows a login screen, so backend web/worker revision and live frontend deployment cannot be confirmed from that session. Deployment configuration is unchanged.

No new live research run was authorized. Exact original per-source eligibility and post-release live accuracy remain unverified because raw evidence was unavailable and paid calls were prohibited. The offline browser uses controlled API responses; real persistence and access controls are covered separately by backend integration tests. Existing dependency deprecation warnings and the frontend graphics-bundle warning are documented rather than hidden.
