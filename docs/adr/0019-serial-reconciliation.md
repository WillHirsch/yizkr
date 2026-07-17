# ADR-0019: Serial, sequenced artifact reconciliation

Status: **proposed** — rulings recorded below; residual forks marked ★. No
code yet.

## Context

Folder ingestion (`yizkr ingest run`, ADR-0007 / the orchestrator) advances
every document to the first review gate in one pass, so **no document is
written when its siblings reconcile**. Each artifact therefore reconciles
only against the graph as it stood *before the run*, never against the other
artifacts in the same folder. Cross-artifact identity is left to a separate
`reconcile-graph` pass run afterwards.

That shape spends human review twice on the same fact. A person named two
ways across two artifacts ("Donald" in a letter, "Donald Bennett Hirsch" in
the tree) is reviewed once per document as a *new* entity — because the
sibling was invisible — then **again** at `reconcile-graph` as "merge these
two", after a duplicate item has already been created (merges are also
messier than never duplicating). For a cold folder ingest into a near-empty
graph this doubles the identity-review burden and guarantees a large
`reconcile-graph` backlog.

The minimum-review shape is the opposite: resolve each artifact against
*everything already accepted*, so a cross-artifact match surfaces once, as
an ordinary identity-gate row on the later artifact, at the moment the
duplicate would otherwise be born.

A note on what ordering can and cannot do. The set of distinct people is
fixed; each is minted exactly once — by whichever artifact mentions it
first — and every other mention is a match. **Reordering the artifacts does
not change the number of identity decisions.** What it changes is their
*difficulty*: reconcile weighs relational evidence over spelling, so a match
against an entity already rich with dates/parents/spouse is a
high-confidence, skim-approve proposal, whereas a match against a bare name
is a low-confidence "uncertain" that demands investigation — and it decides
which source defines each entity's canonical record. So sequencing is a
confidence/quality lever, not a count lever, and it is only worth anything
*with* serial processing (which makes prior artifacts visible at all).

## Decision

Add an opt-in `--serial` ingestion mode. The current advance-all behaviour
stays the default and is unchanged.

### 1. Greedy-overlap sequencing (offline, after extraction)

`--serial` first extracts every artifact (unchanged, still parallelisable),
then computes a processing order from the entity manifests — no model calls:

- For each artifact, build the set of **normalised name keys** of its
  entities (lower-cased label/alias tokens — the same signal blocking uses).
- **Greedy order:** repeatedly place the still-unplaced artifact whose key
  set overlaps the *union of the remaining unplaced* artifacts' key sets the
  most — the most connective "hub" — seeding the graph with the most
  reusable entities first. Break ties by larger entity set (denser first);
  a `.ged` tree naturally ranks at or near the front, which is correct.
- The order is **persisted in run state** so re-invocations resume the same
  sequence; a newly-succeeding extraction (a document that failed earlier)
  triggers a re-sequence of what remains.

This is a set-cover-flavoured heuristic; greedy is cheap (O(n²) over
folder-sized batches) and, crucially, **safe: it only affects order, never
what is written.** A poor ranking costs a little review ease, never
correctness — the real matching is still the per-artifact reconcile with its
human gate.

### 2. Serial processing against the accumulated graph

Artifacts are taken **one at a time, in sequence, each fully before the next
reconciles**, so every artifact resolves against a graph that already holds
its predecessors. The cross-artifact match is then an ordinary identity-gate
row on the later artifact — decided once, in context. This composes with the
impossibility filter and containment closure (ADR-0017), which are richer
the more the graph holds, so pruning improves as the run proceeds.

### 3. Batch-review affordance: decouple the two gates

Only the **identity** gate carries the cross-artifact dependency (reconcile
needs prior entities to exist); **claim** correctness does not. Serial mode
exploits this to keep a batch-review convenience:

- The **identity gate stays per-artifact and in sequence.** On approval, the
  artifact's entities are materialised as an **identity skeleton** — the
  item, its label/aliases, and `instance of`, referenced to the artifact —
  so the next artifact reconciles against them. Identity is settled at this
  gate, which is exactly what the skeleton records.
- The **claim gate is collected and reviewed once, at the end of the run**,
  across all artifacts. A final write pass attaches the approved,
  source-referenced claims to the already-materialised items.

This restores "review all the facts together" (one mode-switch instead of
alternating identity/claims per artifact) without weakening the serial
identity resolution. It requires a **two-phase write** (identity skeleton at
the identity gate, claims at the batched claim gate) — the notable
implementation cost, and the ★ fork below. A bare skeleton whose every claim
is later rejected is an acceptable stub: the source did mention the entity,
and its `instance of`/label carry the artifact reference.

Nothing here weakens R3/ADR-0007: identity is still human-confirmed at the
per-artifact gate, no claim is written without an explicit `approve`, and
the skeleton is only ever written *after* its identity gate is approved.

### 4. `reconcile-graph` retained, demoted

`reconcile-graph` is **kept as an explicit cleanup command**, not deleted —
it still catches pre-existing duplicates, duplicates across *separate* runs,
and pairs a per-artifact adjudicator judged distinct on thin evidence that
later accumulates. But under `--serial` it is **no longer a required
post-folder step**; the routine cross-artifact case is handled inline. (It
also remains the only backstop for *within*-artifact duplicates the
extraction manifest failed to unify — serial mode is a cross-artifact fix,
not a within-artifact one.)

## Rulings

1. **Opt-in.** `--serial` is a flag; the existing advance-all run stays the
   default and behaves exactly as today. (Whether serial later becomes the
   default is deferred — ship it opt-in and revisit with usage. ★)
2. **Batch affordance included.** The claim gate is batched at end-of-run via
   the gate decoupling above, since it is compatible with serial identity
   resolution and directly serves the minimise-review goal.
3. **Greedy-overlap sequencing**, offline, from the extraction manifests,
   order persisted for resumable runs.
4. **Docs in scope.** `pipeline/README.md` is reworked as part of this
   change (see Consequences) — including the `reconcile-graph` role it
   currently omits.

★ **Residual forks**

- **Batched-claims write mechanism.** Two-phase write — identity skeleton at
  the identity gate, claims at the batched claim gate *(recommended:
  Wikibase-native, no provisional-identity bookkeeping)* — versus an
  in-memory "pending roster" that defers all writes to the end and resolves
  cross-artifact provisional matches at write time (avoids two-phase write
  but must track un-written entity identity across the run).
- **Strict-serial fallback.** If the two-phase write proves too invasive to
  land at once, ship `--serial` first as *strict* (both gates inline per
  artifact, no claim batching) and add the batched claim gate second. The
  serial identity resolution — the main win — stands either way.

## Consequences

- Cross-artifact identity is resolved inline, once, in context; the
  mint-then-merge double review and the routine post-folder `reconcile-graph`
  backlog are eliminated for `--serial` runs. `reconcile-graph` survives as
  an occasional cleanup tool.
- Human review of a `--serial` run is **sequential for identity** (finish an
  artifact's identity gate before the next reconciles) and **batched for
  claims** (one gate at the end). This is a deliberate rhythm change from
  advance-all's "all identity gates, then all claim gates".
- No extra model cost — the same number of reconcile calls, reordered; the
  sequencer is offline.
- **Documentation is part of this change, not a follow-up.**
  `pipeline/README.md` today documents the per-document steps but does **not**
  explain `reconcile-graph` at all — so a reader cannot tell how entities
  from different artifacts get resolved, which is the exact confusion that
  motivated this ADR. The implementing PR must rework that document to: (a)
  describe `reconcile-graph`'s role in the *current* default mode (the
  separate cross-artifact dedup pass) so today's behaviour is finally
  complete; and (b) add `--serial` — the sequencer, the per-artifact identity
  gate, the batched claim gate, and `reconcile-graph`'s demotion to cleanup —
  as a first-class alternative to the default loop, with the mermaid diagram
  extended or paired to show both.
- The advance-all default is untouched, so nothing regresses for existing
  runs or tests; `--serial` adds paths, it does not alter the old one.
