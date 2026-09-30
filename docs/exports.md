# Result exports

`GET /v1/jobs/{job_id}/export` supports CSV and XLSX through the `format`
query parameter. The default request remains compatible with the established
export contract:

```text
GET /v1/jobs/{job_id}/export?format=csv
```

Use `provenance=field` to append one supporting-source column beside each
selected value and confidence pair:

```text
GET /v1/jobs/{job_id}/export?format=xlsx&provenance=field
```

The added columns are named `{field}_source_urls`. Each contains the unique,
semicolon-separated URLs supporting that selected field value. These URLs come
from that output record's field decision; sources supporting another education
record are not included. The existing person-wide `source_urls` column remains
available in both modes for compatibility and research diagnostics.

A result with several education records produces one export row per record.
Original input columns, the display-normalized seed name, and shared employment values repeat on
those rows, while education fields, confidence, coverage, review state, profile link,
and field provenance remain record-specific. The enriched `full_name` value always
uses the validated seed name with conservative display casing and deterministic
confidence `100`. The original input cell stays unchanged. It has no selected
web claim, field-level review, or supporting source URL; web identity remains internal
and the optional `full_name_source_urls` cell is therefore blank. Existing
single-record results and historical flat profiles still produce one row. Failed tasks
and tasks without a result also produce one row with empty enrichment values.

Source-list columns such as `LIST_NAME`, `source_list`, `source_dataset`,
`dataset_name`, `cohort`, and `source_cohort` are passive input metadata. Their original
header and value are preserved through the existing original-row projection and repeat
unchanged on every derived education row. They are not sent into research queries or
model/identity context, do not become provenance, and do not affect selected values,
confidence, coverage, or review. Rows with the same person but different list metadata
remain separate rows/results. Missing list metadata is valid, and unrecognized metadata
columns receive the same passive preservation behavior.

Both modes apply the same CSV/XLSX spreadsheet-formula and XML character
protections. XLSX also fills a populated selected-value cell and its associated
confidence cell with pale yellow (`#FFF2CC`) when confidence is strictly below the
configured `SCORING.review_threshold` (50 by default). Blank fields, confidence equal
to 50, higher-confidence fields, unrelated cells, and whole rows are not highlighted.
CSV retains the deterministic confidence and review columns but has no styling.
Profile confidence and coverage use only the six evidence-derived fields, so the
guaranteed name value does not raise either exported research metric.

The accuracy/reconciliation pass adds no export columns. Enriched values below
`SCORING.selection_threshold` (10 by default) are blank in CSV/XLSX because the field
decision is null; their raw claims, alternative IDs, provenance, confidence, and reason
codes remain available in the JSON result/evidence ledger. Seed metadata and original
input columns remain unchanged.

## Download filenames and retained files

CSV and XLSX downloads use `<List-Name>_<YYYY-MM-DD>_<HHmm>.<extension>`, with the
research/job start in UTC. Repeated downloads use the same timestamp; an unstarted job
uses its creation time. The first nonempty list value in original row order supplies
the filename only. With no list metadata the prefix is `KnownBy`. The list prefix is
sanitized to safe readable Unicode and hyphens, bounded to 100 characters and 180 UTF-8
bytes; job IDs are absent. `Content-Disposition` includes an ASCII fallback and an
encoded Unicode filename when needed.

An explicit save to the [shared results library](library.md) uses this identical
export renderer, filename, provenance mode, and highlighting threshold. Saving does
not alter research or call providers. The saved bytes remain downloadable after job
cleanup. XLSX archive packaging timestamps may differ between separate renderings;
the cells, columns, evidence, formulas protections, and styles are the same.
