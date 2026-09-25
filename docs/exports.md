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
Original input columns and shared person/employment values repeat on those rows,
while education fields, confidence, coverage, review state, profile link, and
field provenance remain record-specific. Existing single-record results and
historical flat profiles still produce one row. Failed tasks and tasks without a
result also produce one row with empty enrichment values.

Both modes apply the same CSV/XLSX spreadsheet-formula and XML character
protections. XLSX also fills a populated selected-value cell and its associated
confidence cell with pale yellow (`#FFF2CC`) when confidence is strictly below the
configured `SCORING.review_threshold` (50 by default). Blank fields, confidence equal
to 50, higher-confidence fields, unrelated cells, and whole rows are not highlighted.
CSV retains the deterministic confidence and review columns but has no styling.
