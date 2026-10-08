# Partial-failure salvage

Validated on **2026-10-08** against baseline `cb210c6`. This refinement keeps the
existing research architecture, budgets, confidence rules, source policy, and recovery
flow. It changes how already-earned evidence survives local failures and how terminal
outcomes describe the stage that actually prevented a result.

## Root cause

Three behaviors combined to make a partially successful person appear wholly failed:

1. A seeded identity anchor could remain an absolute selection veto when several
   independent retrieved sources consistently established a newer or corrective
   current organisation. The sources were accepted and their claims were grounded,
   but every source stayed provisional, so `selection_eligible_source_count` became
   zero. An anchored page whose extraction failed could also count as a direct anchor
   despite having no claim to select, suppressing the corrective-cluster fallback.
2. Claim validation treated an unsupported extracted date as a reason to discard the
   complete claim, even when the value and quotation were literal and grounded.
3. Terminal classification treated any retained provider/retrieval diagnostic as the
   person's outcome. A failed page could therefore label a person `RETRIEVAL_FAILED`
   after other pages had been retrieved and extracted successfully.

Recovery already accumulated claims. The failure was primarily at validation,
selection eligibility, and final status classification rather than discovery capacity.

## Salvage behavior

- **Source scope:** a page that cannot be retrieved remains failed. Independent
  successful pages continue through extraction, identity, reconciliation, and field
  selection.
- **Claim scope:** an ungrounded value, evidence span, subject, or link rejects that
  claim and records its reason. Other grounded claims from the same source remain.
- **Date scope:** an unsupported `as_of_date` or `end_date` is removed and logged as
  `UNGROUNDED_CLAIM_DATE`; the otherwise grounded claim remains eligible.
- **Field scope:** an invalid education claim cannot erase an independently grounded
  organisation/title relationship, and the reverse is also true.
- **Recovery scope:** the accumulated claim ledger is additive. A failed follow-up
  search or retrieval does not reset evidence selected before recovery.
- **Budget scope:** `MAX_SOURCES` records why discovery stopped. If valid evidence is
  already available, reconciliation finalizes it and the person succeeds.

No grounding threshold, blocked-source rule, confidence threshold, or namesake check
was weakened.

## Selection gate

Direct seed-anchor matches and the existing one-hop identity bridge remain the primary
admission rules. When no page repeats the seed anchor, one narrow deterministic fallback
can admit a corrective cluster only if all of these hold:

- at least two independent, non-mirrored domains agree;
- each source has directory-or-better authority and passes the existing identity floor;
- each claim is explicit, sufficiently normalized, and says the organisation is current;
- the organisation is specific rather than a generic term; and
- there is exactly one corroborated cluster.

A direct anchor participates in this decision only when that source retained at least
one grounded claim. An extraction-failed anchor with no selectable claim cannot veto
independent surviving evidence.

A lone page, historical/alumni clue, generic organisation, weak identity, mirrored
content, same-domain repetition, or multiple namesake clusters remains ineligible. This
lets seed context remain a hypothesis while preserving the Elizabeth Adams-style
abstention behavior.

## Error taxonomy and outcomes

The durable error-code list still records all source and provider diagnostics for
backward compatibility. The additive `metrics.failure_reason` records the terminal
stage only when coverage is zero:

| Reason | Meaning |
| --- | --- |
| `RETRIEVAL_FAILED` | Sources were attempted, every attempted retrieval failed, and no usable page was obtained. |
| `GROUNDING_FAILED` | Extraction returned claims, but none survived deterministic grounding. |
| `IDENTITY_UNRESOLVED` | Grounded claims remain, but no source is identity-safe for final selection. |
| `NO_ELIGIBLE_EVIDENCE` | Identity-eligible claims exist but no enrichment field survived selection. |
| `INSUFFICIENT_EVIDENCE` | The bounded run completed without enough evidence and without a more specific technical cause. |
| Existing provider/extraction code | A technical failure prevented meaningful research before usable evidence existed. |

Person outcomes are now:

- `clean`: full usable evidence-derived coverage;
- `partial_coverage`: usable selected fields with unresolved fields and no review need;
- `needs_review`: usable evidence that meets the existing review rules;
- `insufficient_evidence`: zero usable coverage due to evidence/identity/selection; or
- `retryable_research_failure`: zero usable coverage due to a technical, provider,
  extraction, or genuine retrieval failure.

Every zero-coverage task is unsuccessful rather than an ordinary completed profile.
`review_required` remains an evidence-review signal and is never used as a failure flag.
At job level, a terminal mix keeps the compatible `partial` status and the label
**Completed with issues**. `JobView` and `JobResults` add `outcome_counts` containing
terminal `attempted`, `successful`, and `unsuccessful` counts.

## Decision trace

The compact INFO result summary now includes:

- sources attempted, retrieved, and failed;
- claims extracted, grounded, rejected, and selected;
- selection-eligible source count;
- final selected-field count;
- person outcome and terminal failure reason.

DEBUG claim-validation events report rejection and adjustment counts by concise reason.
Identity entries distinguish retrieval failure, no selectable claims, ordinary identity
rejection, and the coherent corrective-cluster fallback. Logs remain bounded and omit
source bodies, prompts, provider responses, credentials, and export-only diagnostics.

## Regression coverage

`tests/test_partial_failure_salvage.py` covers the observed six-candidate shape with
three failed and three successful sources, ten mixed claims, and `MAX_SOURCES`; grounded
fields survive and the person does not become `RETRIEVAL_FAILED`. It also covers:

- unsupported education dates with retained university, degree, and subject;
- mixed valid organisation/title and invalid education claims;
- true all-source retrieval failure;
- failed recovery after an already-grounded university;
- grounded but disconnected identity evidence;
- extracted claims that all fail grounding;
- disconnected same-name groups that must remain empty; and
- an extraction-failed seed-anchored page with no claims that must not block a coherent
  surviving current-organisation cluster.

The full offline suite passes **739 tests**, with six optional PostgreSQL runtime tests
skipped because `TEST_POSTGRES_URL` was unset. The existing 14-case frozen quality panel
remains green.

## Performance

The production offline harness ran the same 1, 5, 25, and 100-person fixtures at 2 ms
mock latency against both `cb210c6` and this change. Normalized result hashes and all
work counts match. Searches remain 1/5/25/100; model attempts remain 4/20/100/800;
extractions remain 2/10/50/600; physical fetches remain two per batch; and completed
result reads remain six SQL queries. Local elapsed times were 0.552/0.410,
1.955/1.576, 9.238/7.607, and 61.037/60.612 seconds. These single runs are non-gating
and do not establish a general speedup. No live provider request was made.

Compact benchmark evidence is in
[`benchmarks/partial-failure-salvage.json`](../benchmarks/partial-failure-salvage.json).
No database table, export column, search limit, source limit, model limit, token limit,
or prompt was changed.
