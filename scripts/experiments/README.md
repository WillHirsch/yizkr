# Experiments

One-off measurement scripts that calibrate a decision before it is built. They are not part of the `yizkr` package or CLI; code that proves useful moves into `src/yizkr` under its own issue.

Rules:

- **No private data in the repo.** Private input paths are explicit arguments; a fictional-only measurement reads only committed fixtures. Outputs go to a directory outside the repository (the scripts refuse a path inside it). Detail files that carry private names stay private (directories 700, files 600).
- **Aggregates only leave the machine.** A script's `report.md` / `summary.json` holds counts, kinds and examples as `<artifact SHA-1 prefix>:<local id>`. GEDCOM entities use their position in the adapted manifest (`<prefix>:ged<n>`) instead, because their ids derive from the export's xrefs.
- **Offline.** No LLM calls, no graph access.

## `conversation_reuse.py`

Runs the production chunked extractor and response cache with authored answers from the fictional postcard and family-history fixtures. Compares cold, fully cached and partially cached extraction using restorable conversations, replay-only conversations and one-shot calls carrying their context. The partial cache holds the opening answer and first chunk; subsequent chunks are missing. The explicit chunk budget is 512, so these small readings exercise multiple chunks without inventing additional source material.

```sh
uv run python scripts/experiments/conversation_reuse.py --out /tmp/conversation-reuse
```

The output directory must be new. `summary.json` and `report.md` give dispatched operation counts, submitted prompt bytes, reconstructed logical context bytes, persisted session sizes and actual bytes/timing for materializing the authored provider's restored bundle. Temporary caches are deleted. No archive paths, provider credentials, external calls or graph are used.

These measurements test reuse mechanics with fixed answers. They do not establish model quality, live-provider equivalence, billed tokens, provider prompt-cache hits or corpus frequencies. UTF-8 bytes are not tokens; a fork's submitted prompt omits its inherited context. Restore-copy timings measure small local fixture files without fsync, not Claude restoration latency. Existing reuse optimizations remain until suitable evidence supports a change.

## `cluster_calibration.py` (#71)

Calibrates corpus-level identity (ADR-0027): candidate-pair blocking, cannot-link constraints, transitive closure and the review load, over a cohort's extraction manifests plus the GEDCOM export. It reuses `reconcile.py` (`block_candidates`, `impossible_pair`, `entity_facts`, `_batch_candidates`) and the GEDCOM adapter.

```sh
uv run python scripts/experiments/cluster_calibration.py \
    --runs <run dirs with batch.json [+ state.json]> \
    --gedcom <export.ged> \
    --verdict-runs <run dirs with batch.json + equivalences.csv> \
    --out <private output dir>
```

`--calls-per-window` sets the assumed adjudication calls per subscription usage window (default 50) and `--pairs-per-call` the pairs per adjudication call (default 40, as `reconcile.py`). The module docstring defines the strategies, constraints and the deterministic proxy used to size review.
