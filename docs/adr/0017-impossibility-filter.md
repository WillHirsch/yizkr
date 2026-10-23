# ADR-0017: Reconciliation impossibility filter + containment hand-off

Status: accepted — implemented. Candidate blocking now subtracts pairs
the graph's own referenced facts prove cannot be the same entity, before
any LLM adjudication. A rejected equivalence that is really a containment
becomes a `part_of` proposal for reconcile-places.
Amended by #388 (2026-10-04): rule 2 reads containment by QID alone (in
clustering's mention space, by the id that stands in for one). The label
form, which matched a batch place without a QID against the labels of a
candidate's containers, is gone: places share names, so a new
"Klurnau" may be another Klurnau than the one containing the candidate,
and pruning it would drop a live pair. A place without a QID is never
pruned by containment. Clustering's conflict check reads a member's
containers among the cluster's members the same way. Blocking keys on
whole name forms (ADR-0007, as amended by #388), not name tokens.
Amended by [ADR-0038](0038-place-containment-as-source-claims.md)
(accepted 2026-10-23, #515; §8.2): §2's hand-off is withdrawn. Graph
dedup's adjudicator no longer returns a container from its own knowledge
on a `distinct` verdict, which would be a model asserting geography, and
`reconcile-graph` writes no `-containment.csv`; the `distinct` verdict
stands. A reviewer who knows a containment records it by citing the
source it rests on. Rule 2 stands: a source's claim that one place lies
within another is still a referenced fact that the two are distinct, and
it reads the edges the inconsistency report reads (ADR-0038 §5), a
`part_of` or `location` with a stated-in source that is not deprecated.

## Context

Reconciliation blocks candidate pairs on **shared kind + a shared name
token** (ADR-0007), then an LLM adjudicates each. Blocking never consults
the graph, so it proposes — and the model spends a call rejecting, and the
reviewer skims — pairs a structural fact already rules out:

- `Ursula Löbig` vs `Löbig, Ursula, Wwe.`, blocked on the shared name
  though the first was born in 1911 and the second is listed as a widow
  in 1912: a one-year-old cannot be that widow.
- `Klurnau` (the locality) vs the camp `Lager Klurnau`, blocked on
  "Klurnau" — but a camp *inside* Klurnau is not Klurnau.

Both are noise: high adjudication cost and review burden, zero signal. The
generalisable observation is that **an asserted, irreflexive relation
between two candidates is itself proof they are two entities** — you are
never your own parent or your own container, and you cannot act after you
are dead. Those facts are already in the graph as referenced statements;
blocking just never reads them.

## Decision

### 1. A deterministic `impossible_pair(a, b)` predicate

Applied inside `block_candidates` (new-document × graph) and
`propose_graph_pairs` (graph × graph). It prunes a blocked pair when a
**structural** graph fact makes identity impossible. Two rules, each
**recall-safe** — they fire only on pairs the graph's own referenced
statements contradict, never on pairs that are merely dissimilar:

1. **Irreflexive kinship edge.** One candidate names the other via
   `father`/`mother`/`spouse`/`child`/`sibling`. The roster query now
   exposes these as raw QIDs (child and sibling were not fetched before).
2. **Transitive place containment.** One place is inside the other via the
   `(part_of|location)+` closure — the same closure reconcile-places uses
   (ADR-0009). Read once per pass as an injected oracle (mirroring
   `plan_containment`'s `creates_cycle` seam), so the predicate needs no
   SPARQL and is unit-tested with a lambda. A not-yet-written batch place
   (no QID) is matched by label against the candidate's container labels —
   but never when the two share a label, since eponymous nesting is real
   ("Rekau" the town inside "Rekau" the urban district) and an equal
   label may be the *same* place, not its container. (The label match is
   gone: amended by #388 above.)

Unknowns are never impossible: a missing kind or relation leaves the pair a
live candidate for the human (rules 5 & 6 forbid silently narrowing). The
predicate does zero I/O; the graph facts it reads are themselves referenced
assertions — it reads them, never writes.

### Rejected: a temporal rule

An earlier draft added a third rule — prune when lifespans cannot overlap
(one dies before the other is born, or acts after the other's death). An
adversarial review rejected it as **not recall-safe in this archive**, for
reasons that go to the project's core:

- A date disagreement between two candidates is not evidence of
  distinctness — it is the **healthy coexisting state** the whole store is
  built to hold (ADR-0002). Two sources giving different death years for
  one victim is exactly what reconciliation must *surface* to a human, not
  silently prune.
- Dates carry **precision** — a "decade" is ten years, a "century" a
  hundred — and comparing them as points would narrow a source's vagueness
  (rule 5). A decade-precision death "the 1920s" is fully compatible with a
  birth in 1926; a point comparison would wrongly rule it out.
- Posthumous dates (memorials, reburials) and earlier death *estimates*
  are routine here, so "active after death" is not a safe signal.

So there is no temporal rule. Contradictory dates go to adjudication, where
the model and the reviewer weigh them — never to a silent prune.

### 2. Rejected-containment → `part_of` hand-off

The graph-dedup adjudicator (`propose_graph_pairs`) may now, on a
`distinct` verdict where one **place** is spatially inside the other,
return the container's QID. Those become `a_within_b` rows in the
reconcile-places CSV format (`yizkr reconcile-graph` writes
`<out>-containment.csv`), so `reconcile-places --apply` writes the
referenced `part_of` edge after human review (ADR-0009 — containment is
world knowledge with its own run provenance, never a per-document write,
and never auto-applied). Only pairs where both members are places produce a
row, so a stray `contained_in` on a person or organization pair can never
reach the place-hierarchy writer.

This closes a loop: the graph-dedup pass *discovers* a containment the
graph lacked → a human approves it → the `part_of` edge is written → rule 2
of the impossibility filter then prunes that pair (and its kin) from every
future run. Review noise becomes hierarchy signal.

## Consequences

- Fewer adjudication calls and far fewer `distinct` review rows: the
  containment pair never reaches the model or the CSV once the `part_of`
  edge exists. Recall is unaffected — only structurally-contradicted pairs
  are dropped, and the filter degrades gracefully (no closure / no resolved
  kin ⇒ it simply prunes less, never wrongly).
- The roster query grows five kinship-QID aggregates and child/sibling
  fetches; one extra query per pass builds the containment closure. On a
  family-archive-sized graph this is negligible.
- The ingest path prunes **existing** containment deterministically; a
  containment the graph still lacks surfaces where it is writeable — the
  graph-dedup pass over written items — rather than against a batch entity
  that has no QID yet. Running `reconcile-graph` then `reconcile-places`
  over the graph formalises those, after which ingest blocking prunes them.
- The impossibility rules are structural facts, not heuristics, so they
  compose with (do not replace) the string blocking key, which stays
  deliberately loose to preserve recall on clipped/maiden/nickname forms.
  (Amended by #388: the key is a whole name form, read through the
  mention record; maiden and nickname forms reach it through the
  record's typed parts and the graph's typed name statements, clipped
  forms only through the aliases a document adds.)
