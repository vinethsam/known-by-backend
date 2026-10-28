# October 2026 seed and output correction

The confirmed output defects were projection and serialization problems. Rich seed attributes were retained in raw metadata but absent from unselected core cells. Export layout varied with input columns and the provenance option. Raw administrative columns appeared after the source list. Nationality/birthplace aliases also blurred the distinction between geographical context and origin.

The correction projects mapped supplied values after independent research scoring. All eight fields support this path, including `ALUMNI_*` headers, custom mappings, single-person input, and failed tasks without a result profile. A common stored profile adapter applies the same semantics to API reads and exports. Selection stopping rules, confidence, and coverage exclude supplied-only values. Exact input text remains in stored provenance.

For example, a failed row containing `ALUMNI_ORGANISATION=6a architects`, `ALUMNI_JOB_TITLE_EXACT=Architect`, and a supplied profile URL now displays `6a Architects`, `Architect`, and the URL. Each is identified as supplied with 100% input certainty, zero independent research confidence, no selected evidence IDs, and zero research coverage. No current employment is inferred from the title. An incompatible researched employer or credential prevents a supplied fragment from being attached to that relationship; the fragment remains visible in details.

The export registry defines exactly 73 columns, identically for CSV and XLSX. The first eight are the requested core values and the last is `source_urls`. Research confidence, supplied certainty, source support, field review, person status, and record status have explicit columns. No original administrative fields or arbitrary JSON metadata columns follow the schema. See [the column groups and semantics](exports.md).

The shared normalizer provides display casing in backend presentation paths. Supplied-input projection sanitizes display controls without changing raw values. Source policy also zeros export confidence when a legacy disallowed profile URL is removed, avoiding an empty field with a misleading 99% score.

The projection also respects source-policy exclusions for supplied social profile URLs. Their exact raw values remain accessible with 100% input certainty in supplied details, while the core field stays empty with zero research confidence. This keeps API, frontend, and both export formats aligned without admitting excluded research sources.

Workbook acceptance tests inspect yellow fills on actual reviewed cells and the summary review indicator. They verify ordinary populated cells and empty cells remain uncolored and compare both formats' values. Supplied conflicts and superseded input propagate explicit field and record review reasons after research scoring; no score is adjusted to make a result pass.

Offline validation covers all eight rich input mappings, failure before a profile exists, geography distinctions, raw control-character retention, acronym casing, conflict/supersession, pairing guards, fixed layout, both formats, workbook fills, legacy stored profiles, source filtering, and explicit oversized-cell errors. Database compatibility checks continue to compare every stored research property and every evidence/usage ledger; only additive display provenance is excluded from the old snapshot comparison. The result read remains six SQL queries for both one and eight people. Full-release test counts and provider-work comparisons are in the integrated release report.

This is offline validation of implementation behavior. It does not claim new live research coverage or a live deployment. No provider request was made for this work; configured models and provider budgets remain unchanged.
