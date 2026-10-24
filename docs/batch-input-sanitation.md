# Batch input control-character fix

## Reproduced failure

Before editing code, `parse_batch` at `5eb04b7` reproduced
`BatchParseError: Input text contains unsupported control characters` on the
original `KnownBy_PDL_Baseline_50.csv` (50 people, 25 columns).

The failing path was `parse_batch` → `_read_delimited` → `_stringify_cell` →
`_validate_text` → `validate_input_text`. CSV decoding and parsing succeeded;
the per-cell validator rejected the input before seed normalization, database
serialization, job creation, or any external provider call.

CSV row 42 contained U+0081 in `ALUMNI_FULL_NAME` and `ALUMNI_FIRST_NAME`.
Each was preceded by U+00C3: UTF-8 Á misdecoded as Latin-1. The existing narrow
encoding repair correctly recovers U+00C1, but ingestion rejected the C1 control
before that repair could run. Removing U+0081 first would lose the accent.

Verified source SHA-256:
`e5bbef428cef5d085e0c53827d28b2c4748dc275cb57267ecf824378d04e05ea`.
No real person values or dataset content are included in the test fixtures.

## Boundary behavior

`sanitize_input_text` in `app/input_validation.py` applies the existing encoding
repair first. It removes remaining non-layout C0/C1 controls (including NUL and
DEL), converts vertical tab, form feed and next-line controls to line breaks in
multiline mode, and uses spaces for layout controls in single-line processing.
It preserves ordinary spaces, accents, multilingual characters, and Unicode
format characters such as joiners. Unpaired surrogates and unsupported Unicode
noncharacters remain validation errors rather than guessed biographical text.

Original cells and labels remain in JSON-backed rows and seed metadata, where
control characters are escaped during serialization. Processing values, raw
metadata used as context, header matching, supplied/evidence comparisons and
derived Shared Library labels all use the centralized sanitation. Provenance
retains the original supplied strings and 100% input certainty. Optional JSON
provenance exports preserve original controls in both CSV and XLSX; ordinary
XLSX cells retain the existing visible replacement for XML-invalid characters.

Unusable individual names or seed records now become failed tasks with
`INVALID_PERSON_NAME` or `INVALID_PERSON_SEED`, keeping their original row positions
and metadata. They are never leased for research. Valid neighbours continue;
the existing job lifecycle reports a partial result when appropriate. Invalid
file structure, unsafe XLSX archives, ambiguous/missing headers and configured
upload/size bounds still reject the upload. No database migration is required.

Models, budgets, search planning, identity thresholds, evidence confidence,
eight-field ordering and the frontend workflow are unchanged.

## Offline verification

The original file now produces 50 valid seeds and zero row errors. All original
rows and seed metadata compare exactly to the decoded input, all 50 seeds survive
JSON reload, and approved processing context contains no control characters.
The source file hash is unchanged. This verification invokes ingestion only,
with zero model/search/fetch calls.
The same file also returned HTTP 202 through the real upload route backed by an
isolated local SQLite database: 50 queued records, zero failures, and exact original
rows in the JSON response. Outbound HTTP was blocked and no worker was started.

Synthetic regressions cover the exact misencoded-accent sequence in CSV and XLSX,
NUL/VT/FF/C1 controls, multilingual and multiline text, header collisions after
sanitation, raw metadata context, corroboration without confidence inflation,
CSV/XLSX provenance, mixed and entirely invalid records, API responses, task
claiming, JSON persistence and Shared Library labels. PostgreSQL persistence
coverage runs when `TEST_POSTGRES_URL` is configured; otherwise it is skipped.
Final validation: 894 backend tests passed, seven PostgreSQL tests skipped,
39 frontend tests passed, and backend lint, formatting and dependency checks passed.
