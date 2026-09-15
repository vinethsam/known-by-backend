# Evidence confidence, version `evidence-v1`

Scores describe the strength of the evidence used by this implementation. They are
deterministic heuristics, **not calibrated probabilities that a fact is true**.
Calibration against reviewed profiles is future work. Neither runtime model emits
the final numeric field or profile confidence.

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

Model A classifies the source from discovered metadata. This is a judgment signal,
not verification of a domain's institutional ownership. Operator domain overrides
can correct known classifications. A longest matching hostname suffix override wins;
`example.gov` never matches `example.gov.attacker.net`. Authority and identity remain
separate: an official page about the wrong person is rejected.

## Identity

The complete normalized seed name must occur as a bounded phrase in selected source
text. A different extracted `subject_name` rejects that claim. Exact name alone has
identity strength **0.55** and requires review. Each distinct supplied identity clue
found in the text adds **0.18**, capped at 1. Repeated copies of a clue and the person's
own name do not count twice. Clues include organisation, country, location, university,
subject, program year and explicitly supplied `known_attributes`.

Model A can reject an unrelated candidate or cap an ambiguous match at 0.80; it cannot
raise an otherwise ambiguous same-name match to certainty. Identity below **0.45** is
excluded from field decisions, and below **0.80** requires review. Missing a clue is
not proof of a mismatch: employers and locations may change. Near-name variants are
conservatively rejected in this POC and require an improved seed/review.

Identity clues are user-supplied anchors. Facts learned during research refine search
queries; they do not silently become trusted identity anchors. Unrelated batch columns
stay in the original row and are never used as identity evidence.

## Claim eligibility and normalisation

Model B returns a validated `ExtractionResponse`, not a flat profile or biography.
The excerpt must occur literally in the provided chunk (Unicode/whitespace insensitive),
and the extracted raw value must occur within the excerpt. The subject must match the
seed name. Unknown values remain absent. Dates cannot be in the future and must occur
in the excerpt as ISO dates or supported English full-date forms. A year alone does not
justify inventing a month/day. Dates remain model-extracted signals rather than verified
publication metadata; reviewers can inspect the preserved excerpt and raw claim.

Raw values are always kept. Deterministic normalization produces comparison keys:
case, Unicode, whitespace, punctuation, limited legal suffixes and conservative subject
aliases. Recognized degrees map to Bachelor's, Master's or Doctorate. Unrecognized
degree descriptions retain their comparison text with normalization certainty 0.75.
Job-title synonyms are not merged by an LLM in this version; uncertain differences
remain visible for review.

## Field formula

For each eligible claim `c`:

```text
A = source authority, 0..1
I = min(source identity score, claim identity relevance), 0..1
D = directness: explicit 1.00; implied 0.72; ambiguous 0.35
N = normalization certainty, 0..1
P = 0.02 for a preferred source; otherwise 0
R = recency factor

base(c) = 0.55*A + 0.30*D + 0.15*R
strength(c) = I * N * min(1, base(c) + P)
```

For organisation and job title, `R = 2^(-age_days/730)` when an explicit as-of or
publication date is available. Unknown recency is **0.65**. Retrieval time is never
treated as publication time. For stable/historical education fields, names and profile
links, `R = 1`. Claims explicitly marked former/ended are retained as alternatives,
and do not populate a current-role field. The scoring helper gives such claims a
0.25 recency factor if evaluated independently.

Equivalent normalized values form a support group. Let `B` be its strongest claim,
`k` its number of independent domains/content hashes, `Imax` its maximum supporting
identity and `C` the strongest conflicting claim:

```text
bonus = min(0.20, 0.08 * max(0, k - 1))
penalty = 0.25 * strength(C)      # zero when no credible conflict exists
field_confidence = round(100 * clamp(B + bonus*Imax - penalty, 0, 1), 2)
```

Multiple pages on one domain do not increase `k`. The same content copied to several
domains also counts once. A maximum domain/content pairing makes independence counts
order-independent and ensures adding corroboration cannot reduce the score. Domain
grouping uses a conservative common-public-suffix heuristic; it does not establish
that separate publishers have separate ownership. Identical body hashes catch exact
mirrors; lightly edited syndication may require manual review.

Default candidate *retrieval ranking* is a separate weighted sum: authority 0.45,
relevance 0.35, metadata identity 0.15, preferred status 0.05. Candidates are then
interleaved across domains. These values help allocate the research budget; they are
not the final field score.

## Selection and conflicts

Select the support group with the best deterministic score. Preserve supporting,
conflicting and alternative claim IDs, supporting source IDs/URLs, selected claim ID,
and the full component dictionary. Education/employment `fact_group` values associate
components from one source record. A flat result does not borrow a university or
subject from a different degree to fill a gap. The current POC selects one strong
record; it does not attempt a complete career/education timeline.

Different degrees, institutions or subjects can coexist. They remain alternatives
and trigger `MULTIPLE_VALUES` review, instead of automatically becoming contradictions.
Explicitly historical employment stays alternative evidence. Disagreeing eligible
current claims on different sources incur the conflict penalty and `SOURCE_CONFLICT`.
Several legitimate profile links are alternatives, not contradictory biographical facts.

Review is required for missing values, conflicts, low confidence (below **75**),
identity ambiguity, or multiple education/employment alternatives. Additional reason
codes include `LOW_SOURCE_COUNT`, `LOW_DIRECTNESS`, `UNKNOWN_RECENCY`,
`OUTDATED_CURRENT_ROLE`, `UNPAIRED_FACT`, and `MISSING_FIELD`. Reasons are structured
codes; there is no generated confidence narrative.

## Profile confidence versus coverage

```text
present = fields whose selected value is not null
profile_confidence = weighted mean(field_confidence for present fields)
coverage = 100 * number_of_present_fields / 7
```

All field weights default to 1. An empty profile has confidence 0 and coverage 0.
One populated field at confidence 95 gives profile confidence **95** and coverage
**14.29%**. Adding a second populated field at confidence 45 gives confidence **70**
and coverage **28.57%**. Missing fields are not filled from the seed just to improve
coverage. Every missing field still returns a decision with confidence 0.

## Configuration and tests

All numeric evidence and candidate weights live in `ScoringPolicy` in `app/config.py`.
Set nested environment overrides, for example:

```dotenv
SCORING__REVIEW_THRESHOLD=80
SCORING__CORROBORATION_STEP=0.06
SCORING__DOMAIN_OVERRIDES={"example.gov":"government"}
```

Whole policy maps can be supplied using `SCORING=<JSON object>`. Partial nested maps
merge with defaults through explicit policy validation. Base and candidate ranking weights must sum
to one. Invalid weights fail configuration validation.

`tests/test_evidence.py` protects authority, corroboration, copied content, conflict
penalties, identity, recency, normalization, grounding, missing values, multiple facts,
score bounds and the separate profile/coverage formula. Provider and pipeline tests
verify schema validation, usage accounting, and budgets around retry attempts.
