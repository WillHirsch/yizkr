# ADR-0009: Place hierarchy — demand-driven containment with transitive elimination

Status: accepted
Superseded in part by [ADR-0038](0038-place-containment-as-source-claims.md)
(proposed, 2026-10-16, #515): its "Provenance" section and decision FA
of #201's amendment (PR #513), with what follows from them (ADR-0038 §9);
this ADR stands as written until the owner accepts ADR-0038.
Amended by [ADR-0031](0031-the-relation-decides-the-kind.md) (#202,
2026-09-29): a camp, prison or other site that a source names as where
something happened is a place, inside its town or region, so it joins
this hierarchy; an organisation stays outside it, linked to a place by
`location` (ADR-0012, Fork F).
Amended by #388 (2026-10-04; the link-by-link chain decided by the
orchestrator 2026-10-05, the owner may override): the comma-parent
candidates are gone. A label's commas never say what contains what
(ADR-0035 §1). A place's parent candidates come from the chain of
containers each of its mention records states (`within`, nearest first,
ADR-0035 §2.2), read from the run directories that wrote the graph
(`reconcile-places --run DIR`). Each written place reaches its QID
through its batch: the QID reconcile resolved, else the item the writer
used for its local id, found under its label or minted (`write.json`'s
`created_items`). A chain is asked link by link: the place within the
nearest of its containers that names an existing place, and each such
container within the next, so an approved chain writes no shortcut edge
of its own ("Fläckfeld part_of Germany" beside "Fläckfeld part_of Kreis
Mockburg" from one record's "Fläckfeld, Kreis Mockburg, Germany"), and a
nearest container that states no parent of its own is still placed.
Records that stop at different depths can still propose one: a second
record's "Fläckfeld, Germany" asks Fläckfeld within Germany beside the
first's links. Each record's statement is asked as made, with no
reduction across chains (decided by the orchestrator 2026-10-05, the
owner may override). Likewise a container missing from the graph is
passed over, so its neighbours are asked of each other ("Kreis Mockburg"
within "Germany" where no Hesse exists); once the middle container is
minted, a later round asks the links through it, and the earlier edge
becomes a shortcut, since the check against the graph's containment
never removes an edge. A container names every place whose label or alias is
that name, folded as ADR-0035 §7 folds a part; namesakes are each
asked, and a pair the graph already nests either way is not. Questions
are ordered spine-first by the graph's own containment: a question
whose places hold more places comes first, then one whose places sit
less deep.
Amended by #465 (2026-10-05): extraction gives a place's `part_of` (its
country among them) only where the source states it: in the place's
mention, in the fields a form gives that place, or in a chain of
containers, which states each link ("Snylsjum, gemeente Drakeradiel,
Fryslân" also puts gemeente Drakeradiel in Fryslân). It is never
inferred from what is known of the place or carried over from another
place the document names; the critic flags a `part_of` its excerpt does
not state (`unsupported_containment`, ADR-0023). Everything else is this
pass's to propose.

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
2. **comma-parent candidates** (replaced by stated-container
   candidates, amended by #388 above): place labels are usually hierarchy
   chains ("Fläckfeld, Kreis Mockburg, Hesse, Germany"); a place whose
   comma-suffix matches another place's label or alias gets that
   place as a deterministic parent candidate. Structure never rides
   the generative path (ADR-0006): these arrive pre-evidenced, the
   adjudicator (and reviewer) merely confirms.

Questions are ordered spine-first (amended by #388: a question whose
places the graph puts more places inside comes first), so
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
edge (comma-parse; a record's stated container since #388, above), the
proposal's evidence records it; when
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
GeoNames dataset item plus the run. Tracked in #164.

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
