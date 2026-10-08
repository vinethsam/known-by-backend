# Quality regression recovery

This pass compares how the same grounded evidence survives identity checks,
reconciliation, confidence, field selection, and export. It adds no search budget,
model call, provider change, or database migration. The default review threshold
remains **50%**.

The golden fixtures are synthetic, deterministic source and claim records using the
requested names as regression labels. They are **not** archived live web evidence or
assertions about those people's actual biographies. Fixture success demonstrates
the specified selection behavior; a small live panel is still needed to verify
retrieval and extraction against today's pages.

## Historical comparison and root causes

The frozen 14-case golden panel was replayed in isolated `git archive` snapshots,
using each revision's own application code and the same local Python environment.
These are outcome assertions, not a count of whichever historical unit tests existed.

| Revision | Passed | Failed |
| --- | ---: | ---: |
| `9c4223b1fb1d6ab7264bc0a5edc095597ba37cee` | 1 | 13 |
| `ece3142f2163d7dc411d664bb587fa3c5999027f` | 1 | 13 |
| `13086832b869d82b37a7da39ed0b3daacb9b3c83` | 2 | 12 |
| `359202acfd51211746d2a59b93af24dff9a02871` | 2 | 12 |
| Recovery implementation | **14** | **0** |

This does not establish that every older revision was better. Several deficiencies
predate the latest commit, while newer fixes such as credential deduplication are
valuable. Specific functions were repaired rather than reverting whole commits:

- `ece3142` changed `_employment_decisions` from current-role priority to
  freshness-first ordering. In the Anutin fixture, Prime Minister at 82.97% in
  `9c4223b` was displaced by Party Leader in later revisions. Primary current
  relationships now outrank secondary roles with slightly newer pages.
- The same commit added a multiplicative time factor in `claim_strength` while
  retaining additive recency loss. It also treated an old official biography's
  publication date as the date of its explicit current role. In the Muizzu and
  Asadov fixtures, role confidence fell from 60.57% to 25.94%. The recovery scores
  these seeded, externally corroborated current relationships at **79.75%**.
- `_representative_link` compared composite claim strength with the identity
  threshold. Removing seed-name web provenance in `1308683` exposed this older
  admission defect for otherwise useful partial profiles. The fix checks actual
  identity separately from the selected-value floor and keeps seed-name provenance
  empty. The Hlabathe fixture now retains its contributing professional profile.
- `1308683` made all degree-free education bundles orphan-only. The targeted
  recovery below restores strong partial institution evidence without restoring
  duplicate credentials.
- Generic institution selection, administrative-title substring classification,
  late PM aliases, and disconnected name-only evidence were additional weaknesses
  visible in earlier revisions. Their fixes do not rely on names or country lists.

## Recovered selection behavior

Current employment stays one organisation/title pair. Ranking prioritizes explicit
current evidence, relationship priority, primary public office, and supported
institution specificity before finer freshness and score ties. Historical claims
cannot populate current employment. A final sanity check can replace a secondary
winner only with an existing, strong, identity-secure current primary relationship.
Comparable genuine current-role conflicts still require review.

A missing extractor group label no longer creates a spurious unpaired-title penalty
when the exact same grounded quote identifies one organisation and one title for
the person. Different quotes, incompatible dates, and multi-role quotes do not pair.
Specific offices beat generic Government when supported; country names, residences,
and parties do not become an invented government institution.

`evidence-v3` applies age uncertainty once. Explicit fact dates and dated news or
archives still decay. Only recognizable official biographies/profiles/leadership
pages with explicit current claims can avoid aging a role by the page's creation
date. Authority, directness, identity, pairing, and material conflict checks remain.
Neither the **50% review threshold** nor the **10% selection floor** changed.

The final normalization gate covers new reconciliation, stored JSON results, and
CSV/XLSX exports, including every record. PM casing variants become Prime Minister;
raw seed cells, evidence, scores, review flags, provenance, and export columns remain
intact. A credible contributing source can provide a reviewed Profile Link even
when its selected field is below 50%; no link is fabricated from seed input.

Name-only research still works, but multiple provisional same-name sources must
have compatible explicit institution evidence to combine. Disconnected credible
namesakes remain alternatives instead of being merged or choosing the largest
group. Seed-aware one-hop identity checks and Andrew's Entergy discrimination remain.

## Golden outcome matrix

All values below describe synthetic fixtures, not verified live biographies.

| Fixture | Recovery assertion | Result |
| --- | --- | --- |
| Andrew Marsh | Entergy selected at 77.49%; football namesake excluded; seed alone supplies no facts | Pass |
| Anutin Charnvirakul | Prime Minister / Office of the Prime Minister at 97.25%; historical analyst and party role remain alternatives | Pass |
| Jafar Hassan (five title variants) | Prime Minister / Prime Minister's Office at 79.75%; no PM casing escape or missing paired institution | All 5 pass |
| Tshering Tobgay | Specific Prime Minister's Office at 97.25% replaces generic Government | Pass |
| Petteri Orpo | Prime Minister at 97.25% beats Head of the Prime Minister's Office | Pass |
| Mohamed Muizzu | President / President's Office at 79.75%, clearing review naturally | Pass |
| Ali Asadov | Prime Minister / Cabinet of Ministers at 79.75%, clearing review naturally | Pass |
| Elizabeth Adams | Disconnected Alpha Corp and Beta University namesakes are not assembled into one profile | Pass |
| Isaac Lungu | One doctoral credential and one Master's credential; overlapping doctorate descriptions do not duplicate rows | Pass |
| Hlabathe Posholi | Contributing professional Profile Link retained; incomplete role remains honestly reviewable at 44.01% | Pass |

The Jafar, Muizzu, and Asadov fixtures exercise deterministic seed-context identity
scoring (0.82), while some other fixtures supply already secure identity to isolate
downstream selection. Separate identity tests cover provisional name-only inputs.

## Verified historical regression: useful partial education

Commit `1308683` changed `_education_clusters` to send every degree-free bundle to an
orphan path. A university could only survive by overlapping exactly one already
formed degree credential. This prevented fake credential rows, but also erased an
explicit university from an authoritative, identity-secure biography when the
degree was unknown. It discarded separate university/subject claims even when they
quoted the exact same credential as an accepted degree claim but lacked a shared
`fact_group`.

The historical reconciliation functions were replayed with identical university-only
input against the current contracts and scoring helpers. This isolates the grouping
change rather than recreating each complete historical runtime:

| Reconciliation revision | Selected university |
| --- | --- |
| `9c4223b` | `University of Example` |
| `ece3142` | `University of Example` |
| `1308683` | null |
| `359202a` | null |
| Recovery implementation | `University of Example`; degree remains null |

The new education quality suite initially produced **2 failures and 10 passes** on
the pre-change behavior: the missing reliable university and missing same-quote
components failed. All 12 cases pass after the recovery, alongside the existing
credential safeguards.

The repair retains a university-only partial record only when its claim is explicit,
its source is publication authority or better, its identity clears the existing
identity review threshold, and its evidence strength clears the unchanged review
threshold. It does not infer a degree or subject. Partial components can join exactly
one compatible credential through overlapping facts or identical source-local
quoted context that contains the components. A shared page or section alone is not
a relationship. A fragment fitting several credentials remains an internal
alternative rather than being assigned by sort order.

Same-level degrees at the same institution do not split just because two weak
subject descriptions differ. Distinct levels, qualification labels, dates, or the
existing supported institution distinctions still establish separate credentials.
The Isaac Lungu fixture retains one doctoral record and one Master's record despite
overlapping descriptions. Wrong-person sources cannot supply partial education.

## Internal decision logs

`LOG_LEVEL=INFO` provides a compact per-person result summary. `LOG_LEVEL=DEBUG`
also reports deterministic candidate comparisons and selection reasons, including
education merges, splits, retained partial fields, and orphan alternatives.
Decision details are embedded in the actual log message as compact JSON as well as
structured metadata, so a plaintext Railway log export retains useful context.
Logs exclude source bodies, prompts, full provider responses, and exported
diagnostic columns. Selected values and limited identity context are internal
operational data and remain subject to the deployment's log access controls.

INFO includes seed name/context, candidate/retrieval acceptance and rejection
counts, final selection-eligible/rejected counts, claim count, selected role pair
and confidence, credential count, Profile Link, review outcome/reasons, and stop
reason. Retrieval acceptance and final identity eligibility are separate counters.
DEBUG includes role winner/runner-up, currentness, institution specificity,
authority/directness/identity/age components, field alternatives, identity signals
and exclusion reasons, credential merge/split/orphan decisions, and link candidates.
Job/person correlation IDs survive plaintext export. Strings, collection lengths,
nesting, and total payload are bounded; URL credentials, query strings, fragments,
and sensitive keys are removed. Alembic logging configuration also preserves
existing application loggers so migration commands cannot silently disable tracing.

## Job outcomes and persistence

Status/result responses now include `status_label`. The existing terminal code
`partial` is displayed as **Completed with issues**, with completed profiles and
accurate task counts retained. This is an additive response field, not a database
status migration or CSV/XLSX column. No table or migration was added; the existing
head remains `202609290001`. Shared Library retention and authorization are unchanged.

The later [partial-failure salvage pass](partial-failure-salvage.md) adds terminal
attempted/successful/unsuccessful counts, treats zero-coverage people as unsuccessful,
and keeps usable incomplete profiles successful. It does not change the compatibility
job code or this historical recovery result.

## Validation and performance

On 2026-10-01 the full offline suite passed **730 tests**, with six PostgreSQL runtime
tests skipped because `TEST_POSTGRES_URL` was unset and two upstream deprecation
warnings. All 14 golden cases and the focused education, identity, finalization,
decision-trace, and job-outcome checks pass. Ruff, formatting, compilation/import,
and migration validation are recorded in [validation.md](validation.md).

The production offline harness used its standard 1/5/25/100-person fixtures and
2 ms mocked HTTP latency against an isolated `359202a` archive. Before/after times
were 0.428/0.359, 1.529/1.619, 7.416/9.212, and 55.161/61.779 seconds. These are
single local runs; the larger
after-runs were slower, so no speedup is claimed. Search, model, extraction, physical
fetch, SQL, cache, and six-query result-read work counts are unchanged. No browser
render, paid provider call, or increased budget was introduced. Output hashes
intentionally differ because scoring/selection changed. See the
[benchmark record](../benchmarks/quality-recovery.json) and
[benchmark explanation](../benchmarks/README.md#regression-recovery-measurement).

## Smallest useful next live panel

Use the normal production API/settings for Andrew Marsh with Entergy context;
Anutin Charnvirakul, Jafar Hassan, and Tshering Tobgay for current-role, confidence,
canonical title, and specific-institution checks; Elizabeth Adams without context
for abstention between namesakes; and Isaac Lungu for credential grouping. Review
actual retrieved evidence and INFO/DEBUG decisions rather than treating fixture
biographies as live truth. This six-person panel was **not run** in this offline pass.

## Validation references

- `tests/test_education_quality.py`: reliable partial education, safe same-quote
  attachment, rejected weak/ambiguous fragments, namesake rejection, and Isaac
  Lungu's overlapping doctoral/Master's descriptions.
- `tests/test_reconciliation_refinement.py`: institution/campus equivalence,
  positively distinct qualifications/dates, and conservative duplicate suppression.
- [Validation record](validation.md): final suite, lint, migration, and offline
  performance results.

The Shared Library, stable readable export filenames, source-list metadata,
LinkedIn/social exclusions, Wikipedia fallback, and PostgreSQL/Railway deployment
remain part of the existing backend.
