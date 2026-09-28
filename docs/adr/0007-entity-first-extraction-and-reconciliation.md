# ADR-0007: Entity-first extraction and entity reconciliation

Status: accepted (implemented: `pipeline/chunking.py` entity path,
`pipeline/reconcile.py`, writer alias handling; validated live — the
interview's entity manifest unified aliases unaided; document-side
reconciliation proposed the correct family-tree items for
Pünktchen/Gus/Leopold at 0.75–0.95 on relational evidence, flagging the
trial's duplicate items as duplicates; the graph-side pass proposed all
four known duplicate pairs and correctly rejected the two-Ursulas trap
as different generations; approved equivalences applied through
`resolve --equivalences` end to end)
Amended by #225 (2026-09-27): the entity manifest's state moves from
the stage store to the LLM cache of ADR-0032 (State and CLI).

Amends ADR-0006. The transport machinery specified there — forked
conversations, NDJSON emission with sentinel, per-chunk state with
resume, salvage validation — is unchanged; this ADR changes the *unit
of chunking* from line ranges to entities, and adds the reconciliation
stage that unit makes possible.

## Context

Two identity problems surfaced in the first trial and the ADR-0006
acceptance run:

1. **Within a document**, entities are referred to by multiple aliases
   ("Cäcilie Löbig" in a tree entry; "Cäcilie", "our great
   grandmother" in narrative) and sources themselves vary spellings
   ("Floerburg, Germany" / "Flörburg, Hessen, Deutschland"). Line-range
   chunking preserves whatever form each chunk happens to see; label
   consistency across chunks is luck, mitigated but not guaranteed by
   full-document context.
2. **Across documents**, exact-label resolve creates duplicates: the
   trial wrote "Leopold" (interview) alongside "Leopold Löbig" (family
   tree) as distinct people. Every further document re-mentions the
   same core family under variant names, so bulk ingestion without
   reconciliation makes the graph worse per document ingested.

Line-range chunking also made claim *ownership* ambiguous — the
ADR-0006 acceptance run dropped 22 parent-child claims whose evidence
straddled a chunk boundary, patched with a prompt rule. Entity-keyed
assignment makes ownership structural instead of prompted.

String matching (OpenRefine-style) is the wrong primary tool here:
the decisive identity evidence in genealogical material is
**relational** — "Hilde" whose spouse is "Sebastian Löbig" and
"Hildegard Laudler" whose spouse is "Sebastian 'Wastl' Löbig" are the
same person by structure, not by edit distance. Adjudicating that is
exactly what an LLM pass is good at, and exactly what must never be
auto-confirmed (R3/ADR-0005: identity errors corrupt every downstream
attribution; a human approves every equivalence).

Agentic graph access (an MCP server over the instance) was considered
and deferred: the full entity roster of a family archive (label,
aliases, key dates, kind) fits in a single prompt for years at this
graph's scale, so prefetching it preserves the pipeline's
pure-function reproducibility (prompt in, proposals out, state on
disk) and the engine's Read-only tool lockdown, at no capability cost.
Revisit when the roster outgrows a prompt (~10k entities) or for an
interactive curator assistant, which is a different feature.

## Decision

Extraction becomes entity-first (phases 1–2, replacing ADR-0006's
line-range units as the default), and a reconciliation stage (phase 3)
turns the resulting entity manifests into human-reviewed equivalence
decisions before anything is written.

### Phase 1 — entity manifest (the conversation opener)

The first call sends the full transcription (numbered lines, as
before) and asks for a concise manifest of every entity the document
mentions — small enough to fit a single turn even for dense documents
(~80 entities ≈ 7 KB):

    EntityRecord:
      id:            str        # document-scoped, e.g. "e01"
      label:         str        # canonical: the fullest form the document gives
      aliases:       list[str]  # every other way the document refers to it, verbatim
      kind:          str        # manifest item key: human, place, event, marriage, ...
      summary:       str        # one line, for reconciliation evidence
      mention_lines: list[int]  # best-effort; enables coverage audit + packing

    EntityManifest:
      entities: list[EntityRecord]

Validation: unique ids; non-empty labels; `kind` must be a manifest
item key. One retry, then fall back to ADR-0006 line-range
segmentation (which remains implemented as the degraded tier, with its
own offset-window fallback below it). The manifest turn stays in the
conversation, so every phase-2 fork sees the complete alias-unified
roster — this is what makes within-document identity exact rather
than lucky.

### Phase 2 — entity-keyed claim extraction

Entities pack greedily (document order) into chunks under the output
budget — by summed mention-line text when `mention_lines` is usable,
by entity count otherwise. Each chunk call forks the manifest
conversation (context modes and fallbacks exactly as ADR-0006 §3) and
is assigned a set of entity ids:

- Emit every claim whose **subject** is an assigned entity — including
  relations whose object or evidence lies elsewhere in the document
  (ownership is subject-keyed and total; the ADR-0006 ownership prompt
  patch disappears).
- Refer to ALL entities — subjects and objects — by their **manifest
  id** with their canonical label. Local ids are document-scoped now,
  so the merge stops chunk-prefixing them.
- NDJSON + terminal sentinel, unchanged.

New merge-time invariants (replacing chunk-prefixing):

- every local ref's `local_id` must exist in the manifest — an
  invented entity is a detectable reject, not a silent duplicate;
- labels on local refs are **normalized to the manifest's canonical
  label** (excerpts stay verbatim — they quote the source; labels
  identify the entity);
- exact-duplicate claims collapse; near-duplicates flow to review.

The `ClaimBatch` schema grows an optional `entities:
list[EntityRecord]` carrying the manifest. The writer materializes
local entities from it: canonical label, **aliases written as item
aliases** (MODELING.md already assigns name variants there), and
`instance of` from `kind` under the existing registrar reference
convention — so explicit instance_of claims become redundant rather
than required. Alias accumulation is append-only and idempotent; every
ingested document improves the next one's matching.

Amended by #88 (2026-09-27): the `instance of` and `type hint` the
writer writes when it mints an item carry, beside `stated in` → the
artifact that mentions the entity, `inferred from extraction run` → the
batch's run item (ADR-0003 §2). The kind is the extraction's proposal, a
machine write, so it takes both references rather than the registrar's
single one.

Amended by #233 (2026-09-27): kinds accumulate across sources, as
aliases do. When a later batch's manifest names an entity whose item
already exists (found by exact label, by the writer or the resolve
step, or named by an approved equivalence) and one of its claims
mentions the entity, the writer records the kind and type hint that
batch gives it, citing the artifact of the first such claim and the
batch's run. A matching `instance of` or `type hint` (a type hint
compared as free text) gains those two references; a different value
becomes a statement of its own, coexisting with the others at normal
rank (ADR-0002). A curated statement, one with no run reference or a
rank a curator set, is never altered: a source that agrees with it gets
a statement of its own beside it. A re-run adds nothing.

Amended by #240 (2026-09-28): a document's aliases and kinds reach an
existing item whichever way the writer finds it.

- **Aliases.** A record whose item the resolve step matched by exact
  label gives the item the document's aliases too, after the claims, as
  a record the writer matches or an approved equivalence names already
  does. The resolve step leaves the record without a `qid`, so the writer
  pairs it with a QID that one of the batch's claims mentions, by that
  item's label; a record no claim mentions adds no alias.
- **Kinds without a manifest.** A batch without an entity manifest, such
  as the line-range tier of ADR-0006 produces, states each entity's kind
  only on its local refs' `instance_of`. When the writer matches such a
  ref to an existing item by exact label, it records that kind as it
  records a manifest's: the same comparison, the same two references,
  and curated statements left as they are. A face-proposal batch's
  sitters are such refs, so a sitter a curator approved and the writer
  found gains the photograph and the detection run as a source of its
  kind, as a sitter the writer mints takes its kind from them
  (ADR-0005). A ref the resolve step replaced with the item's QID brings
  no kind, since a QID carries none. An item the batch itself minted is
  left out: it took its kind from the first ref that named it.
- The orchestrator's write summary (`write.json`) and its log line count
  the type statements written and referenced.

Coverage report: in place of ADR-0006's line audit, `coverage.json`
flags transcription lines that mention no manifest entity (heuristic:
capitalized-token match against labels/aliases) plus manifest entities
that yielded zero claims. Advisory, as before.

### Phase 3 — reconciliation (proposals, never assertions)

A separate pipeline step between extract and resolve:

    yizkr ingest reconcile --batch batch.json --out equivalences.csv

1. **Roster prefetch** (deterministic): all graph entities with label,
   aliases, kind, and key relational facts (birth/death years, spouse
   labels, parent labels) via one SPARQL query. Cached to a file;
   small graph, single prompt.
2. **Candidate blocking** (deterministic): for each manifest entity,
   graph candidates sharing kind and any name token with any alias.
   Keeps the LLM prompt small and the pipeline auditable — the LLM
   never free-searches the graph.
3. **LLM adjudication** (one call, or few): for each manifest entity
   with candidates, the model weighs relational evidence and emits
   `{entity_id, qid, verdict: same|uncertain|distinct, confidence,
   evidence}` — evidence is a human-readable sentence naming the
   relational facts that decide it ("same spouse, compatible birth
   decade, alias subset").
4. **Human review**: proposals land in `equivalences.csv` with a
   decision column, exactly like claim review. Nothing is
   auto-confirmed.
5. **Application**: `yizkr ingest resolve --equivalences
   equivalences.csv` maps approved `same` rows local_id → QID, so
   claims attach to the existing item instead of minting a duplicate;
   the writer then also appends the new document's aliases to that
   item. Unapproved rows leave the local ref for exact-label resolve
   as today.

**Graph-side reconciliation** (cleaning duplicates that already
exist, e.g. the trial's two Leopolds): the same blocking + adjudication
run over roster × roster, proposing pairs. A curator resolves each
approved pair by MediaWiki item merge (true duplicate) or — when
equivalence is itself a contestable, source-attributed judgement — a
`said to be the same as` statement (Wikidata P460 precedent; add the
property to the manifest), referenced per ADR-0003: `stated in` → each
artifact whose content supports the identification, plus `inferred
from extraction run` → the reconciliation-run item. Identity claims
are thereby ordinary claims: sourced, coexisting, contestable. Ships
as `yizkr reconcile-graph`, expected to be run occasionally rather
than per ingestion.

### State and CLI

The extraction state dir keeps its ADR-0006 layout with
`entity-manifest.json` alongside (or instead of) `segmentation.json`;
`--strategy chunked` now means entity-first, with line-range as the
recorded fallback (`manifest-fallback.txt`). `--resume` semantics are
unchanged. Reconciliation artifacts (`roster.json`, `candidates.json`,
`equivalences.csv`) live beside the batch, runtime-state as always.

Amended by #87 (2026-09-27), which made the entity manifest a stage of
the stage store, and by #225 (2026-09-27), which replaced the store with
the LLM cache of ADR-0032: the manifest call, like every call, is answered
from the cache when it was made before (ADR-0006 §5, docs/LLM_CACHE.md).
A manifest the extractor refuses is asked once more, as a second attempt
keyed apart in the cache; both answers are kept, so a later run takes the
same path (ADR-0032 §5). A fallback to line-range segmentation is an
attention reason on the extraction stage's `done` event and a `fallback`
entry in `coverage.json`, rather than a `manifest-fallback.txt` file. An
`entity-manifest.json` or `manifest-fallback.txt` from before the store is
not read; `ingest run` sets aside a document directory without
`derivations.json` and starts it afresh (ADR-0006 §5).

### Testing

Offline, scripted fakes: manifest validation + fallback to line-range;
entity packing by mentions and by count; unknown-manifest-id rejection;
label normalization to canonical; alias flow into `ClaimBatch.entities`
and the writer; blocking produces expected candidate sets; proposal
CSV round-trip into resolve. Live acceptance uses the answer key the
trial already produced: extracting the interview entity-first must
manifest Pünktchen/Gus/Leopold with aliases; reconciling against the
existing graph must propose `same` for exactly those three against
their family-tree items (relational evidence: kinship to the narrator
and to each other); and `reconcile-graph` must propose the two existing
Leopolds as a pair.

## Consequences

- Within-document identity becomes exact (one id per entity,
  aliases recorded) instead of dependent on label-consistency luck;
  claim ownership becomes structural; the manifest doubles as the
  reconciliation input, so the two problems share one artifact.
- Cross-document duplicates are prevented at ingestion (resolve to
  existing QIDs via reviewed proposals) rather than repaired after;
  repair of existing duplicates is a separate occasional pass.
- Identity decisions gain the same audit trail as every other
  statement: proposed by a run, approved by a human, referenced to
  sources.
- Costs: one more schema surface (`EntityRecord`, `ClaimBatch.entities`,
  an `equivalences.csv` contract); the line-coverage audit weakens to
  a mention heuristic; manifest quality becomes a single point of
  failure (mitigated by validation + retry + line-range fallback).
- The claim-extraction prompt contract changes (manifest ids), so
  ADR-0006's chunk prompt and merge are modified in place — one
  chunked pipeline, not two.
- Deferred: MCP/agentic graph access (revisit at roster ≫ prompt
  scale or for interactive curation); automatic merge execution
  (never — human-gated by design).
