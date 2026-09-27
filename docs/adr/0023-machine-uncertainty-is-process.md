# ADR-0023: Machine uncertainty is process, not graph data — a general four-axis reappraisal mechanism

Status: **accepted** — design settled through an adversarial design panel (three
approaches, each critiqued, synthesised); implemented: a `Finding.axis` field,
a review-CSV column, an `accept-all` disposition + writer edit summary, and the
`under-witnessed-statements.rq` query (live validation of the query pending,
#23). Renumbered from local ADR-0022 when reconciling with origin (#18). **Amends ADR-0022** (its §6 per-name uncertainty
treatment is *generalised*, not overturned) and **ADR-0013** (the extraction
critic's `Finding` gains an `axis`). Sibling to **ADR-0020** ("don't store a
derived inference"). Cites ADR-0002 (coexistence), ADR-0003 (dual provenance),
and MODELING.md (a human decision's process is the MediaWiki edit history).

## Context

A reviewer needs to find and re-appraise the machine's most contentious
decisions — above all the ones that entered with little human scrutiny
(auto-/bulk-accepted through `accept-all`). ADR-0022 §6 proposed surfacing this as
a per-*name* uncertainty self-report across bespoke axes (spelling / type /
composition / identity). That was rightly challenged as **hand-crafted treatment
of one corner of the schema**: those axes are nothing but the universal parts of
*any* statement — **subject / predicate / value / existence** — so a name-specific
vector is redundant. The real question became whether there is a **fully
generalised** representation of equal-or-greater expressiveness, and whether it
should be **graph-resident** so downstream reappraisal is a SPARQL query.

The current model draws a deliberate line, which this ADR makes explicit and
keeps:

- `Claim.confidence` is a single float and is **never written to the graph**
  (claims.py): ephemeral extractor metadata for review *ordering* only. The graph
  is deliberately machine-confidence-free.
- The graph **does** record the *source's own* uncertainty as **qualifiers**:
  `sourcing_circumstances` (e.g. `circa`), `earliest_date`/`latest_date`, and
  Wikibase time precision. The principle: **source vagueness is data; machine
  doubt is process.**
- Writes are always **normal rank** — the claim contract has no rank field
  (ADR-0002). Rank is an optional curator overlay, and rule 4 forbids any feature
  *requiring* preferred; confidence must never auto-drive rank.
- **Neutral coexistence** (ADR-0002): contradictory statements coexist at normal
  rank as the healthy resting state — but for *cross-source* multiplicity. The
  disagreement report (`disagreement-report.rq`) lists `date_of_birth` (and the
  other single-value properties) in its `VALUES`, so two *same-source* candidate
  readings written as coexisting statements would surface there as a **fabricated
  cross-source contradiction** (rules 5/6).
- **Dual provenance** (ADR-0003): machine writes already carry graph-resident
  *process* metadata — an `inferred from → extraction run` reference. So some
  machine metadata is already in the graph; the question is whether machine
  *epistemics* should join it.
- **Identity** uncertainty already has its own gate (the equivalences CSV:
  verdict/confidence/evidence), separate from claim review.
- The extraction critic (ADR-0013) emits `Finding{target, issue, suggestion}`,
  and its `target` grammar is *already* statement-part-addressable: a bare id
  (`e01`) targets the subject, `e01 alias: X` a value, `claim: e01 occupation` a
  predicate.

## Decision

### 1. The four universal axes each route to an existing home

Uncertainty about any statement decomposes into **subject/identity,
predicate/property, value/reading, existence/interpretation** — the general parts
of a statement, of which the name axes are exact instances (spelling = value,
type = predicate, composition = value-structure, identity = subject). Each routes
to a home that already exists; **no per-attribute confidence vector is added**:

- **subject/identity** → the equivalences reconciliation gate; only the *outcome*
  (a resolved reference, a minted item, or `said_to_be_the_same_as`) is
  graph-resident. Never a per-claim identity flag.
- **predicate/property** → **no faithful graph home** (§4). Handled by the
  lower-commitment layers where they exist (the alias bag for a name's type;
  `unclassified_entity` + `type_hint` for an entity's kind, ADR-0013;
  `gaps.ndjson` for a relation the schema cannot frame, never written) plus a
  single best predicate the critic may flag. Deliberately **not**
  SPARQL-reappraisable.
- **value/reading** → source vagueness is **data** via the existing qualifiers;
  genuine cross-source multiplicity is **coexistence**; a machine's reading-doubt
  of a *clear* source is handled by ephemeral confidence-ordering, a precision
  downgrade, or deferral to a curator — never two same-source coexisting
  statements (§4).
- **existence/interpretation** → the transcription excerpt and the
  statement→reference→region verification affordance; a source that itself hedges
  is source vagueness; a thing the schema cannot frame is the ADR-0013 escape
  hatch / `gaps`.

### 2. Machine epistemics stay out of the graph

**No graph-resident machine confidence, contentiousness, or doubt.** The
SPARQL-reappraisal payoff does **not** justify reversing the standing "confidence
never in the graph" principle, for three convergent reasons:

1. **ADR-0020 precedent.** ADR-0020 just removed a *stored derived inference* (the
   living flag) precisely because a stored derived fact fights source-first,
   freezes a time-varying inference that goes stale, and taxes review for
   something the system can already recover. A machine-confidence scalar is the
   same shape; storing it would overturn a decision the project has now made twice
   (claims.py and ADR-0020).
2. **A stored self-report is the wrong instrument.** An LLM's confidence is
   uncalibrated, non-re-derivable (so unfalsifiable on reappraisal), and — by
   construction — **blind to confident-but-wrong**, the hardest error class and
   the exact one the critic exists to catch. It buys little on the cases that
   matter.
3. **Rank governance (rules 4, 6).** A per-statement confidence invites a frontend
   to "prefer the confident value" — a governance hazard fenced only by
   discipline. Keeping it out honors rule 4 *by construction*.

Process metadata that is *already* graph-resident (the `inferred from → extraction
run` reference) stays; no new epistemic snak, qualifier, or node is added.

**This is a principled loss, not a redundancy.** Machine contentiousness is a
*real* quantity that provenance structure cannot recover — under-corroboration is
a different measure and can invert the true ranking (a crisp single-source reading
looks "under-witnessed"; two mutually-corroborating coin-flip readings look
"witnessed"). We accept not storing it because the only faithful in-graph
realisation — an uncalibrated float — is worse than the loss.

### 3. Generalise the critic (this replaces ADR-0022 §6's per-name framing)

Add `axis: Literal["subject", "predicate", "value", "existence"] | None` to
`critique.Finding`, derived mechanically from the existing `target` grammar
(`e01` → subject; `e01 alias: X` → value; `claim: e01 occupation` → predicate).
Surface it as an `axis` column beside the existing `confidence` column in the
review CSV (or as a structured prefix within `critique_notes`). This **is** the
general form of §6 — the same review-layer mechanism, now inherited uniformly by
every property, with the name axes as instances. `Claim.confidence` stays
ephemeral and unchanged; the critic still only flags, never rewrites
(advisory-only, ADR-0013).

### 4. Predicate uncertainty has no graph home; coexistence is cross-source only

A Wikibase qualifier is scoped *under* a statement's committed property and cannot
doubt it; recording two predicates from one source fabricates a second assertion
(rule 5). So predicate uncertainty is deliberately **not** graph-representable and
not SPARQL-reappraisable — the critic flag is its only surfacing. Likewise,
**there is no "two coexisting candidate statements from one source" fallback**:
because `date_of_birth` (and the other single-value properties) are in
`disagreement-report.rq`'s `VALUES`, same-source candidates would appear as a fake
contradiction in the flagship R2 view. Coexistence is reserved for genuine
cross-source value multiplicity.

### 5. Reappraisal is a structural SPARQL query, honestly labelled

Ship `queries/under-witnessed-statements.rq` (rendered by `publish.py`) —
machine-written statements (`?st prov:wasDerivedFrom/pr:{inferred_from_extraction_run}`)
with exactly one distinct `stated_in` source and no coexisting curator
(run-reference-free) sibling on the same subject+property. It is the **structural
complement** of `disagreement-report.rq`: that view finds *over*-witnessed
contention; this finds *under*-witnessed machine calls. The name-reappraisal §6
could not previously express becomes one `VALUES` filter on the four typed-name
properties. Its header must state — ADR-0002 style — that this is a **queue, not a
defect report**, and it must be labelled **"under-witnessed / low-scrutiny," not
"contentious":** it measures under-witnessing (which can invert true
contentiousness) and it cannot see the predicate axis.

### 6. Capture the bulk-accept subset off-graph

`accept_all` (claims branch) writes `decision="bulk-accept"` to the durable
reviewed CSV — add `bulk-accept` to the APPROVE token set — so the disposition is
distinguishable from a per-row human `approve`; `apply_review` threads the mode so
the writer emits a distinct edit summary (e.g. `ingest: bulk-accepted claim`).
MediaWiki RecentChanges/history plus the ADR-0019 reviewed CSVs are then the
bulk-accept reappraisal queue — matching ADR-0020's "read where it matters, don't
store," and MODELING.md's assignment of a human decision's *process* to the edit
history. No graph node, no export surface, no staleness.

## Rulings

1. **No graph-resident machine confidence, contentiousness, or doubt** — the graph
   stays machine-confidence-free.
2. **Four axes → existing homes** (§1); no per-attribute confidence vector.
3. **The critic gains an `axis`**, generalising ADR-0022 §6 into a per-property
   review-layer channel.
4. **Coexistence is cross-source value multiplicity only** — never predicate
   hedging or single-source value-doubt (either would corrupt
   `disagreement-report.rq`).
5. **Reappraisal = a structural query + an off-graph bulk-accept signal**; the
   query is a low-scrutiny *queue*, not a contentiousness ranking, and the
   predicate axis is not queryable.
6. **Any future graph-resident acceptance fact is a run-item `ratification_mode`
   only, behind its own ADR** — never a per-statement marker or a confidence
   number.

## Consequences

- **Zero graph / export / privacy delta.** No new property, item, qualifier, node,
  or reference snak. The confidence-free graph is the safest ADR-0020 export
  surface — nothing new for the possibly-living sweep to reason about, no
  machine-epistemic leakage.
- **Rank rule 4 and ADR-0020 honored by construction** — no stored scalar can
  shadow rank or go stale.
- **One general mechanism**, not a name corner: the critic `axis` covers every
  property; the reappraisal query covers every machine write; the name case is a
  `VALUES` filter.
- **A genuine, accepted loss**: no stored ranking of how contentious the machine
  *felt* an item to be. Documented, not hidden.
- **Reliance on durable `var/` artifacts** (ADR-0019) for the bulk-accept audit
  dimension — if those are lost, that provenance is unrecoverable (unlike a graph
  fact); this is the accepted trade of the no-new-node design.

## Rejected and deferred alternatives

- **A per-statement `confidence` float in the graph** — uncalibrated,
  non-re-derivable, confident-but-wrong-blind; the ADR-0020 stored-stale-inference
  antipattern; a rank-governance hazard. Rejected.
- **A `contested_axis` / `assessment_confidence` *reference* snak** — a quantity
  reference snak materialises `prv:` value-node triples that `backup.py`'s
  predicate-suffix strip does not remove cleanly (it would rely on the orphan
  sweep), an added export-correctness cost the core design avoids by adding
  nothing. Rejected.
- **A per-statement `provisional_acceptance` marker** — puts *human*
  review-process into the graph, which MODELING.md assigns to the MediaWiki edit
  history; reprises the stored-stale-inference antipattern (a later per-row
  re-review would not clear it); and manufactures a de-facto vouched/provisional
  *trust tier* that source-first neutrality is designed to resist. Rejected.
- **Coexisting single-source candidate statements** for value or predicate doubt —
  corrupt the flagship disagreement report (§4). Rejected.
- **A single item-valued `ratification_mode` fact on the extraction-*run* item** —
  *deferred*, not adopted. Only if a pure-SPARQL bulk-accept filter later proves
  operationally necessary; it carries no PII and rides existing run-item handling,
  but it extends process-provenance-in-graph from *extraction* to *acceptance*,
  reintroduces an ADR-0020-shaped staleness risk, and mis-models a review-*episode*
  fact onto a *run* item — so it must be justified in its own ADR.

## Open questions

- Whether a run-item `ratification_mode` is ever worth its own ADR (only if the
  off-graph CSV + edit-history join proves operationally painful).
- **Unmatched-entity subject-doubt** (ADR-0022 open Q2): a brand-new entity with
  no blocking candidate generates no equivalences row, so a low-confidence
  *invented* subject gets no identity-uncertainty surfacing through the reconcile
  gate — this needs the lightweight unmatched-entity review view ADR-0022 already
  flagged.
- **Retention policy for `var/` reviewed CSVs** (ADR-0019): the bulk-accept
  reappraisal dimension depends on these durable artifacts.
- The under-witnessed query counts **multi-run corroboration by the same model**
  as "witnessed"; whether to discount same-model corroboration when scoping by
  run/model is a query-refinement question, not a schema one.
