# KnownBy Phase 4 · Stage B

The seven-field engine is available through the offline evaluation commands below. Operational research uses the source model and extraction model path from before Stage B. See [current extraction behavior](phase-4-stage-c.md).

Completed on 10 November 2026 against backend `6b39846` and frontend `117342e`. The workspace already held the Stage A report, six synthetic cases, replay tool and recorded output. The earlier Stage B work had located the integration point, but Git and the working tree had no saved Stage B implementation. The new producer runs through an opt-in offline adapter at the existing claim-proposal boundary.

## Implementation

The adapter reuses `prepare_content`, source admission and the existing structured JSON-LD, microdata and JSON pass. It gives each residual chunk to `extract_chunk` and sends the exact same chunk with its `ExtractionResponse` to `validate_claims`. Source IDs, raw and normalized values, dates, quote offsets and selection evidence are retained by the existing schemas. The existing LLM client, model routing, orchestration, identity checks, confidence, persistence, result and export code are unchanged. There is no production switch or fallback.

| Field | Deterministic strategy |
| --- | --- |
| Full Name | Literal target observation in profile headings and biography statements. Honorifics and punctuation use the existing name key. Middle-name expansion requires the existing source anchors and identity predicate. Explicit aliases can bind adjacent statements while the observed full name remains the name claim. |
| Organisation | Person-owned employment statements, titled appositions and career rows identify named employers. A literal abbreviation remains evidence, and an explicitly paired expansion can preserve the full observed employer name. Separate roles keep separate groups. |
| Job Title | A bounded title vocabulary and employment grammar bind titles to the same employer as the quote. Literal dates and current/former cues are preserved when available. Unpaired and conflicting proposals still go through existing selection and review. |
| University Name | Education-scoped attendance, graduation, alumni and credential-issuer patterns preserve explicit institutions and recognized acronyms. Lecture attendance, employment, awards and ambiguous issuer lists do not establish an institution relationship. |
| Degree Type | Bounded terminology and existing qualification normalization recognize degree abbreviations, full titles, multiple qualifications and explicit shared modifiers. Generic degrees can keep supported subject and issuer without assigning a level. Honorary and incomplete credentials do not become earned degrees. |
| Subject | Explicit subject, major, concentration or specialization is retained within its qualification group. A shared subject is copied only where the text makes the sharing explicit. |
| Profile Link | An observed target-labelled biography link needs a person-specific profile path. A target-owned heading or title can support the retrieved URL or same-host canonical metadata. A scoped structured Person URL can support opaque profile IDs. Each proposal is grounded against the link or metadata projection. Final Profile Link selection still follows the existing representative-source rule. |

Shared sentence and row segmentation retains one bounded exact span for each relation. A qualification and an employment pair each get a source-local group, including distinct degrees from the same institution. The adapter applies the schema's 60-claim cap to whole groups. Link metadata preserves its original reference alongside a resolved relative URL. An ambiguous pronoun or adjacent namesake does not inherit the target's scope.

The implementation uses extruct-style structured projections, heading/list/table scoping on the existing Markdown and spaCy-style bounded phrase and relation rules. It keeps the existing content recovery and retrieval rather than adding Trafilatura or Crawl4AI. The normal path scans prepared text and a bounded URL metadata head. The structured link reader is used when the page still lacks a safe owned URL. There are no new dependencies or models.

## Offline accuracy and coverage

The 12 cases combine Stage A's six immutable cases and six independently labelled synthetic extensions. The extensions exercise shared subjects, adjacent microdata persons, dated current and former jobs, education tables, unrelated events and a rejected same-name source. Unknown labels are excluded from scoring. Values are counted across scoped records, and Full Name requires researched evidence.

| Field | Scored cases | Correct selected values | Precision | Recall | Grounded from the new producer |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full Name | 12 | 12 | 100% | 100% | 14 |
| Organisation | 12 | 6 | 100% | 100% | 9 |
| Job Title | 11 | 5 | 100% | 100% | 9 |
| University Name | 12 | 8 | 100% | 100% | 8 |
| Degree Type | 12 | 8 | 100% | 100% | 6 |
| Subject | 12 | 7 | 100% | 100% | 6 |
| Profile Link | 7 | 7 | 100% | 100% | 10 |

- **Supported coverage.** 53/53 reviewed values and 44/44 reviewed positive person-field slots were recovered. Production telemetry counted 50/84 slots as supported (59.5%), including values without accuracy labels. All 70 selected decisions in scoped records had a quote and source reference.
- **Relationships.** 10/10 exact education tuples, including explicitly incomplete rows, and 5/5 reviewed selected employer–title pairs matched. The audited dated biography also preserved both the current and the ended pair. The same-date title dispute continued to trigger review. Its selected title is unverified.
- **Claim flow.** All 72 proposals grounded, including 62 from the new producer and 10 from the existing structured pass. The direct-link audit recovered all four reviewed links across the six extensions. The labelled same-name source was rejected at source admission. No forbidden claims in the labelled extensions grounded. The direct proposal audit initially caught a false attendance claim from a lecture mention. A separate stress test exposed a false pronoun attribution after a family relationship. Both have regression coverage and now abstain.
- **Matched Stage A comparison.** The two paths shared 25 correct values. Stage B alone recovered Geology in the explicit MSc quote, completing the missing tuple and moving exact tuple recall from 4/5 to 5/5 on the same six cases. No previously correct value was lost. The supported-slot count remained 25/42 because the Subject field already had another value for that person. Seven scripted replies were replaced in the replay. Their count measures fixture work replaced and provides no live API-savings estimate.

Stage A found no complete original source-and-reply capture for the named production cases. A comparison to those original LLM outputs remains unavailable. Stage A's historical per-person cost and latency describe different cohorts. The current evaluation provides synthetic correctness and local performance measurements.

## Local performance

Twenty timed repeats after one warm-up on Windows with Python 3.12. Values below are medians for all 12 cases. Shared relation matcher time applies jointly to the listed fields.

| Work | Median ms |
| --- | ---: |
| Shared evidence spans | 0.615 |
| Full Name | 0.224 |
| Organisation and Job Title together | 0.714 |
| University Name, Degree Type and Subject together | 1.159 |
| Profile Link in chunks | 0.214 |
| Profile Link page metadata | 4.497 |
| Proposal deduplication and schema assembly | 0.076 |
| Total new extraction, including metadata | **8.491** |
| Existing page/structured preparation | 25.733 |
| Grounding | 6.223 |
| Reconciliation | 57.238 |
| Whole replay including evaluation overhead | **107.363** |

Whole replay wall time ranged from 105.577 to 206.559 ms. Median process CPU was 109.375 ms using the coarse Windows process clock. One separately traced replay peaked at 858,123 bytes of Python allocations, excluding interpreter baseline, native allocations and process RSS. There were zero live model calls and zero paid inference requests. These measurements exclude retrieval, queueing and production persistence.

## Reproduction and verification

Run from the backend root with its Python environment active. The Stage A report describes the local Python 3.12 environment used here.

```powershell
New-Item -ItemType Directory -Force output/phase-4-stage-b
python -m benchmarks.stage_b_evaluation --output output/phase-4-stage-b/evaluation.json
python -m benchmarks.stage_b_evaluation --repeats 20 --output output/phase-4-stage-b/evaluation-timed.json
```

The untimed report was byte-identical across Python hash seeds 17 and 97. Its SHA-256 is `0194ae4af6b35e39e13eec2665fc1cef2b19c2d2ea7d047d5e0600f7fb0939d1`. The Stage A CLI reproduced the original SHA-256 `2e11c4ff5bf22b79e6f3660f4e98487ed642bf2e0d56c39aa01ee4e934ab5b00`.

The focused set passed 64 tests covering all fields, relationships, provenance, repeatability and client/storage tripwires. Eleven mocked legacy LLM extraction and recovery tests passed separately. The broader backend run passed 1,116 tests, with 13 PostgreSQL cases deselected. It used an in-process event loop without a local socket because the usual Windows event loop stalls in this workspace. HTTP transport remained mocked and live socket/DNS calls were blocked. All 70 frontend tests passed and Ruff passed on the new and changed Python files.

## Changed files

- [`app/research/deterministic/__init__.py`](../app/research/deterministic/__init__.py), [`context.py`](../app/research/deterministic/context.py), [`identity_fields.py`](../app/research/deterministic/identity_fields.py), [`employment.py`](../app/research/deterministic/employment.py) and [`education.py`](../app/research/deterministic/education.py) implement the producer.
- [`benchmarks/stage_a_replay.py`](../benchmarks/stage_a_replay.py) accepts an optional proposal stream while retaining the default Stage A replay. [`stage_b_evaluation.py`](../benchmarks/stage_b_evaluation.py) and [`stage_b_cases.json`](../benchmarks/stage_b_cases.json) supply the comparison and fixtures.
- [`tests/test_deterministic_identity_fields.py`](../tests/test_deterministic_identity_fields.py), [`test_deterministic_employment.py`](../tests/test_deterministic_employment.py), [`test_deterministic_education.py`](../tests/test_deterministic_education.py), and [`test_stage_b_evaluation.py`](../tests/test_stage_b_evaluation.py) verify the new path. This report records the result.

The frontend working tree remained clean. Graphify refreshed its local graph. Evaluation JSON is in the ignored `output/phase-4-stage-b/` directory. No commit was created.

## Stage C recommendation

Ready for a controlled **read-only shadow comparison on approved retained real-world evidence**. Before considering production activation, freeze the same source evidence and seed context for both arms, retain any recorded LLM replies, and independently label person identity, current and former roles, qualification tuples and profile URLs. Compare grounded proposals and final selections, and record extraction CPU/time separately from discovery and source-validation work. Keep shadow results outside production job state and use zero extraction model calls on the deterministic arm.

Known gaps include cross-paragraph coreference, unfamiliar acronyms and non-English relationship syntax, implicit institutions, complex timelines, and unconventional or conflicting URL metadata. Existing source compaction and identity thresholds can also withhold otherwise valid evidence. Require stable namesake, provenance and confidence/review behavior on held-out cases and inspect disagreements before any production default change.
