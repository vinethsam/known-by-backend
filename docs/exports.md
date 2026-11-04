# Research display and downloads

The standard frontend shows exactly seven fields: Full Name, Organisation, Job Title,
University Name, Degree Type, Subject and Profile Link. Confidence, review metadata and
field evidence remain in the detailed viewer.

Every CSV/XLSX download, including History and Shared Library/Database, uses exactly
these fourteen columns in this order:

1. Full Name
2. Organisation
3. Job Title
4. University Name
5. Degree Type
6. Subject
7. Profile Link
8. Full Name Confidence
9. Organisation Confidence
10. Job Title Confidence
11. University Confidence
12. Degree Confidence
13. Subject Confidence
14. Source URLs

Both formats share the same mapping and independent evidence confidence on a 0–100
scale. Supplied input certainty is never exported as verified research confidence.
There is no Profile Link Confidence column. Source URLs contains deduplicated evidence
links; field-level provenance remains internal. The legacy provenance request option
is accepted without changing the download schema.

The 500-person input limit and 500-row export cap remain. Multiple credentials keep
separate rows when they fit; otherwise existing numbered university, degree and
subject entries preserve their association within one person row. Individual scores
and evidence remain in detailed records. No credential deduplication rules changed.

Country of Origin is no longer extracted, grounded, normalized, scored or reconciled.
Origin and nationality input columns remain accepted and preserved as optional
identity context. Historical origin evidence remains decodable and stored.

Library saves retain the detailed internal representation needed by the existing
record viewer. Standard downloads project that representation, or supported older
CSV/XLSX artifacts, into the fourteen-column contract without rewriting stored files.
Save, paging and authorized Delete retain their existing access controls. No database
migration is required. Unreadable artifacts return an explicit error and remain stored.

Formula protection, XLSX review highlighting and the shared 32,767-character cell
limit remain. Oversize output fails explicitly without truncating the underlying data.
Coverage excludes the supplied name and now covers the six active researched fields;
historical coverage values are retained when loading saved records.

## Download filenames

Downloads use `<List-Name>_<YYYY-MM-DD>_<HHmm>.<extension>`, with the research/job start in UTC. Repeated downloads use the same timestamp; an unstarted job uses its creation time. The first nonempty list value in original row order supplies the filename only. With no list metadata the prefix is `KnownBy`. The list prefix is sanitized to safe readable Unicode and hyphens, bounded to 100 characters and 180 UTF-8 bytes; job IDs are absent. `Content-Disposition` includes an ASCII fallback and an encoded Unicode filename when needed.

Saving does not rerun research or call providers. Saved research remains downloadable after job cleanup. XLSX archive timestamps can differ between separate renderings; the columns, values, and cell styles remain equivalent.
