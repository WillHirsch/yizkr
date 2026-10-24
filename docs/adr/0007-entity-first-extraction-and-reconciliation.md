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
Amended by #128 (2026-09-28): in `ingest run`, phase 3 has no roster.
Reconciliation is a stage before the write, and those read nothing from
the wiki (ADR-0025 §2), so the reconcile step matches a document's
entities against no graph entity and proposes nothing; matching against
a kept graph's roster comes back as an explicit, keyed input (#130). The
standalone `ingest reconcile` step and `reconcile-graph` still read the
graph's roster.
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): a manifest record of a person, place, organisation or
family gains a `structure`, what its names are made of, which is input to
identity and never written, and any record may carry a judgement note
(ADR-0035 §2, §4).
Amended by #388 (2026-10-04; the record's typed parts decided by the
orchestrator 2026-10-05, the owner may override): candidate blocking
(step 2) keys on whole name forms, folded for case and accents with
umlauts also written out (ADR-0035 §7), never on name tokens or
prefixes. A manifest entity keys through its mention record: a person
only when its status is `name`, by its label, aliases, unsplit forms and
nicknames, and by the whole forms its typed parts make, each given name
(as written and as its standard form) and each nickname before each
surname and patronymic, a pair whose parts fold alike making none
("Gustl" read as Gustav with the surname Löbig keys "Gustav Löbig";
"Agnes Löbig geb. Nertler" keys "Agnes Löbig" and "Agnes Nertler"); a
place only when it is a `toponym`, by its record's own names and
address, less a short form that is another name's toponym; any other
entity by its label and aliases. A graph item keys by its label,
aliases and the values of its birth, nickname and official name
statements; a married name, a family name alone, never stands for a
whole name. A shared surname or a clipped form ("Gus" against "Gustave
Lobig") no longer blocks a pair. The roster maps an item's kind to its
manifest key by the kind item's QID, so occupations and time periods
block like any other kind.
Amended by #477 (2026-10-05): every whole name form, a manifest's and a
graph item's alike, and every record part it is composed from, is read
in the form the instance keeps a term in (the writer's `stored_term`)
before it is folded. Folding already took NFD and NFC alike; the stored
form makes a control character one space and trims the edges, so
"Cäcilie\x08Löbig" meets the graph's "Cäcilie Löbig".
Amended by #407 (2026-09-29): the entity-manifest prompt asks for each
record and note; one that fails validation is set aside, never repaired,
and never refuses the manifest, and `coverage.json` counts the records
given, recordless and set aside (by reason) and the notes (by category).
The capitalised-token coverage check is gone, since capitals are no sign
of a name in every language.
Amended by #500 (2026-10-06): an empty entity list, or null, is the
engine's word that the document names nothing, and validation accepts it
at once: no second attempt, no line-range segmentation and no fallback
tier, so the batch holds no entity and no claim, its attention says "no
claims extracted: the entity manifest names no entity", and the run's
`zero-entities` and `zero-claims` checks read it as any batch without
entities, never as a `fallback-manifest`.

Amended by #106 (2026-10-23): step 3's adjudication runs at corpus
level too, over the ambiguous pairs of the corpus clustering
(`pipeline/adjudication.py`, Phase 3 below, "Corpus adjudication").
Amended by #568 (2026-10-23): the confident components of an ambiguous
corpus cluster merge before adjudication, which asks only the ambiguous
pairs between them (Phase 3 below, "Confident components").

Amends ADR-0006. The transport machinery specified there — forked
conversations, NDJSON emission with sentinel, per-chunk state with
resume, salvage validation — is unchanged; this ADR changes the *unit
of chunking* from line ranges to entities, and adds the reconciliation
stage that unit makes possible.
Amended by #523 (2026-10-24), see ADR-0018: a person's label or alias
that the item's own `referred_to_as` statements give as an appellation
keys nothing.
Amended by #638 (2026-10-24), see ADR-0018: so does a document person's
label or alias that its batch gives as an appellation, and the
adjudicator reads an appellation as what an entity is called, never as
an alias.

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
item key; an empty entity list is valid, a document that names nothing
(amended by #500). One retry, then fall back to ADR-0006 line-range
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
existing item on the paths that bring the writer its manifest records
and local refs.

- **Aliases.** A record whose item the resolve step matched by exact
  label gives the item the document's aliases too, after the claims, as
  a record the writer matches or an approved equivalence names already
  does. The resolve step leaves the record without a `qid`, so the writer
  pairs it with a QID that one of the batch's claims mentions, by that
  item's label; a record no claim mentions adds no alias. The label is
  the record's, or its tagged label where the writer would place the
  record under it (ADR-0010), its own label then an alias, with the
  record's aliases (amended by #476, 2026-10-05).
- **Kinds without a manifest.** A batch without an entity manifest, such
  as the line-range tier of ADR-0006 produces, states each entity's kind
  only on its local refs' `instance_of`. When the writer matches such a
  ref to an existing item by exact label, it records that kind as it
  records a manifest's, with the same comparison and with curated
  statements left as they are. It cites the artifact of the claim the
  ref is in, beside the batch's run: a manifest's kind cites the first
  claim that mentions the item, but a ref states its kind where it
  stands. The writer reads the first ref to each local id, as it does
  for the label, so each artifact where such a ref gives the kind is a
  witness of it.
  A face-proposal batch's sitters are such refs, so a sitter a curator
  approved and the writer found gains the photograph and the detection
  run as a source of its kind, as a sitter the writer mints takes its
  kind from them (ADR-0005). An item the batch itself minted holds the
  kind of the ref that minted it; a later ref to it under another local
  id, as each chunk or page of a document can name an entity by an id
  of its own, finds it by label as any such ref does, and is a witness
  of its own kind on the first write, so writing the batch again adds
  nothing. A kind the vocabulary lacks is recorded for no item, and the
  writer's result warns of each, found before its first write.
- **Where kinds do not arrive.** A ref the resolve step replaced with
  the item's QID brings no kind, since a QID carries none. `ingest run`
  resolves every batch before its write, so there a kind that a later
  source gives only on a ref (a batch without a manifest) does not
  arrive for an item that existed when the resolve step ran: only
  `ingest write` of an unresolved batch, and refs the resolve step left,
  bring it. A manifest record's kind still arrives, through the resolve
  step's label match (see the #233 amendment). Nor does a later ref to a
  local id an earlier ref named bring a kind, since the first ref to
  each local id gives it. The one exception (amended by #476,
  2026-10-05): a ref without a record that the resolve step resolves to
  the item under its tagged label (ADR-0010) is given a manifest record of
  its first ref's label and kind in the resolved batch, so its kind and
  its label, as an alias, reach that item as the unresolved write gives
  them. One with no kind stays local, for the writer to find that item
  itself, and so does one of a batch whose written claims cite more than
  one artifact, as a record's kind cites the artifact of the first claim
  that mentions the item alone, where a ref's kind cites the artifact of
  the first claim of each local id. Resolve reads the entity past
  every claim the writer refuses before its plan (the writer's
  `pre_plan_refusals`), so a made record's label and kind never come from
  such a claim; only the claims the writer refuses while it plans are
  unseen by it, and there a first ref may give the label and kind. #109, whose identity map is to replace
  exact-label resolution, takes on carrying every ref's kind to its
  item. Amended by #109 (2026-10-06): the resolve step matches nothing
  by label and makes no record; the writer resolves every local ref
  through the identity map, and each ref's kind, and a record's type
  hint, reach the item of its cluster, minted or found, cited to the
  artifact of the claim the ref is in, a later ref of an item the batch
  minted and chunks that give one entity two kinds among them.
- The orchestrator's write summary (`write.json`) and its log line count
  the type statements written and referenced.

Coverage report: in place of ADR-0006's line audit, `coverage.json`
flags manifest entities that yielded zero claims, and counts the mention
records and judgement notes (ADR-0035 §5; amended by #407, which removed
the capitalised-token check of lines that mention no manifest entity).
Advisory, as before.

### Phase 3 — reconciliation (proposals, never assertions)

A separate pipeline step between extract and resolve:

    yizkr ingest reconcile --batch batch.json --out equivalences.csv

1. **Roster prefetch** (deterministic): all graph entities with label,
   aliases, kind, and key relational facts (birth/death years, spouse
   labels, parent labels) via one SPARQL query. Cached to a file;
   small graph, single prompt.
2. **Candidate blocking** (deterministic): for each manifest entity,
   graph candidates sharing kind and any name token with any alias
   (a whole name form since #388, above).
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

**Corpus adjudication** (amended by #106, 2026-10-23). Corpus
clustering (ADR-0025; the identity-map draft, ADR-0027, #66) replaces
steps 1, 2 and 5 within a scratch run: it blocks and links every
mention against every other on records alone, and merges only confident
clusters (until the #568 amendment below). Step 3 then runs over what it could not decide: the ambiguous
pairs of each ambiguous candidate cluster, never a conflicting
cluster's, as one stage keyed by what each call sends (ADR-0032): the
pairs' mention fingerprints and evidence, the prompt and the model.
The prompt names mentions by fingerprint and pair number and documents
by SHA-1, never an item by its QID, and gives each mention's kind, names,
record, kin, places, roles and dates, each date at its own precision.
The verdict per pair is `same`, `different` or `uncertain` with a
one-sentence reason (the `distinct` of step 3 is `different` here, and
a confidence is not asked); verdicts and the raw answers are kept with
the run, as local-only data. Step 4 differs by stage. In a development-stage scratch run `same`
merges unattended (ADR-0025 §1): a cluster all of whose ambiguous pairs
are `same` or `different` is cut at its `different` pairs, and the
closure over its confident links and its `same` pairs gives its parts.
Each part is checked again before it merges: a `different` pair is a
cannot-link, so a part the closure joins across one stays apart, and so
does a part with any two members whose evidence says they differ. A
`same` verdict excuses only a date conflict on its own pair (sources
disagree on dates); whatever else the pair's evidence says differs keeps
it apart whatever the verdict: for people, stated sexes or civil
statuses that cannot both hold, and kin that conflict; for places,
containers that differ (neither equal nor nested); for organisations,
places that differ; for events and unions, kinds that cannot be one;
and for events, principals held apart (amended by #610, 2026-10-24, the
owner's decision there): two events whose principals ended in clusters
an adjudicator held apart, a member of one said `different` from a
member of the other, contradict, since one event cannot happen to two
people the identity map keeps apart. Events are settled after people,
so a part of events whose `same` verdicts join two such events does not
merge and falls back to its confident components, and a candidate
cluster of two such events is one no `same` could merge. Likewise for
unions, couple members held apart (amended by #641, 2026-10-24, the
owner's decision there): two unions whose couple members (spouses or
partners) ended in clusters an adjudicator held apart contradict, since
one union's spouse or partner cannot be two people the identity map
keeps apart. Unions
are settled after people, so a part of unions whose `same` verdicts
join two such unions does not merge and falls back to its confident
components, and a candidate cluster of two such unions is one no `same`
could merge. In
a development-stage run a candidate cluster of two whose one pair no
`same` could merge is not asked, since no verdict on it changes a
cluster, and is counted; in keeper mode it is asked, for the review
sheet.
A part merges with the method `adjudicated` when a `same` pair is among
its links, and `confident` when its confident links alone join it
(`confident_component` since the #568 amendment below), so
the identity map says what made each merge; a cluster with an
`uncertain` or unanswered pair stays split whole (until the #568
amendment below). In keeper mode the
verdicts are recorded for the cluster review sheet (#108) and merge
nothing, so a human still approves every equivalence that is kept.

**Confident components** (amended by #568, 2026-10-23; the owner chose
option 2 of #568). Corpus clustering no longer holds a whole ambiguous
candidate cluster apart for one ambiguous link. Each *confident
component* of an ambiguous candidate cluster, the closure over its
confident links, merges with the method `confident_component`, after
the same re-check the cluster check makes: a component that joins a
cannot-link pair (which a component of a cluster that joins none cannot),
holds a pair said `different` (a cannot-link, as above), or holds two
members whose evidence says they differ, stays apart, each member
`ambiguous`. A chain of confident links can join two people no one link
would; the re-check guards against that but cannot rule it out, so
every such merge is counted apart in the identity map, the run report
and the run diff, whose detail lists each one with its members. A
conflicting cluster merges nothing, its components included. The rule
holds in both modes: in keeper mode the components merge in the
identity map as confident clusters do, proposals the cluster review
ratifies (#108, #147). It changes adjudication in three ways.

1. *What is asked.* An ambiguous pair whose two mentions one merged
   component holds is settled by it and never asked. The pairs asked
   are the ambiguous pairs between components (a mention no confident
   link reaches being a component of one), and those inside a
   component its re-check kept apart, where a verdict can still change
   the outcome (a `same` excuses a date conflict on its own pair). Each
   is asked as before, on its own two mentions' evidence; the prompt
   gives no evidence of the rest of either component.
2. *What a verdict does.* A part is the closure over the confident
   links and the `same` pairs, so a `same` on any pair between two
   components joins both whole, and a `different` on any pair between
   them keeps them apart; two components given both verdicts are a
   part that joins a `different` pair, which does not merge. A part
   that does not merge falls back to its confident components, each
   merged or apart as without verdicts, so a verdict can add merges to
   the components but never undo one, unless it says `different` of a
   pair inside one. A cluster with an `uncertain` or unanswered pending
   pair is left as without verdicts: its components merged, the rest
   apart.
3. *What a method says.* A part merges `adjudicated` when a `same`
   pair is among its links, every member of the components it joins
   included, since the verdict made the merge; `confident_component`
   when its confident links alone join it, whether or not a verdict cut
   it from the rest; `confident` only for a whole confident candidate
   cluster. With the owner's extension of #130 (2026-10-23), each item's
   merge method (confident, adjudicated, confident component, owner
   override, singleton) is to be traceable in the graph.

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
  scale or for interactive curation; the deferred-on-trigger register,
  #171); automatic merge execution (never — human-gated by design).
