# Experiments

One-off measurement scripts that calibrate a decision before it is built. They are not part of the `yizkr` package or CLI; code that proves useful moves into `src/yizkr` under its own issue.

Rules:

- **No data in the repo.** Every input path is an argument, and outputs go to a directory outside the repository (the scripts refuse a path inside it). Detail files that carry names stay private (directories 700, files 600).
- **Aggregates only leave the machine.** A script's `report.md` / `summary.json` holds counts, kinds and examples as `<artifact SHA-1 prefix>:<local id>`. GEDCOM entities use their position in the adapted manifest (`<prefix>:ged<n>`) instead, because their ids derive from the export's xrefs.
- **Offline.** No LLM calls, no graph access.

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
