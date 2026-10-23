# ADR-0009: Place hierarchy — demand-driven containment with transitive elimination

Status: accepted
Superseded in part by [ADR-0038](0038-place-containment-as-source-claims.md)
(accepted by the owner 2026-10-23, #515): its "Provenance" section and
decision FA of #201's amendment (PR #513), with what follows from them
(ADR-0038 §9). A containment is a source's claim, and no model answers
whether one place lies within another; the rest stands.
Amended by #201 (2026-10-23; ADR-0038 §8.5 to §8.7, as applied): the
pass asks one question, of identity: which existing place a container of
a chain a mention record states means. The worklist takes the links the
records state, link by link (the place within its nearest container,
each container within the next), and asks of a link whose container
names more than one existing place, or that no claim of its document
gives (a link the record alone states); a link a claim gives, whose
container names one place, is not asked, as the write wrote it from the
claim, and a link whose container names no place is left out and
counted. The answer names a numbered candidate, or none (`uncertain`),
which writes nothing. Competing pairs and the verdicts `same`,
`a_within_b`, `b_within_a` and `distinct` are gone (the "Worklist
derivation" item 1 and "Adjudication and verdicts" below are
superseded). An answered link is written `part of` the item the answer
names, stated in the stating document's artifact (no page: a record has
no locator; since #593 with the container's evidence and its page:
ADR-0038 §8.5) with the time its claim gives and none where the record
alone states it, and inferred from the pass's run item; a link written
already, stated in the same artifact with the same time, is not written
again, and an equal statement another source states gains the link's
references (the "Provenance" paragraph below is superseded). The cycle
guard stands, at any time, and a refused cycle is counted and named in
the local run report. A scratch run asks and writes unattended in its
`places` stage, after the write (reset → register → write → places →
site; ADR-0025 §1), with decision FB (a complete write only) and FC (a
link with no place to name is left out and counted). The prompts name no
QID: each place goes by its label and description, the labels of the
places the graph puts it in and the chains its records state; places
whose names still coincide are numbered by the documents whose records
name them, else in the order given (decision FD), so a scratch run after
a reset asks the same prompts. Between rounds the graph's containment
answers no question: a candidate the graph already puts the place in may
still be a namesake the source does not mean, so each round asks again,
and the idempotent skip keeps a link from being written twice
(transitive elimination, below, no longer removes questions).
`reconcile-places` asks the same questions for a curator, with the same
CSV columns (A the place whose record states the chain, B the candidate
the answer names, the verdict `same` or `uncertain`, plus the chain and
the container's number in it, from 1), and its `--apply` writes only the
links the approved rows decide, cited as above.
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

1. **competing pairs** (superseded by ADR-0038 §4 and #201, above):
   place-valued value pairs of the same
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
transitive elimination between rounds (since #201, above, the closure
removes no identity question). Convergence is typically two
to three short rounds, each cheaper than the last.

### Adjudication and verdicts

(Superseded by ADR-0038 §3 and #201, above: the answer names a
candidate, or none.) The ADR-0007 machinery (batched calls ≤40 pairs,
NDJSON + sentinel, rulings restricted to given pairs) with an extended
verdict vocabulary, because a competing-value pair poses a three-way
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

**Provenance** (superseded by ADR-0038 §1 and #201, above). Geographic
containment is world knowledge, not a
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
