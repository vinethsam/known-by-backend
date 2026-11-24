# KnownBy extraction and offline evaluation

Operational research uses the source model for discovery and source assessment and the extraction model for claim extraction. This is the pipeline at `6b3984601f81aa93ef2820e13f61c1878e7bf704`, including its existing structured metadata preprocessing. The API and worker have no mode for running the Phase 4 seven-field rules engine. The database uses the existing migration chain.

The [seven-field rules engine](../app/research/deterministic/) is retained for offline evaluation. Run the [Stage B synthetic comparison](phase-4-stage-b.md#reproduction-and-verification) or evaluate approved local evidence from the backend directory.

```powershell
python -m benchmarks.stage_c_shadow --evidence '<private-local-path>\approved-evidence.json' --output '<private-local-path>\evidence-review.json'
```

The evidence manifest requires `version: 1`, `evidence_kind: "approved_retained_webpage"`, `as_of` and one to five `cases`. Each case includes an `id`, the original `seed`, `sources`, reviewed `expected` labels and a `label_basis`. Each source includes its ID, URL, source type and retained HTML, Markdown or structured JSON. Supply all seven reviewed fields and the qualification and employment relationships. Use `null` for unreviewed labels and an empty list when review confirms no qualifying evidence. A recorded Model 2 comparison requires original page evidence and the parsed responses tied to matching residual chunk hashes. Save evidence and reports in a private local directory outside Git.

The [evidence inventory](phase-4-stage-a.md#3-evidence-inventory) found no approved source captures or matched Model 2 replies for the real-world benchmark. The offline synthetic results remain the measured reference for the rules engine.
