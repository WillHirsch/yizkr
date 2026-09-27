# ADR-0019: Serial, sequenced artifact reconciliation

Status: **accepted** — implemented (`ingest run --serial`, the greedy
sequencer, `--extract-concurrency` for the parallel extract-all phase, and
the `pipeline/README.md` rework). Strict serial, two gates per artifact,
stateless re-run advances. Supersedes an earlier draft that batched the
claim gate — see §3 for why that and a combined single gate were both
rejected.

## Context

Folder ingestion (`yizkr ingest run`, ADR-0007 / the orchestrator) advances
every document to the first review gate in one pass, so **no document is
written when its siblings reconcile**. Each artifact therefore reconciles
only against the graph as it stood *before the run*, never against the other
artifacts in the same folder. Cross-artifact identity is left to a separate
`reconcile-graph` pass run afterwards.

That shape spends human review twice on the same fact. A person named two
ways across two artifacts ("Gustl" in a letter, "Gustav Löbig" in
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

### 3. The existing two gates per artifact, unchanged

Each artifact keeps both review gates in their existing order — identity
(`equivalences`), then, after `resolve`, claims (`review`) — and is written
in full before the next artifact reconciles.

Two rejected alternatives, and why each fails:

- **Batching the claim gate to end-of-run** (an earlier draft) would defer
  claim writes, so during the identity phase every prior entity is a *thin
  skeleton* — name and kind only, no dates or relations. Later artifacts
  would then match against bare names, which is exactly the low-confidence
  case the sequencing is meant to avoid: reconcile weighs relational
  evidence, and claims can only be written after review (R3), so rich
  context requires per-artifact claim review. Batching cancels the
  sequencing benefit and adds a two-phase write for it.
- **Combining the two gates into one review per artifact** would present
  claims *before* `resolve` runs, so claims would be reviewed on the
  un-collapsed set (identity resolution collapses byte-identical claims from
  two locals mapping to one item, `apply_equivalences`) and against
  still-provisional entities. Identity-first exists precisely so claim
  review lands on the leanest, settled set; combining inflates the human's
  claim workload. Rejected.

So serial mode is *only* a change to the run **order** — the per-artifact
pipeline (reconcile → identity gate → resolve → export-review → claim gate →
write) is untouched. Both gates stay human-confirmed; nothing about R3 /
ADR-0007 changes.

### 4. Stateless re-run stays the advance mechanism

`--serial` keeps the pipeline's stateless, file-based shape: each
`ingest run` re-derives what to do from the on-disk state and the live
graph, advances the sequence as far as the gates allow, and stops. The
human saves a reviewed CSV and **re-runs to advance** — so a serial run
takes roughly two re-invocations per artifact (identity gate, then claim
gate), versus the advance-all default's ~3 total.

This is deliberate, not a limitation to engineer away. The stateless core
gives correctness-by-reconstruction (no long-lived process state to drift),
full auditability (every intermediate is an inspectable/correctable file),
and crash-safe resumption. Because each run re-derives status from the
durable artifacts, a transient downstream failure (e.g. the graph
unreachable at write time) leaves a `failed` marker that the next re-run
clears and advances past — a valid cached batch means extraction
succeeded — so a fixed environment self-heals without manual state
surgery. The re-run frequency is a *surface* concern, and
the right place to remove it is a thin stateful layer **on top** — the
planned human frontend, or a future optional `--watch`/`--loop` mode that
re-invokes when a reviewed CSV appears — driving the stateless core rather
than replacing it. That layer is out of scope here; this ADR only requires
that the docs state plainly that advancing the process currently means
re-running `ingest run`.

### 5. `reconcile-graph` retained, demoted

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
   default is deferred — ship it opt-in and revisit with usage.)
2. **Two gates per artifact, unchanged; not batched, not combined.** Claim
   review must land on the resolved, minimal set (identity-first keeps it
   lean), and each artifact must be fully written for the next to see rich
   context — so both gates stay, in order, per artifact. §3 records why
   batching and combining were rejected.
3. **Greedy-overlap sequencing**, offline, from the extraction manifests,
   order persisted for resumable runs.
4. **Stateless re-run stays the advance mechanism** (§4); the frontend or a
   future `--watch` mode is the convenience layer, out of scope here.
5. **Docs in scope.** `pipeline/README.md` is reworked as part of this
   change (see Consequences) — including the `reconcile-graph` role it
   currently omits, and a plain statement that advancing = re-run.

## Consequences

- Cross-artifact identity is resolved inline, once, in context; the
  mint-then-merge double review and the routine post-folder `reconcile-graph`
  backlog are eliminated for `--serial` runs. `reconcile-graph` survives as
  an occasional cleanup tool.
- A `--serial` run takes roughly **two re-invocations per artifact** (identity
  gate, then claim gate) versus the advance-all default's ~3 total — accepted
  as a surface cost the frontend/`--watch` layer removes, not a reason to
  make the core stateful (§4). Review *decisions*, the thing being minimised,
  go down (no duplicate cross-artifact review); re-*runs* go up.
- No extra model cost — the same number of reconcile calls, reordered; the
  sequencer is offline.
- Implementation is small: the per-artifact pipeline is untouched; only the
  run-loop ordering (extract-all → sequence → process-in-order, stopping at
  the first unwritten artifact) and the offline sequencer are new. No
  two-phase write, no combined gate.
- **Documentation is part of this change, not a follow-up.**
  `pipeline/README.md` today documents the per-document steps but does **not**
  explain `reconcile-graph` at all — so a reader cannot tell how entities
  from different artifacts get resolved, which is the exact confusion that
  motivated this ADR. The implementing PR must rework that document to: (a)
  describe `reconcile-graph`'s role in the *current* default mode (the
  separate cross-artifact dedup pass) so today's behaviour is finally
  complete; and (b) add `--serial` — the sequencer, the per-artifact two-gate
  sequence, advancing-by-re-run, and `reconcile-graph`'s demotion to cleanup
  — as a first-class alternative to the default loop, with the mermaid
  diagram extended or paired to show both.
- The advance-all default is untouched, so nothing regresses for existing
  runs or tests; `--serial` adds paths, it does not alter the old one.
