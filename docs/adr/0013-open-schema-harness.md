# ADR-0013: Open-schema extraction harness

Status: accepted

Amended by #7 (2026-09-29): the terminal sentinel carries no gap count,
nor any other (§B).
Amended by #386 (2026-09-29): each critic finding is a validated record
with an axis and a category, not a `target` string (§C, ADR-0023 §3).

## Context

Two trial defects — a company typed as a `place` (ADR-0012), a form
field label "MOTHER" captured as a person's alias — look like separate
bugs but share one root cause: **the pipeline treats the schema as
closed and total.** Every entity must be assigned a kind from a fixed
list; every fact either maps to a known property or silently vanishes
(`validate_against_manifest` hard-rejects any non-manifest property
key). When the model meets something the schema cannot hold, its only
moves are to **coerce into the nearest wrong slot** or **drop it** — and
coercion usually wins, because a company really is place-adjacent and a
role label really does refer to a person.

This is the same failure the project already refuses for *values*
("uncertainty is structural, never silent" — never round a date, never
narrow a source). Here the uncertainty is about **schema coverage**, and
it is being silently resolved wrong. The fix is to make the schema
**open and partial**: give the model a first-class, structured way to
say *"this exists and matters, but your vocabulary cannot express it,"*
turning schema limits into reviewable signals that also drive vocabulary
growth — instead of corruption a human has to spot by eye.

Two distinct failure modes need distinct treatment:

1. **Missing vocabulary** — the schema genuinely cannot express it (a
   company before ADR-0012; a relation like "deported to" / "imprisoned
   at"). Mechanisms A and B.
2. **Miscategorisation under pressure** — the schema *could* handle it,
   but the model force-fits a plausible-adjacent slot (MOTHER→alias;
   company→place). An escape hatch does not help, because the model was
   confident. Mechanism C.

## Decision

Three composing mechanisms plus prompt hardening.

### A. Entity escape hatch — `unclassified_entity` + `type_hint`

A holding class item `unclassified_entity` and an optional free-text
`type_hint` field on the entity manifest. When no real kind fits, the
model records the entity as `unclassified_entity` with a hint
("company", "ship", "military regiment", "congregation") rather than
mistyping it as a person or place. These are **written to the graph**
under the holding class (a source asserted the entity exists; recording
it is source-first), carrying `type_hint` as a referenced string
statement and any claims about it that *are* expressible. Because their
`instance of` is `unclassified_entity`, they never pollute person
timelines or the places map. A standing SPARQL view — unclassified
entities and their hints, by frequency — is the input to the next
vocabulary round.

This closes the structural hole: claims could always be skipped, but an
entity was *forced* to pick a kind, so mistyping was mandatory.

### B. Relation gap channel — `gaps.ndjson`

Extraction emits, alongside claim lines, **gap lines** for relations no
property expresses:

    {"gap": {"subject": "<id/label>", "predicate": "<free text>",
             "object": "<id/label/literal>", "evidence": "<excerpt>"}}

and its terminal sentinel stays `{"done": true}` (ADR-0006 §3). Gap
lines are parsed and aggregated across chunks into a per-document
`gaps.ndjson`, counted in the run log, and rendered to a `gaps.csv` the
curator reads — but are **never written to the graph** (they have no
property id). A relation the model would otherwise have dropped or
forced becomes a lossless, reviewable "the vocabulary is missing this"
signal.

### C. Critic pass

After extraction, one adversarial LLM turn reviews the entity manifest +
claims and flags confident-but-wrong assignments the first pass will not
self-report: an entity whose kind looks wrong (an agent typed as a
place), an alias that looks like a role/field label rather than a name
(MOTHER, INFORMANT, BRIDE), a claim whose property looks misapplied. It
**flags, never rewrites** — findings land in `critique.json`, are
counted in the run log, and are merged into the claim review CSV's
`critique_notes` column (renamed from `warning`, ADR-0020) so the curator
sees them at the existing gate. Each `Finding` is a record naming its
entity (and a claim or an alias) by id, the statement-part it doubts
(`axis`: subject/predicate/value/existence, ADR-0023) and a `category`
from the fixed list kept with the critic prompt; code validates it
against the batch and the manifest, and keeps one that fails verbatim,
counted, never repaired (ADR-0023 §3 as amended). This
is the reconciliation adjudicator's pattern applied to extraction, and it
is what would have caught both MOTHER and the hat shop without a human
eyeballing raw output.

### Prompt hardening

The entity-manifest and chunk prompts are told, with concrete negative
examples, to **prefer the escape hatch over a wrong-fit slot**: a
company/committee/agency is an organization or (if unmodelled)
`unclassified_entity`, never a place or person; a form field or section
label (MOTHER, FATHER, INFORMANT, BRIDE) names a role, never a person or
an alias.

## Consequences

- Schema limits become structured, reviewed data instead of silent
  corruption — extending "uncertainty is structural" from values to
  coverage. Each ingestion produces a populated gap log and unclassified
  view that say what the *next* vocabulary round should add.
- Nothing a source asserts is lost: an unmappable entity is held (typed
  `unclassified_entity`), an unmappable relation is logged, and neither
  pollutes the modelled graph.
- The critic catches miscategorisation the hatch cannot, at one extra
  LLM turn per document — the main added cost, accepted for the
  correctness gain on exactly this kind of error.

### The real risk, and its controls

The escape hatch can become a **dumping ground** — the model taking the
easy `unclassified` path and degrading extraction. Three controls keep
it honest: (i) guidance that `unclassified_entity` requires a
`type_hint` and is a last resort; (ii) the gap/unclassified surfaces are
*reviewed*, so over-use is visible, not hidden; (iii) the critic flags
"this was dumped as unclassified but is clearly a person." The gap log
and unclassified view only pay off if a curator acts on them — minting
vocabulary and re-ingesting — which is the same human-in-the-loop
curation the rest of the system already assumes.

## Not in scope

Auto-minting vocabulary from gaps (a human decides what to add);
auto-applying critic findings (flags only); multi-kind entities (an
entity that is genuinely org *and* place — deferred with ADR-0012's
Fork C; see #47).
