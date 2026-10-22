# Targeted research quality refinement

This pass addresses specific identity, relationship selection, and retrieval failure
patterns in the existing research pipeline. It retains seed-as-hypothesis behavior,
the 50% review threshold, the 10% selected-value floor, source policy, and existing
search, source, model, token, and timeout budgets. It adds no separate research mode,
provider, export schema, or database table.

## Evidence and its limits

The user-provided `Forbes-2000_2026-10-08_0719.xlsx` and
`US-Government-Top-500-Young-African-Leaders-Initiative_2026-10-08_0636.xlsx` are
output snapshots, not captured source bodies or decision traces. Their `source_urls`
cells show URLs associated with a result; they do not prove that each page was fetched,
extracted, or admitted as evidence. Different runs are not controlled before/after
comparisons. The representative observations below motivated the offline fixtures:

| Export row | Observed result |
| --- | --- |
| Forbes 13, 20, 111 | Alexander Karp, Allison Kirkby, and Bill Winters ended `IDENTITY_UNRESOLVED` while their URL lists included their organisations' official pages. |
| Forbes 31, 37 | The input listed Agnico Eagle Mines and QBE Insurance Group for Ammar Al-Joundi and Andrew Horton, while the reviewable selections were Barrick Gold and Beazley respectively. The URL lists alone do not establish which employer was verified as current. |
| Forbes 87, 94 | Aziz Fakhroo received a secondary Indosat board affiliation; Bassel Mohamed Ali received a British University in Egypt/PRME role from an apparent partial-name match. |
| YALI 32, 73, 90 | Fanelesibonge Mbuyazi and Hellen Munyasa had no selected fields; Isaac Lungu received a low-confidence Dalyn Mine/Accountant selection requiring review. |

The exports identify symptoms, not the exact failing stage in each live job. In
particular, an official URL in `source_urls` does not by itself show a successful
identity decision. Live cause and accuracy require per-job evidence and INFO/DEBUG
decision traces if retained, or a new production-pipeline run reviewed against current
sources. No paid provider run was made for this refinement.

## Corrected mechanisms in offline fixtures

### Official source dismissed before identity could be checked

An advisor's `unrelated` decision previously dropped a candidate before retrieval.
For candidate metadata that explicitly joins the full seed name to a seeded
institution, the candidate can now be fetched so its body can be checked. A genuine
official biography with local full-name and institution agreement can survive the
advisor disagreement, but it remains ambiguous and reviewable. For a disputed source,
at least one claim quotation must locally connect the full name to the seeded
institution before the source becomes selection-eligible; a related-search footer
alone cannot certify a partial-name claim elsewhere on the page. Each claim on an
advisor-disputed page must also link the full name to its own quoted value. This
guard applies during extraction and reconciliation of previously stored claims.
Metadata alone
never becomes final evidence or raises confidence, and an unrelated body still fails
normal identity and claim gates. This narrow exception
stays inside the existing source and model budgets, though it may fetch and extract a
source that was previously skipped. The
[identity admission fixtures](../tests/test_identity_admission.py) cover the accepted
official pattern, split title/snippet context, and rejected partial-name or
unrelated fetched bodies.

For a corroborated corrective cluster, the claim quotation normally connects the
full seed name to its institution. A page that opens with a full-name biography
heading can also support a following present-tense pronoun role quotation when two
coherent sources corroborate it and the advisor did not dismiss them. A partial name
on a mixed page cannot use that exception. Newer, independent current evidence can
still correct an obsolete seed affiliation; the seed remains a hypothesis until
external evidence supports it.

### Historical or secondary relationship selected as current employment

Relationship classification now recognizes the observed board titles
“Administrateur” and “Deputy President Commissioner”, along with common English
variants such as “President of the Board” and “Independent Director”, before
treating a generic executive title as primary employment. A recent, grounded primary
employer pair can outrank an old page that still says an executive role is current
when the newer pair is explicitly current or supported by a present-tense role quote
from an institutional source. A recent article using past tense for a former CEO
does not gain this current-role tier. Board and advisory affiliations do not gain
it either. Organisation and title continue to come from the same grounded
relationship. Historical and secondary claims remain alternatives for review.
[Relationship fixtures](../tests/test_relationship_selection_regressions.py)
exercise newer primary employment, board-vs-executive choice, and historical roles.

### Static or rendered placeholder mistaken for a biography

The static path treats a short explicit loading placeholder, including a brief
“we are gathering details” continuation, as incomplete and tries the existing
Playwright fallback. When Playwright reaches network idle before a biography is
hydrated, it rechecks only the unusable DOM within the existing
five-second maximum settle window, subject to browser timeout, byte, redirect, and
network guards. A substantive static biography remains static; a normal sentence
that merely contains “loading” is not a placeholder. Failed rendering remains a
retrieval failure and cannot create evidence. [Retrieval fixtures](../tests/test_retrieval.py)
cover the delayed-content and control cases.

## Validation boundary

The same 77 focused offline cases ran against an isolated archive of the pre-change
`main` revision and the modified tree:

| Fixture measure | Before | After |
| --- | ---: | ---: |
| All focused assertions passing | 67/77 | 77/77 |
| Official candidate/advisor admission assertions | 0/4 | 4/4 |
| Partial-name namesake exclusion | 0/1; wrong organisation selected at 82.67% | 1/1; organisation null |
| Current primary-role choices | 0/3 | 3/3 |
| Placeholder and delayed-DOM retrieval | 0/2 | 2/2 |

All 67 cases that passed before still pass. Three added controls guard against
regressions encountered during implementation: a disputed page with a seed anchor
only in its footer does not admit a partial namesake claim; two coherent biographies
with a full-name heading and following pronoun role remain eligible; and a recent
article about a former CEO does not displace explicitly current official employment.
These are fixture outcomes, not measured field coverage or precision on the Forbes
and YALI populations.

A further mixed-page fixture reproduced a remaining source-level leak: one valid
full-name claim admitted a second claim about a partial namesake. The claim-level
guard rejects the latter during extraction and selection, including for older
stored claims. A companion fixture rejects a single quote that mentions both the
full name and a partial namesake in separate clauses. These additional fixtures
pass in the final suite; they were not part of the 77-case controlled comparison
above.

The golden plus affected pipeline suite passed **114** tests. The complete offline
suite passed **755**, with **6** PostgreSQL-dependent tests skipped because
`TEST_POSTGRES_URL` was absent and **2** warnings. No live provider call was made.

The [offline production-pipeline benchmark](../benchmarks/README.md#targeted-quality-refinement-measurement)
replayed identical static fixtures against the prior and modified revisions at 1,
5, and 25 people. Normalized output hashes and model, search, extraction, fetch,
database, and result-read counts matched at every size. The fixtures did not trigger
browser fallback or disputed-advisor admission, so equal work counts do not bound
cost on those newly corrected paths. Single local elapsed times were mixed; no
general speed claim follows.

The archived code lacks two newly introduced test interfaces. Only the comparison
harness adapted those calls: it omitted `source_type` when invoking archived
`assess_identity`, and treated missing candidate-context admission as the original
advisor rejection. The archived production code was unchanged.

The Isaac Lungu export symptom is not counted as repaired. In a separate synthetic
name-only probe, a weak employment pair remains selectable with review at
44.41%; adding a conflicting independent substantive source makes the role null.
Without the live claim ledger and source bodies, that export cannot establish which
of these mechanisms applied. Hellen Munyasa and Fanelesibonge Mbuyazi also require
their live decision traces to establish why the exported fields were empty; the
Hellen-style corrective fixture only establishes that the present fallback still
accepts locally grounded, corroborated full-name evidence.

The targeted tests use synthetic source and claim records named after observed
cases; they do not assert those real people's biographies. The existing golden panel
and complete offline suite are necessary regression checks, but passing them does
not measure live Forbes/YALI accuracy, provider cost, or browser success on Railway.
The next production review should inspect source bodies, grounded claims, final
identity eligibility, selected and alternative role pairs, confidence, and review
reasons for a small panel through the normal API and settings. Compare the same
people and evidence windows; count correct evidence-derived fields separately from
the immutable seed name, and count false-person selections separately from missing
fields.
