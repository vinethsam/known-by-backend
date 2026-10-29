# Shared results library

The library holds finished CSV/XLSX exports selected by an authorized user after
review. It is a file library: no new person, claim, credential, or research-history
records are created. Completing a job, viewing results, and downloading a normal
export never save anything to the library.

## API

All routes require a Bearer token. The library is disabled with HTTP 503 if
`API_ACCESS_TOKEN` is not configured, including in development. The ordinary team
token permits reads and saves, but direct DELETE requests return 403. KnownBy has
no individual-user or ownership roles, so the conservative deletion rule uses an
optional, distinct `LIBRARY_DELETE_TOKEN` through the existing Bearer mechanism.
When unset, deletion is disabled for everyone. Equal team and privileged tokens
are rejected as invalid configuration. The privileged token can read, save, preview,
download, and delete library entries; it does not authorize research endpoints.
Missing or incorrect credentials return 401. No user identity is inferred from a
shared token. Metadata includes server-computed `can_delete`; hiding the frontend
action is additional presentation, not the authorization boundary.

| Method | Route | Result |
| --- | --- | --- |
| POST | `/v1/jobs/{job_id}/library` | Save both formats as one entry; return metadata with HTTP 201 |
| GET | `/v1/library/files?limit=50&offset=0` | Metadata, newest first; maximum page size 100 |
| GET | `/v1/library/files/{file_id}?format=csv` (or `xlsx`) | Download original saved bytes of that format |
| GET | `/v1/library/files/{file_id}/records?limit=50&offset=0&q=` | Read-only paginated record preview, maximum page size 100 |
| DELETE | `/v1/library/files/{file_id}` | Privileged deletion of the entry and both formats; HTTP 204 |

The explicit save body is:

```json
{"provenance": "none"}
```

An empty body object is also accepted. `provenance` is `none` (default) or `field`,
matching normal export options. A legacy `format=csv|xlsx` body is accepted but no
longer selects a single format: every new save retains both. Unknown fields or
invalid options return 422. There is no upload endpoint. Both artifacts are rendered
from one in-memory result snapshot before any database write. One transaction
inserts one logical entry containing both byte sequences; a rendering, quota, or
storage failure cannot report success for a partial save. Repeating an explicit save
intentionally creates another complete entry, subject to the same limits.

Jobs with `status=completed` or `status=partial` and retained results can be saved after every person is terminal. `partial` means processing completed with issues. Completed results
may contain review flags; the user's explicit request is the decision to retain the
file. Queued, running, failed, and cancelled jobs return 409. Unknown jobs or
files return 404. No provider configuration, network retrieval, or model call is needed
to save, list, download, or delete a file.

## Files and naming

The download and saved filename follow
`<List-Name>_<YYYY-MM-DD>_<HHmm>.<csv|xlsx>`. Time is the job start in UTC, with job
creation time as a stable fallback for normal exports of unstarted jobs. The first
nonempty source-list value in original row order supplies the name; absent metadata
uses `KnownBy`. Sanitization preserves safe readable Unicode, uses hyphens, strips
controls and path characters, and bounds the list portion. Internal job IDs never
appear as the filename. See [export contracts](exports.md).

Metadata includes file ID, filename, default format, saved time, research start time,
total byte size, optional originating list name, `available_formats`, and `can_delete`.
New entries list both CSV and XLSX; old entries list only their original format.
Without a format query, downloads preserve the original/default format (XLSX for
new entries). An unavailable legacy format returns 404; it is never regenerated.
The file itself contains the ordinary
standardized export, with supplied values and research provenance kept distinct. Administrative input columns are excluded. No separate
HTML, Markdown, search/model responses, claims, or confidence objects are copied into
library storage. Listing retrieves metadata without loading either binary file. Library
responses are private and disable browser/proxy caching.

Saved CSV and XLSX files have the same fixed column contract as a normal download:
`full_name`, `country_of_origin`, `organisation`, `job_title`, `university_name`,
`degree_type`, `subject`, `profile_link`, their confidence columns, then the standard
provenance, summary, review, status and error columns, ending with `source_urls`.
Nothing follows that last column. A name-only upload and an upload with extra seed
columns share the entire export layout. See [export contracts](exports.md).

## In-app record previews

The records endpoint reads the actual saved artifact and returns `{file, records, total,
limit, offset, has_more}`. Records use the canonical export keys; legacy friendly headers
are recognized. Search (`q`, maximum 200 characters) matches the eight core displayed
fields. Unknown administrative columns do not appear in previews. Missing legacy
confidence/provenance is left empty, never reconstructed as independent research.
The known legacy research prefix and optional provenance block are recognized before
appended input metadata, so an administrative `STATUS` cannot replace the saved
research `status`. When a legacy file includes explicit research confidence, it takes
precedence over its older combined confidence column. The original October 26 CSV
and XLSX were both previewed successfully: 65 saved rows each, with unchanged bytes.

CSV rows and read-only XLSX rows are iterated on the server, retaining only the requested
page. Reads are bounded by the configured file-size and uncompressed-workbook limits,
512 columns and 100,000 rows. Browser memory holds at most 100 records. Formula cells
are inert strings; macros, external links and formulas are not executed. Malformed or
oversized previews return 422 while the unchanged original remains downloadable.
Previews require the same library authentication as listing and download. Deletion
additionally requires the privileged token and confirmation in the file-list menu;
the record viewer has no deletion action.

## Storage and limits

`DatabaseResultFileStorage` implements the `ResultFileStorage` boundary. The POC uses
the existing database: PostgreSQL `BYTEA` in production, SQLite binary storage locally.
An object-storage adapter can later implement the same small save/list/get/delete
contract without changing research or the API.

| Setting | Default |
| --- | ---: |
| `LIBRARY_MAX_FILE_BYTES` | 10,000,000 |
| `LIBRARY_MAX_TOTAL_BYTES` | 250,000,000 |
| `LIBRARY_MAX_FILES` | 500 |

The file limit applies to each format; total-byte quota counts both byte sequences,
while the file-count quota counts logical entries. Exceeding any limit returns 413
without storing a partial entry. Save and delete
operations serialize quota checks in the database so simultaneous API replicas cannot
overrun the shared limits. Deleting a file releases its size/count allocation. No
duplicate temporary files are written to disk.

Migration `202609290001` creates only `saved_result_files`; additive migration
`202610280001` adds nullable `csv_content` and `csv_size_bytes` companion columns.
Existing IDs, formats, filenames and bytes remain unchanged. New entries store XLSX
in the original content column and CSV in the companion column. Run `alembic upgrade
head` before deploying this backend revision. It has no foreign key to
jobs, caches, people, or evidence. Normal job/cache cleanup therefore cannot delete an
intentionally saved export; only an explicit library delete or database administration
removes it. PostgreSQL backups must include this table. Downgrading this migration
drops the table and its saved files, so export/backup retained artifacts first.
Downgrading only the companion-column migration removes new entries' CSV copies
while retaining their original XLSX and correcting their byte totals; back up both
formats before any downgrade.
