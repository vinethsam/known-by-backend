# Result exports

`GET /v1/jobs/{job_id}/export?format=csv` and `format=xlsx` use the same centrally defined schema and renderer as history downloads and Shared Library saves. CSV and XLSX have equivalent values and provenance semantics. The existing `provenance=none|field` parameter remains accepted for compatibility; it no longer changes the columns. No input-dependent columns are appended.

## Fixed 73-column contract

The first eight columns always appear in this order:

```text
full_name, country_of_origin, organisation, job_title, university_name,
degree_type, subject, profile_link
```

They are followed by these registered groups in `app/export/schema.py`:

| Group | Count | Meaning |
| --- | ---: | --- |
| `{field}_confidence` for the eight fields in order | 8 | Independent research confidence, never supplied certainty |
| Six provenance columns for each field in order | 48 | `input_confidence`, `provenance_status`, `supplied_values`, `source_urls`, `review_required`, `review_reasons` |
| Summary | 9 | `profile_confidence`, `coverage`, `legacy_coverage`, `review_required`, `review_reasons`, `status`, `record_status`, `error_code`, `source_urls` |

The person-wide `source_urls` column is always last. Nothing follows it. `LIST_NAME`, input headers, identifiers, staging fields, and raw input metadata remain in storage and the API rather than appearing as extra export columns. Raw mapped values also remain in the standardized `{field}_supplied_values` JSON cells. Supplied certainty is 100 even when research fails; independent research confidence is zero when no finding was selected.

Field URL columns contain only sources supporting that field in that record. Ordinary credential rows never borrow support from another education record. An explicitly combined education field lists the union of its components' sources, rather than attributing every source to every numbered qualification. The final person-wide source list is a diagnostic list capped at ten allowed retrieved URLs; it does not claim that every listed source supports every field.

Supplied profile URLs excluded by the existing social-source policy remain in raw supplied details with 100% input certainty. They are not projected into the core Profile Link or supporting-source columns and have zero research confidence, consistently in API results and both export formats.

## Values, records, and review

An upload accepts at most **500 data records**, excluding its header. CSV/XLSX exports separately allow at most **500 data rows**. Education records normally produce separate rows. If expansion would exceed 500, the entire export switches predictably to one row per person. No accepted person or qualification is dropped: education cells contain numbered lines, with matching numbers across university, degree and subject (`—` means that component is unknown). Different non-education values are retained with the same numbering when necessary.

Combined rows carry `EDUCATION_RECORDS_COMBINED_FOR_EXPORT_LIMIT` and require review. Each combined field uses the lowest confidence among its populated components, the union of supporting sources and supplied values, and `mixed` provenance when components differ. Summary confidence and coverage use the lowest component values. This trades separate credential rows and per-credential export scores for the 500-row cap; individual credential decisions, confidence and evidence remain intact in persisted research and its API/History details. The saved-file viewer preserves numbered lines so all qualifications remain readable. CSV and XLSX use the same representation. The existing spreadsheet cell-size limit still fails explicitly rather than truncating an unusually large combined value.

Person-level `status` repeats consistently across ordinary education rows. `record_status` and record review columns describe the individual education record, or the explicitly reviewed combined representation. A completed job can contain failed people; the frontend describes a terminal partial job as completed with issues rather than interrupted. Legacy jobs with more than 500 people receive an explicit export error; their persisted results remain accessible.

Mapped supplied values fill otherwise unselected core cells as `supplied`, including failed people. Their research confidence and coverage contribution remain zero. Supplied employment and education fragments cannot be attached to an incompatible researched relationship; their original values remain accessible in provenance. Explicit historical employment remains in supplied details rather than being projected as current work. A selected research value remains distinct through `researched_value` in the API, alongside the displayed value and original inputs.

Input disagreement is marked `conflicting` or `superseded`. Field review flags and record review reasons expose those conditions without changing independent research scores. A different legitimate university or qualification does not automatically mean the input is false.

Country of Origin accepts an explicit mapped origin value as supplied information. Independent origin research still requires source text establishing where a person is from. Nationality, citizenship, birthplace alone, residence, employer country, and university location are not silently converted to origin; their supplied values remain in record details.

The shared backend display normalizer applies professional casing to selected human-readable values in API results, CSV, XLSX, and saved-record previews. It preserves supported brands and acronyms including IBM, UNESCO, ICCROM, McGill, PwC, ETH Zürich, and RPBW. URLs, error codes, identifiers, raw input, and raw evidence are not title-cased. Control characters are sanitized in displayed input values; exact originals remain in provenance.

XLSX uses pale yellow (`#FFF2CC`) for populated reviewed fields and associated research-confidence cells, including conflicts and independent confidence strictly below the unchanged 50% review threshold. Supplied-only zero research confidence does not imply a low-confidence researched finding. Empty ordinary cells are not indiscriminately highlighted. A true `review_required` summary indicator receives the same yellow fill and bold text. CSV preserves equivalent textual review flags and reasons. Tests inspect actual workbook fills and compare numeric CSV/XLSX values semantically.

`coverage` uses the seven research fields excluding the supplied name; `legacy_coverage` preserves the original six-field comparison excluding origin. Neither counts unverified supplied values. Raw evidence rejected below the existing selection threshold remains in the evidence ledger.

## File safety and compatibility

Both formats retain formula-injection protection. XLSX additionally replaces characters XML cannot represent. If any standardized cell, after safety escaping, would exceed Excel's 32,767-character limit, both formats fail explicitly rather than silently truncating data or introducing variable overflow columns. The API returns 422; original values remain available in record details.

Already saved library files retain their original bytes and downloads. The preview adapter interprets supported legacy headers without rewriting files. New library saves use the current fixed schema and review treatment. See [Shared results library](library.md) for paging, access controls, and preview limits.

## Download filenames

Downloads use `<List-Name>_<YYYY-MM-DD>_<HHmm>.<extension>`, with the research/job start in UTC. Repeated downloads use the same timestamp; an unstarted job uses its creation time. The first nonempty list value in original row order supplies the filename only. With no list metadata the prefix is `KnownBy`. The list prefix is sanitized to safe readable Unicode and hyphens, bounded to 100 characters and 180 UTF-8 bytes; job IDs are absent. `Content-Disposition` includes an ASCII fallback and an encoded Unicode filename when needed.

Saving does not rerun research or call providers. Saved bytes remain downloadable after job cleanup. XLSX archive timestamps can differ between separate renderings; the columns, values, provenance, and cell styles remain equivalent.
