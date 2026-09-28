# Evidence confidence, version `evidence-v2`

KnownBy calculates confidence in Python. Model responses supply grounded claims and
source classifications; they never supply confidence percentages. Scores are
deterministic evidence-strength heuristics, not calibrated probabilities.

## Source authority

| Authority class | Default weight |
| --- | ---: |
| First-party person profile | 0.95 |
| Government/public institution | 0.95 |
| Employer/organisation | 0.90 |
| University | 0.90 |
| Professional/regulatory body | 0.85 |
| Established publication | 0.80 |
| Conference/event biography | 0.72 |
| Structured professional directory | 0.65 |
| Encyclopedia/Wikipedia | 0.60 |
| Aggregator | 0.45 |
| Social/user-generated | 0.35 |
| Unknown | 0.25 |

Authority and identity remain separate. An authoritative page about another person
is rejected. Government classification is country-neutral and includes official
ministries, departments, agencies, legislatures, public bodies and public-office
biographies when the evidence supports that classification. A party, residence,
building, location or news page is not treated as the person's organisation merely
because it is mentioned. Operator hostname overrides still use suffix-boundary
matching; there are no country-specific office mappings.

Wikipedia hostnames, including language and mobile subdomains, are classified as
`encyclopedia` before authority is scored, regardless of advisor/model labels or
operator domain overrides. Wikipedia can support or corroborate a value, but it cannot
acquire first-party or government authority through either mechanism. When important
role or education fields remain unresolved after normal source planning, orchestration
may reserve one bounded Wikipedia query within the existing search budget. It is
skipped when Wikipedia has already been considered and is never required for every
person. At authority **0.60**, it may complete a credible missing value but loses a
conflict to stronger current official evidence.

## Identity

The validated seed name is input identity, not an enriched web field. Reconciliation
copies that exact name into every output record with deterministic confidence **100**,
no selected web claim or source provenance, and no name-field review. Web evidence
cannot blank, expand, or replace it. This number expresses input certainty only; web
identity matching and ambiguity remain separate internal decisions.

All other seed context is an unverified hypothesis. It can narrow the first query,
increase the rank of matching candidates, reject unrelated namesakes, and guide bounded
follow-up research, but it contributes no claim, source provenance, field confidence,
or selected value by itself. A row with only a name uses the same identity and evidence
pipeline without contextual terms.

The complete normalized seed name must occur as a bounded phrase in source text and
the extracted `subject_name` must match it. Exact name alone starts at **0.55**.
Each distinct supplied organisation or university clue locally associated with the
seeded name adds **0.27**; each other distinct local clue adds **0.18**. Sentence and
short profile-header boundaries prevent an unrelated page-wide mention from becoming
an identity anchor. A model assessment may reject or constrain a match, but cannot
promote it to certainty.

Seed organisation and university can be identity anchors. Header semantics refine the
prior without establishing a fact: a current employer or government office is a
stronger current-role hypothesis, a generic organisation is an affiliation clue, and
an alumni organisation or former employer is supporting identity/history context that
is not automatically current. Specific known attributes can also be anchors when their
labels and values identify the person; country, location, subject, and year remain
supporting clues. If no stronger anchor is supplied, job title is the fallback.

When a seed has an anchor, at least one source must match it before that source's claims
may populate final fields. Missing anchor text is provisional rather than contradictory:
the page may still be extracted and retained, but it cannot win a field by agreeing
with other unanchored namesakes. An explicit denial of a seeded affiliation is recorded
as a contradiction and keeps the source provisional; it does not reject the source at
ingestion. Ordinary negated actions such as “did not leave” are not misread as denials.
The contradiction can become directly eligible only when reliable exact-name evidence
supplies an explicit current organisation/title pair, or an explicit current
organisation from a first-party, government, employer, university, or professional
body source. Other contradictions still need an independent seed-linked bridge.
Retrieved corroboration strengthens identity through the normal score. When newer
reliable web evidence establishes another current employer, that evidence can win
current-role selection; the seeded employer remains a research clue and is not emitted
as a current or historical fact without its own evidence.

Name-only research can strengthen identity after extraction. An explicit normalized
organisation, title, university, degree or subject must agree on another independent
domain with different content. The source cannot validate itself, same-domain pages
do not help, exact mirrors do not help, and sources below directory-level authority
do not create new identity anchors. An independent source may join a directly anchored
cluster through one explicit shared institution, or two other explicit context fields;
bridges are one hop and cannot chain an unrelated cluster into eligibility. Context
bonuses are capped and leave the stored page-level identity assessment unchanged.

The internal web-name evidence decision has one additional narrow rule. Two independent,
non-mirrored, explicit exact-name claims from publication-or-better sources receive an
identity floor above the review threshold. This can resolve web-identity ambiguity for
record review, but it never changes the immutable output name, its 100 input-certainty
score, or the identity score used for unrelated facts.

## Claim eligibility and field quality

Every model claim must be literal in the supplied source chunk, contain its raw value,
name the seeded person, and use grounded dates. Raw values and excerpts remain in the
claim ledger. Deterministic normalization handles Unicode, punctuation, conservative
organisation suffixes, subject aliases and common degree forms. Selected degree output
uses `Bachelor's Degree`, `Master's Degree`, `Doctoral Degree`, `Medical Degree`,
`Law Degree`, `Diploma`, `Postgraduate Diploma`, or `Postgraduate Degree`; common
terms such as `maestría`, `licence`, `laurea`, and `promovierte` use explicit
accent-insensitive mappings. A generic `degree` defaults to `Bachelor's Degree` with
`GENERIC_DEGREE_DEFAULT` metadata and reduced normalization certainty. Unknown terms
remain in raw evidence with `AMBIGUOUS_EDUCATION` but do not become selected degree
records. Explicit subjects inside qualification titles create a grounded derived
Subject claim with the same source, excerpt, raw degree title, and fact group; no model
call is added. No translation API is used.

Field quality is relationship-aware:

- a web name variant is assessed internally against the seed while the output name
  remains unchanged;
- a job title without an organisation in its source-local employment group is
  penalized and reviewed;
- sentence-like biographical prose and unusually long title fragments receive a
  deterministic quality penalty; clear descriptive prose can fall below selection;
- a subject without a university or degree relationship is penalized and reviewed;
- an unrecognized degree phrase retains its raw evidence, receives both normalization
  and field-quality reductions, and is reviewed;
- education fields can support a record only from the credential cluster assigned to
  that record.

The code does not use a large organisation, role or country dictionary.

## Field formula

For an eligible claim `c`:

```text
A = source authority, 0..1
I = effective identity for this source/field, 0..1
D = directness: explicit 1.00; implied 0.72; ambiguous 0.35
N = normalization certainty, 0..1
Q = deterministic field quality, 0..1
P = 0.02 for a preferred source; otherwise 0
R = recency factor
T = field-aware time multiplier

base(c) = 0.55*A + 0.30*D + 0.15*R
strength(c) = I * N * Q * min(1, base(c) + P) * T
```

For organisation and job title, dated evidence decays with a 730-day half-life.
An undated claim with explicit `is_current=true` uses `R=1` and `T=1`; present-tense
currentness is stronger than missing temporal information, while identity, authority,
directness, and conflicts still apply. Otherwise unknown recency is **0.65** and uses
`T=0.90`; absence of a date alone is uncertainty, not proof that a role is old. Dated
volatile evidence uses `T=0.35 + 0.65*R`, making stale high-authority evidence
materially weaker. Explicitly ended/former roles are historical alternatives and do
not populate current fields. Education and internal web-name claims use `T=1` and do
not decay; the representative-link score is derived from the selected claims it
summarizes.

Equivalent normalized values form a support group. Independent support is computed
with a maximum distinct domain/content-hash pairing. The strongest claim is the base;
each additional independent representative adds `0.08 * its own strength`, capped at
**0.20**. This makes strong corroboration useful without allowing many weak pages to
outvote first-party evidence. The strongest conflicting claim applies a
`0.25 * conflict_strength` penalty.

```text
field_confidence = 100 * clamp(best_strength + corroboration_bonus
                               - conflict_penalty, 0, 1)
```

Reasons and all components are returned with the field decision. The immutable name is
never field-reviewable. An enriched populated field is reviewable when confidence is
below the centrally configured threshold (**50** by default), a conflicting claim has
strength of at least `0.50` and at least 75% of the selected claim's strength,
normalization is ambiguous, source quality is weak, identity is unresolved, or a
relationship/grouping is uncertain. Weaker conflicts remain recorded without creating
a field-review task. Missing fields and `LOW_SOURCE_COUNT` are diagnostic and do not
themselves require field or record review; one strong direct authoritative source can
still be usable.

A separate centrally configured selected-value floor defaults to **10**. A field or
representative link below that floor is returned as null, with no selected claim, while
its raw claims remain in the evidence ledger and their IDs move to alternatives with
`BELOW_SELECTION_FLOOR`. This does not alter seed/job tracking metadata or add export
columns.

Record review is separate and intentionally rare. A record is escalated only for web
identity ambiguity, an unresolved current-role conflict or incomplete role
relationship, education-grouping ambiguity, a descriptive job title, a critical field
or representative link suppressed below the selection floor, a material critical-field
conflict, no reliable representative source for otherwise substantive enrichment, or
at least two populated critical fields below the review threshold. The guaranteed seed
name is excluded from these research-quality checks.
One weak optional subject, a suppressed optional subject, one fetch failure, a weak
non-material conflict, or incomplete coverage does not escalate the record.
Zero-coverage discovery outcomes use `research_status=insufficient_evidence`;
provider/retrieval errors remain retry/research failures, while clean and reviewable
profiles use `clean` and `needs_review` respectively.

## Education records

Degree terminology is normalized and classified before `fact_group` bundles are
reconciled. `fact_group` is local to one source and does not by itself prove that two
same-level credentials are distinct. Reconciliation starts from the cautious rule that
one qualification remains one record until positive evidence establishes another.

- Different controlled degree types establish separate credentials.
- The same normalized degree and institution merge into one record and retain all
  supporting sources. A comma- or semicolon-appended location qualifier, such as
  `University of Dundee, Scotland`, is ignored for grouping while raw evidence and the
  selected display value remain available.
- Different subject wording at the same institution and level remains one credential
  with competing subject claims. Subject disagreement alone does not create grouping
  ambiguity; a material subject conflict may still make the Subject field reviewable.
- Distinct qualification labels or distinct dates positively establish separate
  same-level credentials.
- Separate source-local groups establish separate same-level credentials only when
  the same source also identifies materially different institutions.
- Cross-source same-type evidence with two incompatible contextual components
  establishes distinct credentials; one non-subject contextual disagreement remains
  an ambiguous record rather than proof of two qualifications.
- Ungrouped components are never spliced together merely to fill missing fields.
- A university-only, subject-only, or otherwise degree-free bundle cannot create a
  credential row. It may corroborate exactly one compatible proven credential;
  otherwise it remains orphan alternative evidence.
- A safe compound claim containing distinct controlled types, such as `maestría y un doctorado`,
  expands before grouping while every expanded claim retains the same literal raw value.
- Certifications, executive programmes, honorary awards, postdoctoral work, ongoing
  study, and training remain alternative evidence and do not create earned-degree rows.
- Vague values such as `two degrees`, `several degrees`, `specialization course`, and
  `graduate studies` remain alternatives; ambiguous values make the degree field
  reviewable without manufacturing a credential.

Each confirmed credential becomes a `ProfileRecord`. The immutable seed name and
selected current-role decisions are copied into every record; university, degree and
subject decisions are scoped to that credential. No confirmed education produces one
general record.
`PersonProfile.fields` remains the deterministic primary-record projection for older
clients, while `PersonProfile.records` is the complete additive result.

## Current employment

Organisation and job title are ranked as relationship clusters. A deterministic
freshness band wins first, then relationship-type priority, explicit current status,
exact observation date, relationship completeness, evidence strength and a stable
signature. An explicitly current undated role receives full recency/currentness
signals rather than the generic unknown-date discount; this does not bypass identity,
authority, directness, pairing, or conflict checks.
This lets a recent primary role beat a stale `is_current` label while preventing a
board page updated a few days later from displacing a current CEO. The compact
taxonomy covers government office, executive/primary employment, academic, board,
advisory, political-party, historical and other affiliations. Current government
office outranks an equally current party/private affiliation; executive employment
outranks an equally current board/advisory role. Both selected fields come from the
same cluster. Historical roles remain alternatives. Equally current, same-type,
similarly supported relationships produce `CURRENT_ROLE_CONFLICT` instead of mixed
fields. An exact seed country/location is excluded as an organisation and retained as
alternative evidence; the reconciler never invents an office name.
Public residences and similarly named government buildings are excluded under the
same rule when the paired title and government source identify them as places rather
than employing organisations.

The public-office classifier records a compact subtype for head of state, head of
government, ministerial, legislature, government department, government agency,
executive office, judiciary, local government, diplomatic mission, or public
institution. Generic `agency` and `department` terms require government context, so a
talent agency or university department does not become a government employer. A short
country-like value paired with a national leadership title is retained as an
alternative even when the seed did not supply the country. Exact institution names
must still come from evidence; reconciliation does not invent an office label.

If a government, executive, or primary current title is populated at or above the
review threshold while its organisation remains null, orchestration may make one
bounded institution-completion search using the exact name, title, available
country/location, and `official`. If important role or education fields remain missing
after normal planning, one lower-authority Wikipedia query may then be queued within
the same hard search limits. Retrieved claims still pass the normal identity,
grounding, pairing, source-authority, and conflict rules.

## Representative profile link

The representative link is derived after the other six fields for each record. For
every source, reconciliation counts distinct selected fields that source supports.
If a directory-or-better source contributed, aggregator, social and unknown sources
are excluded from winning. Remaining candidates are ordered by:

1. selected-field contribution count;
2. authority;
3. directness;
4. effective identity;
5. relevant recency;
6. aggregate contribution strength;
7. canonical URL.

If only weak sources exist, the best deterministic fallback is returned with
`LOW_SOURCE_AUTHORITY` review. Because education support is record-scoped, separate
credentials can choose separate representative links. No extra model call is used.

## Profile confidence and coverage

For each output record:

```text
research_fields = organisation, job_title, university_name, degree_type,
                  subject, profile_link
present = research_fields whose selected value is not null
profile_confidence = weighted mean(field_confidence for present research_fields)
coverage = 100 * number_of_present_research_fields / 6
```

The immutable `full_name` decision is deliberately absent from both formulas, so its
100 input-certainty score cannot inflate research quality or turn a no-evidence result
into partial coverage. Coverage measures evidence-derived completeness and never
changes a field's confidence. The legacy profile exposes the primary record's
confidence and coverage; every `ProfileRecord` has its own values.

All tunable numeric weights remain in `ScoringPolicy`. Partial nested environment
overrides merge with defaults, for example `SCORING__SELECTION_THRESHOLD=10` and
`SCORING__REVIEW_THRESHOLD=50`. The selection floor must not exceed the review
threshold. Tests use the production reconciliation and scoring modules with
deterministic source fixtures.
