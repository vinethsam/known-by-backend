# Rich seed context

KnownBy retains supplied CSV/XLSX cells separately from independently researched
facts. The existing discovery, validation, extraction, retrieval, reconciliation,
and export pipeline remains in use; models, concurrency, budgets, and the 50% review
threshold are unchanged.

## Ingestion and privacy

`app/seed_context.py` shares header interpretation between batch ingestion and
research. Space, punctuation, snake-case, camelCase, and common collapsed spellings
are normalized for matching only. Examples include:

| Meaning | Example headers |
| --- | --- |
| Name | Full Name, full_name, candidateName, person name |
| Employer | organisation, organization, company name, job company name, current employer |
| Historical employer | previous company, former employer, historical organisation |
| Role | job title, current role, designation, position |
| Historical role | previous job title, former role, past position |
| Education institution | university name, college name, alma mater, educational institution, education school name |
| Qualification | degree, qualification, degree type, academic degree |
| Subject | subject, major, field of study, education major |
| Professional context | industry, sector, professional field, specialization |
| Geographic context | country, current country, city, region, location |
| Origin hypothesis | country of origin, birth country, country of birth |
| Other context | graduation year, program year, government office, alumni organisation |

`PersonSeed.input_metadata` preserves original column labels and text, including
whitespace. `input_field_map` records recognized meanings. Unknown columns are
metadata, not inferred facts. Administrative labels (lists, batches, identifiers,
source datasets, status/score columns) and private/contact attributes are preserved
but excluded from research payloads. Contact-like text is also filtered when placed
under a normally useful heading. Summary logs use the same approved context filter.

Optional cells too long or unsafe for a typed research clue remain intact as input
metadata. Public context is limited to 20 distinct semantic values and 2,200 value
characters; each value is at most 200 characters. Existing upload/row/column limits
still apply. Equal-strength optional aliases remain separate hypotheses instead of
arbitrarily overwriting one another. Nested JSON biographies and unknown free-text
columns are retained, not automatically interpreted as new schema fields.

## Discovery and identity

The first query targets one relationship using at most two contextual values.
Education and professional searches stay distinct. Missing education can trigger a
name/university/qualification or subject query; former employers can direct career
research. Identity-eligible selected evidence can supply a newer anchor. Contextual
queries run before optional model planning and share the existing query, tool,
source, retry, cache, and stopping budgets. Equivalent queries and sources remain
deduplicated. Healthy results do not trigger additional mandatory model calls.

Source processing retains relevant educational/professional passages. Candidate
validation and extraction receive only approved context and explicitly treat it as
unverified hypotheses. A full name associated with a supplied university can anchor
an educational source without repeating the employer. Multiple supplied schools or
former employers can anchor their respective histories. Generic subjects, industries,
locations, administrative values, and partial names do not bypass institution-based
namesake protection. Explicit educational denials are contradictions, not matches.
Existing one-hop cross-source corroboration and current/historical role selection
remain in force.

## Certainty, provenance, and output

Every raw supplied attribute is represented in `profile.seed_attributes` with 100%
`input_confidence`. This states what the user supplied, not factual correctness.
Field decisions add `supplied_values`, `input_confidence`, `research_confidence`, and
`provenance_status`: supplied, researched, corroborated, conflicting, or superseded.
Accepted external claims retain their evidence IDs and source URLs when they support
or contradict input. A former employer can be corroborated historically while a
newer independently supported role supersedes it.

The immutable name retains its existing 100% input-certainty value. The other seven
research cells remain evidence-derived: seed-only values appear as supplied input,
not as verified output. They do not increase coverage, research confidence, or
independent source counts. Different legitimate qualifications stay in separate
education records; seed components never fill gaps in another credential. A different
university is not automatically classified as conflicting education.

The eight-field order, confidence columns, status/error columns, and default export
prefix remain unchanged. Original input columns remain on every output record.
Optional field-provenance exports append supplied values, both confidence kinds,
status, and `seed_input_metadata`. JSON values exceeding a safe Excel cell size use
`__part_2`, `__part_3`, etc. columns in both CSV and XLSX; concatenate parts in order
to restore the JSON. Existing stored results and Shared Library files remain valid.
The frontend keeps the upload workflow and adds provenance labels and supplied-input
details inside the existing field drawer.

## Offline validation

All provider responses were mocked; no paid/live research was performed. Focused
tests cover name-only, employer-only, university-only, role-only, university/subject,
mixed professional/educational records, unfamiliar/admin/private fields, stale roles,
conflicting origins, educational denials, multiple schools, distinct credentials,
raw value preservation, and CSV/XLSX round trips. The existing golden cases pass.

The production offline harness compared an isolated archive of `5379a79` with the
implementation using identical fixtures and 2 ms mock latency:

| People | Searches, both | Model calls, both | Physical fetches, both | Extractions, both | Before ms | After ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 4 | 2 | 2 | 438.82 | 466.49 |
| 5 | 5 | 20 | 2 | 10 | 1549.60 | 1824.74 |
| 25 | 25 | 100 | 2 | 50 | 7734.34 | 8436.86 |

SQL work, six result-read queries, and cache reuse also match; Playwright calls remain
zero. Research values, claims, evidence confidence, review flags, and coverage match
after excluding only additive seed-provenance metadata. Full JSON hashes intentionally
change. Timings are single local observations, slower in this run; no speedup is claimed.
Mocked token accounting does not measure real provider prompt charges.

A separate recovery fixture only exposes its education source to a university-targeted
query. Rich context recovers the university, degree, and subject with no planning call
and no more searches than the sparse seed. Complete fixtures produce the same values
and provider-work counts with rich and sparse seeds. These are deterministic offline
results, not an accuracy claim about live unmatched records.

Compact benchmark results are in `benchmarks/rich-seed-context.json`. Local PostgreSQL
integration cases require `TEST_POSTGRES_URL` and are skipped when it is absent.
Final validation passed 867 backend tests (six PostgreSQL cases skipped), 39 frontend
tests, backend lint/format/dependency checks, and the frontend production build.
Raw preferred URLs survive stored-model reloads, and large provenance JSON round trips
through both spreadsheet formats even when a fragment would otherwise start with a
formula character.
