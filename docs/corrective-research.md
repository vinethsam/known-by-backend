# October 26 research corrective release

Validation date: October 27, 2026. This report describes targeted changes to the existing identity, reconciliation and provider paths. No provider requests were made during development or validation, and both configured research roles remain `openai/gpt-5.6-luna`.

## Evidence and limits

The investigation used `logs.1792991728659.json`, the October 26 Royal Academy architecture export and the supplied PDL baseline. The log contains decision-summary counts, seed context, processed domains and provider-attempt diagnostics. It does **not** contain the underlying page bodies, full claim ledger or per-source identity signals. Consequently it establishes the observed failures, but cannot establish which individual original claim or source was valid. No original claim has been declared valid merely because it was grounded.

The before/after results below are deterministic offline reconstructions using the actual PDL field labels and representative biography statements. They reproduce specific implementation defects against baseline commit `83cd7386f76ca6db832a6fee3071dbf16e3c9340`. They are not a replay of the original live run and do not establish a live coverage improvement. The complete values, selected/supporting claim IDs, confidence and temporal labels are recorded in [the comparison artifact](../benchmarks/october26-corrective-research.json). Fixtures are in [test_october26_corrective_research.py](../tests/test_october26_corrective_research.py).

## Identity false rejection

The observed log summaries were:

| Person | Grounded claims | Eligible sources | Selected claims | Outcome |
|---|---:|---:|---:|---|
| Achille Mbembe | 65 | 0 | 0 | Identity unresolved |
| Elahe Mohammadi | 14 | 0 | 0 | Identity unresolved |
| Jonathan Anderson | 11 | 0 | 0 | Identity unresolved |
| Zanele Muholi | 35 | 0 | 0 | Identity unresolved |
| Gamini Wijesuriya | 48 | 0 | 0 | Identity unresolved |

The confirmed boundary defect was between seed-context ingestion and `seed_identity_anchors`, followed by `eligible_identity_source_ids`. PDL's `alumni_organisation` was available for discovery, but was not classified as an institutional identity anchor. Instead, the differently worded job title could become the only anchor. A legitimate biography with the same person and institution then remained unconfirmed. Additional targeted mismatches were a semicolon-separated pair of employers being treated as one institution and the descriptive suffix “Newspaper” preventing an otherwise equivalent institutional match.

Institutional aliases now participate in identity matching without asserting current employment. Explicit semicolon lists are split into separate institutional anchors; commas and “and” are not guessed apart. Equivalent aliases are deduplicated, so repeating a typed employer as a government or alumni field cannot increase identity strength. A university affiliation can also anchor an employment claim about that same institution. The score thresholds, source-authority requirements and exact-name/subject checks remain unchanged.

| Offline case | Confirming context | Eligible sources, before → after | Selected claims, before → after | Resulting organisation / title |
|---|---|---|---|---|
| Achille Mbembe | University of the Witwatersrand | 0 → 1 | 0 → 2 | University of the Witwatersrand / Research Professor |
| Elahe Mohammadi | Ham-Mihan Newspaper ↔ Ham-Mihan | 0 → 1 | 0 → 2 | Ham-Mihan / Journalist |
| Jonathan Anderson | Christian Dior Couture; JW Anderson | 0 → 1 | 0 → 2 | Christian Dior Couture / Creative Director |
| Zanele Muholi | Inkanyiso | 0 → 1 | 0 → 2 | Inkanyiso / Founder |
| Gamini Wijesuriya | ICCROM | 0 → 1 | 0 → 2 | ICCROM / Special Advisor |

Each fixture supplies two grounded relationship claims from one institutionally matching source. Those claims are eligible because the page independently supplies the matching name and institutional context. The seed itself is not an evidence claim. The fixture claiming the same Achille Mbembe name and generic academic title at an unrelated university remains ineligible. Existing tests also retain rejection of disconnected namesake clusters, unrelated advisor/page evidence, partial-name ambiguity and unsupported claims on mixed-person pages. Existing corrective-current-employer and one-hop corroboration paths remain intact.

## Professional relationship selection

The previous selection path filtered explicitly historical employment before considering defining professional practice, while unflagged early employment could survive. Academic roles could also outrank an independently supported principal practice. The fix identifies professional practice from grounded practice and ownership/founding context, separates secondary academic relationships, and preserves the selected organisation and title from one compatible relationship cluster.

Explicit past-tense context can override an inappropriate “current” flag for selection. It is evaluated in the sentence containing the claim's value, so history elsewhere in a biography does not invalidate a separate current job. Raw claims remain unchanged. Historical and active employment bundles do not merge merely because the title is identical. An unnamed relative's business is ineligible as a primary organisation, and its paired title cannot become corroboration for a different practice. Named organisations such as Family Health International remain eligible.

| Offline case | Organisation before | Organisation after | After temporal status |
|---|---|---|---|
| Renzo Piano | His Father's Construction Company | Renzo Piano Building Workshop | Unspecified |
| Zaha Hadid | Office for Metropolitan Architecture | Zaha Hadid Architects | Historical |
| I. M. Pei | Webb & Knapp | Pei Cobb Freed & Partners | Historical |
| Liu Jiakun | Central Academy of Fine Arts | Jiakun Architects | Unspecified |
| Jacques Herzog | ETH Zurich | Herzog & de Meuron | Unspecified |
| Frank Gehry | Yale University | Gehry Partners | Unspecified |

All six cases retain two eligible sources. After correction each selects two supporting relationship claims. Renzo previously counted three supporting claims; the extra title from the invalid family-business relationship is now retained only as an alternative. This removes inappropriate corroboration.

“Unspecified” means that the fixture does not independently establish present employment; it is not displayed as a confirmed current role. The Zaha Hadid and I. M. Pei fixtures explicitly establish the ended professional relationship. Their selections carry `HISTORICAL_PROFESSIONAL_AFFILIATION`, require review and retain a confidence of 48.43%, below the unchanged 50% threshold. No confidence bonus was added to force acceptance. A separate regression confirms a retired founder does not displace a newer, explicitly current principal employer. Existing current-government, executive, academic and partial public-office regressions continue to pass.

## HTTP 200 extraction failures

All seven logged `OPENROUTER_SCHEMA_ERROR` attempts were `extract_claims` requests using `claims-v6`. Each returned HTTP 200, `JSONDecodeError` and exactly 3,000 completion tokens. One source incurred attempts 0, 1 and 2. This is evidence consistent with reaching the configured response cap; the omitted response bodies and finish reasons prevent proving truncation for every original response. There is no demonstrated basis here for changing the model, schema or token budget.

On a schema failure, a reported `finish_reason=length` or completion-token count reaching the request's cap now produces a non-retryable `OutputTokenLimit` diagnostic. The original usage is still recorded. The same bounded malformed request therefore produces one attempt instead of three in the mock regression. Uncapped malformed output and transient transport/provider failures retain their existing bounded retry behavior. Malformed usage/choice envelopes cannot mask the meaningful schema error. Other sources can still contribute independently eligible claims through the existing partial-failure salvage path.

## Targeted validation

- Identity, seed-context and duplicate-anchor checks: 78 passed.
- Relationship selection, reconciliation, Wikipedia treatment, golden quality and evidence checks after the final coherence fix: 134 passed.
- The earlier focused provider, identity and partial-failure salvage run: 66 passed; only two existing Starlette/httpx deprecation warnings.
- No new dependencies, model changes, threshold reductions or increased provider budgets.

These groups overlap and are not additive. The release report records the final integrated suite and benchmark results. Exact original per-source validity and live after-run outcomes remain unverified because the original raw evidence ledger was unavailable and paid live validation was prohibited.
