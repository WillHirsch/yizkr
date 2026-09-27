# ADR-0009: Place hierarchy — demand-driven containment with transitive elimination

Status: accepted

## Context

Sources name the same real-world place at different resolutions
("Gerichtsgefängnis Peiltal" / "Peiltal"; "Brunnenring 35" /
"Flörburg") and under different naming conventions ("Fläckfeld,
Kreis Mockburg, Hesse, Germany" / "Fläckfeld, Kreis Peiltal"). Without
recorded containment, the inconsistency report (ADR-0002's flagship view)
presents differences of resolution as if they were contradictions,
and the places map cannot group anything.

The report already suppresses value pairs connected by a
`(location|part_of)+` path — deterministically, from the graph's own
knowledge. What is missing is the pass that PROPOSES those edges.

A naive place×place adjudication is O(n²) in questions and — the
scarcer resource — in curator review attention. Two observations
(curator design input) restructure it:

1. the pairs that matter are exactly the ones that CO-OCCUR as
   competing values of the same fact — the report's own rows;
2. front-loading the largest-scale containment lets transitive
   closure eliminate downstream questions wholesale: once
   Brunnenring 35 ⊂ Flörburg is approved, every question answerable
   through that edge disappears from the next round's worklist.

## Decision

`yizkr reconcile-places` runs an iterative, converging loop; every
edge it writes is human-approved and contestable.

### Worklist derivation (deterministic, demand-driven)

One SPARQL round-trip yields, in order of preference:

1. **competing pairs**: place-valued value pairs of the same
   entity+property that the inconsistency report still shows (its own
   containment filters applied) — answering these directly sharpens
   the report;
2. **comma-parent candidates**: place labels are usually hierarchy
   chains ("Fläckfeld, Kreis Mockburg, Hesse, Germany"); a place whose
   comma-suffix matches another place's label or alias gets that
   place as a deterministic parent candidate. Structure never rides
   the generative path (ADR-0006): these arrive pre-evidenced, the
   adjudicator (and reviewer) merely confirms.

Questions are ordered spine-first: fewer commas ≈ larger scale, so
container-side questions come before the places that attach to them,
and each apply round re-derives the worklist through the closure —
transitive elimination between rounds. Convergence is typically two
to three short rounds, each cheaper than the last.

### Adjudication and verdicts

The ADR-0007 machinery (batched calls ≤40 pairs, NDJSON + sentinel,
rulings restricted to given pairs) with an extended verdict
vocabulary, because a competing-value pair poses a three-way
question:

    same        naming variants of one place -> routes to the
                EXISTING identity flow (curator merge or a referenced
                `said to be the same as`); never applied by this pass
    a_within_b  containment -> `part_of` proposal
    b_within_a  containment -> `part_of` proposal
    distinct    a genuine contradiction; stays on the report
    uncertain   reviewer's judgement

### Review and application

Proposals land in a review CSV (decision column, like every gate).
`--apply` writes ONLY approved containment rows, as `part_of`
statements with a **cycle guard** (nothing may contain its own
ancestor — checked against the live closure and within the batch)
and idempotent skips. `same` rows are listed for the curator and
never executed (ADR-0007: identity is curator-executed).

**Provenance.** Geographic containment is world knowledge, not a
source assertion, so ADR-0003's `stated in → artifact` first
reference does not apply as-is; this ADR extends the convention:
containment statements carry `inferred from extraction run` → the
reconcile-places run item (which records method and pipeline
version). When a document's own hierarchical label evidences the
edge (comma-parse), the proposal's evidence records it; when
GeoNames supplies the edge (below), a `stated in` → GeoNames dataset
item is added. The unreferenced-statements QC treats any reference
as satisfying; these statements are never bare.

### GeoNames (designed; needs an account to activate)

GeoNames reconciliation supplies external IDs, coordinates (lighting
up the site's map page), and — decisively — its admin hierarchy,
which answers most spine questions with no adjudication at all. The
web services require a registered username, so this half activates
when the curator provides `GEONAMES_USERNAME`; the LLM then only
handles the residue: historical places, address-level fragments, and
ambiguous names. Imported facts are machine claims referenced to a
GeoNames dataset item plus the run.

## Consequences

- Report rows about resolution differences resolve themselves as
  containment lands; genuinely contradictory rows are what remains —
  the report sharpens monotonically.
- Curator attention scales with the number of EDGES worth recording,
  not the number of place pairs.
- Transitive elimination amplifies an early wrong edge (it silently
  suppresses questions and report rows). Mitigations: edges are
  ordinary contestable statements with run provenance; spine edges
  are few, large-scale, and conspicuous; the cycle guard catches the
  pathological case; and the static site renders the containment
  tree on its places page — a visible hierarchy is a self-auditing
  one.
- One more occasional pass (like `reconcile-graph`), one more review
  CSV contract.
