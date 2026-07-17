# ADR-0017: Reconciliation impossibility filter + containment hand-off

Status: accepted — implemented. Candidate blocking now subtracts pairs
the graph's own referenced facts prove cannot be the same entity, before
any LLM adjudication. A rejected equivalence that is really a containment
becomes a `part_of` proposal for reconcile-places.

## Context

Reconciliation blocks candidate pairs on **shared kind + a shared name
token** (ADR-0007), then an LLM adjudicates each. Blocking never consults
the graph, so it proposes — and the model spends a call rejecting, and the
reviewer skims — pairs a structural fact already rules out:

- `Samuel Wolff` vs `Alexander Samuel Genserowsky`, blocked on the middle
  name "Samuel" though the surnames are disjoint and Genserowsky died in
  1938 while Wolff was a Theresienstadt returnee in 1945.
- `Poland` (the country) vs a Chełmno extermination camp whose label ends
  "…, Poland", blocked on "Poland" — but a camp *inside* Poland is not
  Poland.

Both are noise: high adjudication cost and review burden, zero signal. The
generalisable observation is that **an asserted, irreflexive relation
between two candidates is itself proof they are two entities** — you are
never your own parent or your own container, and you cannot act after you
are dead. Those facts are already in the graph as referenced statements;
blocking just never reads them.

## Decision

### 1. A deterministic `impossible_pair(a, b)` predicate

Applied inside `block_candidates` (new-document × graph) and
`propose_graph_pairs` (graph × graph). It prunes a blocked pair when a hard
graph fact makes identity impossible. Three rules, each **recall-safe** —
they fire only on pairs the graph's own referenced statements contradict,
never on pairs that are merely dissimilar:

1. **Irreflexive kinship edge.** One candidate names the other via
   `father`/`mother`/`spouse`/`child`/`sibling`. The roster query now
   exposes these as raw QIDs (child and sibling were not fetched before).
2. **Transitive place containment.** One place is inside the other via the
   `(part_of|location)+` closure — the same closure reconcile-places uses
   (ADR-0009). Read once per pass as an injected oracle (mirroring
   `plan_containment`'s `creates_cycle` seam), so the predicate needs no
   SPARQL and is unit-tested with a lambda. A not-yet-written batch place
   (no QID) is matched by label against the candidate's container labels.
3. **Temporal impossibility.** One dies before the other is born
   (disjoint lifespan), or one is active in a *living* act (any dated
   non-death event) after the other's death. A one-year tolerance protects
   coarse dates — uncertainty is never narrowed into a prune (rule 5).

Unknowns are never impossible: a missing date, kind, or relation leaves the
pair a live candidate for the human (rules 5 & 6 forbid silently
narrowing). The predicate does zero I/O; the graph facts it reads are
themselves referenced assertions — it reads them, never writes.

### 2. Rejected-containment → `part_of` hand-off

The graph-dedup adjudicator (`propose_graph_pairs`) may now, on a
`distinct` verdict where one item is spatially inside the other, return the
container's QID. Those become `a_within_b` rows in the reconcile-places CSV
format (`yizkr reconcile-graph` writes `<out>-containment.csv`), so
`reconcile-places --apply` writes the referenced `part_of` edge after human
review (ADR-0009 — containment is world knowledge with its own run
provenance, never a per-document write, and never auto-applied).

This closes a loop: the graph-dedup pass *discovers* a containment the
graph lacked → a human approves it → the `part_of` edge is written → rule 2
of the impossibility filter then prunes that pair (and its kin) from every
future run. Review noise becomes hierarchy signal.

## Consequences

- Fewer adjudication calls and far fewer `distinct` review rows: the two
  motivating pairs never reach the model or the CSV once the containing
  facts exist. Recall is unaffected — only graph-contradicted pairs are
  dropped, and the filter degrades gracefully (no closure / no batch dates
  ⇒ it simply prunes less, never wrongly).
- The roster query grows five kinship-QID aggregates, child/sibling
  fetches, and a last-activity max; one extra query per pass builds the
  containment closure. On a family-archive-sized graph this is negligible.
- The ingest path prunes **existing** containment deterministically; a
  containment the graph still lacks surfaces where it is writeable — the
  graph-dedup pass over written items — rather than against a batch entity
  that has no QID yet. Running `reconcile-graph` then `reconcile-places`
  over the graph formalises those, after which ingest blocking prunes them.
- The impossibility rules are structural facts, not heuristics, so they
  compose with (do not replace) the string blocking key, which stays
  deliberately loose to preserve recall on clipped/maiden/nickname forms.
