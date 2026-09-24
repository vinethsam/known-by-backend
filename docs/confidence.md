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

## Identity

The complete normalized seed name must occur as a bounded phrase in source text and
the extracted `subject_name` must match it. Exact name alone starts at **0.55**.
Distinct supplied clues found in the page add **0.18** each. A model assessment may
reject or constrain a match, but cannot promote it to certainty.

Name-only research can strengthen identity after extraction. An explicit normalized
organisation, title, university, degree or subject must agree on another independent
domain with different content. The source cannot validate itself, same-domain pages
do not help, exact mirrors do not help, and sources below directory-level authority
do not create new identity anchors. Context bonuses are capped and leave the stored
page-level identity assessment unchanged.

The full-name field has one additional narrow rule. Two independent, non-mirrored,
explicit exact-name claims from publication-or-better sources receive a field-only
identity floor above the review threshold. This prevents a well-corroborated name
from remaining near the name-only baseline while avoiding trust transfer to that
source's other facts.

## Claim eligibility and field quality

Every model claim must be literal in the supplied source chunk, contain its raw value,
name the seeded person, and use grounded dates. Raw values and excerpts remain in the
claim ledger. Deterministic normalization handles Unicode, punctuation, conservative
organisation suffixes, subject aliases and common degree forms.

Field quality is relationship-aware:

- a selected name should normalize to the seed name;
- a job title without an organisation in its source-local employment group is
  penalized and reviewed;
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

base(c) = 0.55*A + 0.30*D + 0.15*R
strength(c) = I * N * Q * min(1, base(c) + P)
```

For organisation and job title, dated evidence decays with a 730-day half-life.
Unknown recency is **0.65**. Explicitly ended/former roles are historical alternatives
and do not populate current fields. Education and name claims do not decay; the
representative-link score is derived from the selected claims it summarizes.

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

Reasons and all components are returned with the field decision. Review remains
required for missing evidence, meaningful conflicts, low confidence, unresolved
identity, unpaired relationships, unrecognized degree language, unclear current-role
selection or ambiguous education grouping. `LOW_SOURCE_COUNT` is diagnostic; one
strong direct authoritative source can still be usable.

## Education records

`fact_group` is local to one source. Reconciliation first creates source-local
education bundles, then merges bundles from other sources only when at least one
normalized component agrees and no populated component conflicts.

- Different normalized degree levels establish separate credentials.
- The same degree and compatible institution/subject merge into one record and retain
  all supporting sources.
- Explicit source-local groups can establish distinct same-level credentials when
  their populated details conflict.
- Cross-source same-level evidence with incompatible details remains one ambiguous
  review record until the relationship can be resolved.
- Ungrouped components are never spliced together merely to fill missing fields.

Each confirmed credential becomes a `ProfileRecord`. Name and selected current-role
decisions are copied into every record; university, degree and subject decisions are
scoped to that credential. No confirmed education produces one general record.
`PersonProfile.fields` remains the deterministic primary-record projection for older
clients, while `PersonProfile.records` is the complete additive result.

## Current employment

Organisation and job title are ranked as relationship clusters. Explicit current
status wins first, then the newest supported observation, relationship completeness,
evidence strength and a stable signature. Both selected fields come from the same
cluster. Historical roles remain alternatives. Equally current, similarly supported
relationships produce `CURRENT_ROLE_CONFLICT` and review instead of mixed fields.

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
present = fields whose selected value is not null
profile_confidence = weighted mean(field_confidence for present fields)
coverage = 100 * number_of_present_fields / 7
```

Coverage measures completeness and never changes a field's confidence. The legacy
profile exposes the primary record's confidence and coverage; every `ProfileRecord`
has its own values.

All tunable numeric weights remain in `ScoringPolicy`. Partial nested environment
overrides merge with defaults, for example `SCORING__REVIEW_THRESHOLD=80`. Tests use
the production reconciliation and scoring modules with deterministic source fixtures.
