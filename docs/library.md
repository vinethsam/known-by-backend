# Shared results library

The library holds finished CSV/XLSX exports selected by an authorized user after
review. It is a file library: no new person, claim, credential, or research-history
records are created. Completing a job, viewing results, and downloading a normal
export never save anything to the library.

## API

All routes require `Authorization: Bearer <API_ACCESS_TOKEN>`. The library is disabled
with HTTP 503 if the token is not configured, including in development. KnownBy has
one shared team token and no individual-user or ownership roles: everyone authorized
by that token can access and delete every library file. No user identity is invented
or inferred from the token. Missing or incorrect credentials return 401.

| Method | Route | Result |
| --- | --- | --- |
| POST | `/v1/jobs/{job_id}/library` | Save one export and return its metadata with HTTP 201 |
| GET | `/v1/library/files?limit=50&offset=0` | Metadata, newest first; maximum page size 100 |
| GET | `/v1/library/files/{file_id}` | Download the retained artifact |
| DELETE | `/v1/library/files/{file_id}` | Permanently remove that artifact; HTTP 204 |

The explicit save body is:

```json
{"format": "xlsx", "provenance": "none"}
```

`format` is `xlsx` (default) or `csv`; `provenance` is `none` (default) or `field`,
matching normal export options. Unknown fields, formats, and provenance modes return
422. There is no upload endpoint: clients cannot store arbitrary bytes, URLs, scrape
artifacts, or substitute exported rows. The backend reads an actual completed job and
generates the artifact using the same renderer and scoring threshold as its ordinary
download. A save is explicit each time; repeating it intentionally creates another
saved file, subject to the same limits.

Only jobs with `status=completed` and retained results can be saved. Completed results
may contain review flags; the user's explicit request is the decision to retain the
file. Queued, running, partial, failed, and cancelled jobs return 409. Unknown jobs or
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

Metadata includes only file ID, filename, format, saved time, research start time,
byte size, and optional originating list name. The file itself contains the ordinary
standardized export, including original per-row source-list metadata. No separate
HTML, Markdown, search/model responses, claims, or confidence objects are copied into
library storage. Listing retrieves metadata without loading the binary file. Library
responses are private and disable browser/proxy caching.

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

Exceeding any limit returns 413 without storing a partial file. Save and delete
operations serialize quota checks in the database so simultaneous API replicas cannot
overrun the shared limits. Deleting a file releases its size/count allocation. No
duplicate temporary files are written to disk.

Migration `202609290001` creates only `saved_result_files`. It has no foreign key to
jobs, caches, people, or evidence. Normal job/cache cleanup therefore cannot delete an
intentionally saved export; only an explicit library delete or database administration
removes it. PostgreSQL backups must include this table. Downgrading this migration
drops the table and its saved files, so export/backup retained artifacts first.
