# ADR-0014: Next vocabulary round (authored by the harness)

Status: accepted — all four ★ forks resolved as recommended (kinship
`relative`+verbatim-type fallback; persecution as event subclasses;
co-residence derived + a `gave refuge to` property; singleton
monument/dynasty classes deferred). Implemented: manifest vocabulary,
extractor guidance, GEDCOM `SEX`→`sex_or_gender`, and the
`queries/kinship.rq` derivation starter. The full pairwise kinship
calculus (degrees: great-, once-removed, half-) remains a frontend
build over the same primitives.

## Context

The open-schema harness (ADR-0013) produced, as a byproduct of the clean
reinit ingestion, a ranked and evidence-backed inventory of what the
schema is missing: **236 gap relations**, **80 organization** entities,
and a set of escape-hatch `type_hint`s — each backed by the exact
documents that demand it. This ADR turns that inventory into the next
vocabulary round. Every item cites its gap-log frequency; nothing here
was hand-guessed. That the round is *authored by the pipeline* rather
than by eyeballing output is ADR-0013's thesis realised.

## 1. Kinship — a calculus over primitives, not more properties ★

The gap log's kinship entries — brother-in-law (×3), cousin, grandson,
granddaughter, great-… — are **second-order relations**: paths through
primitives the graph already stores. Modelling them as properties would
denormalise the graph and, worse, miss the goal — a frontend should be
able to describe how *any* pair of people is related, which is a path
computation, not a lookup. So we do NOT add `cousin`, `aunt`, etc.

**The model has three layers (mirroring the names two-layer of ADR-0011):**

- **Primitive edges (stored, source-attributed):** `father` (P22),
  `mother` (P25), `spouse` (P26), `sibling` (P3373), `child` (P40).
  Plus **`sex or gender` (P21)** — a prerequisite, since the calculus
  cannot produce gendered terms (aunt vs uncle, grandmother vs
  grandfather) without it. `sibling` stays *asserted-not-derived* (a
  source may assert siblinghood directly) AND is *also* derivable from
  shared parents; both coexist and the calculus consumes both.

- **A derivation calculus (query/frontend layer — NOT graph
  vocabulary):** SPARQL property paths mapped to a shared kinship-term
  set, answering "how is A related to B" for any pair with one
  vocabulary. E.g. grandparent = `(father|mother)/(father|mother)`;
  aunt/uncle = `(father|mother)/sibling` (+ its spouse); cousin =
  `(father|mother)/sibling/child`; in-laws via `spouse`; degrees
  (great-, once-removed) from path length. This ships as a documented
  convention + a reusable query-library entry, not new manifest keys.

- **Source-asserted compound relations — a single generic `relative`
  (P1038) with a verbatim `kinship type` qualifier.** When a source
  asserts "Aunt Pünktchen" and the connecting parent is unknown
  or the term is ambiguous (an aunt may be a parent's sister *or* a
  parent's brother's wife — decomposing would narrow what the source
  said, forbidden), the assertion is stored faithfully as `relative →
  person` qualified by the source's exact wording. ONE property, not N.
  The calculus and this qualifier share the same term set, so asserted
  and derived relationships describe uniformly and can corroborate — or
  disagree, as ordinary neutral-coexistence.

★ **Fork:** the compound-assertion fallback — (a) generic `relative` +
verbatim type *(recommended: source-first, one property, decomposable to
primitives later when the intermediate person is established)*; (b)
always decompose to primitives, minting placeholder people for unknown
intermediates *(risks narrowing ambiguous terms)*; (c) leave compound
assertions in the gap log until primitives exist *(loses common, central
assertions like "aunt")*.

## 2. Persecution & displacement — events, not relations

The gap log's loudest single entry is **"deported to" (×10)** — but the
archive already models deportations as **events** ("Deportation of Ida
Löbig" and "Liberation of Ida Löbig" were reconciled in the trial). The gap
appears only because the extractor emitted a person→place *relation*
where an *event* belongs.

**Add event subclasses** `deportation`, `imprisonment`, `release`
(siblings of the existing `migration`/`military_service`), reusing the
existing event machinery — `participant` (person), `location`/
destination, `point_in_time`. Extractor guidance: "deported to X on
date" is a deportation *event*, not a property. This also absorbs
"imprisoned at", "released from", and persecution acts like an arrest
("was taken the first week of the war"). No new relation properties —
the event model already carries it.

★ **Fork:** event subclasses *(recommended — consistent with the
existing, already-reconciled deportation events)* vs a flat
`deported to` / `detained at` property.

## 3. New entity classes — from recurring escape-hatch hints

Minted where the hint recurred or is thematically central; ranked:

- **`family` / `household`** (×5, e.g. "the Löbigs", "the Lobig couple",
  "the Lobig family") — named family units. This is the long-deferred
  "collective manifest entities" guidance; members
  attach via the kinship primitives or `member of`.
- **`vessel`** (×3, e.g. RMS Flund) — the ships people emigrated on;
  recurs and is central to the migration story.
- **`legal instrument`** (laws, decrees, gazettes, registers — a civil
  code, a law gazette, a civil register; the critic also suggested a
  `document`/`law` kind for several
  escape-hatch entries). Minting this ALSO closes the escape-hatch
  over-use the critic flagged.
- **`military unit`** (a regiment, a brigade).

★ **Fork:** defer the singletons — `monument` (a statue ×1),
`dynasty` (a royal house ×1) — folding them into
place/organization or leaving them `unclassified_entity` until they
recur *(recommended)*; or mint them now.

## 4. Person-attribute properties — each backed by the gap log

- **`country of citizenship`** (P27) — "Staatsangehörigkeit" (×3).
- **`religion or worldview`** (P140) — "religiöses Bekenntnis" (×2).
- **`educated at`** (P69) — "educated at" / "registered in school"
  (×3+); person → organization (school), pairs with the org class.
- **`member of`** (P463) — "member of" (×3); person → organization —
  the item deferred in ADR-0012's Fork B, now demanded by the data.

(`employer` P108 landed in ADR-0012.)

## 5. Sheltering / refuge — a decision, not a default ★

"provided a home for" / "given a home by" / "lived with" (×7 combined)
is central to the emigration story but genuinely splits:

- **"lived with"** is co-residence — *derivable* from a shared
  `residence` over overlapping time, like kinship (a path, not a
  property). No new vocabulary.
- **"provided a home for" / "given a home by"** is asymmetric
  hospitality — a real, recurring, NON-derivable social act.

★ **Fork (needs your call):** derive co-residence from shared residence
+ add one asymmetric **`gave refuge to`** property for the sheltering
*(recommended — it is meaningful, recurrent, and irreducible in this
corpus)*; OR model sheltering as a "taking-in" event; OR defer to the
gap log.

## 6. Deferred / not this round

- **"bankkonto bei"** (×2) — a bank-account relation; niche, and it
  pairs an account (not yet an entity) with a bank. Defer.
- **Divorce-as-event** ("Divorce of Leopold Löbig", hint=divorce) —
  largely redundant with the existing `spouse` `end_time` qualifier
  (the GEDCOM adapter already maps DIV to it); revisit only if a source
  narrates a divorce with its own participants/date.
- **Anachronistic tool mentions** (ChatGPT, Google Translate,
  Transkribus, surfaced from a modern transcription's editorial notes) —
  curation noise to reject, not vocabulary.

## Consequences

- Kinship stays a thin primitive graph + a derivation layer, so the
  frontend describes any pair with one vocabulary and no denormalised
  edges — and `sex or gender` (P21) becomes a first-class attribute the
  archive lacked.
- Persecution/displacement reuses the event model rather than sprouting
  relation properties, keeping the reconciled deportation events
  coherent.
- The escape-hatch hints become real classes (family, vessel, legal
  instrument, military unit), and minting `legal instrument` retires a
  recurring escape-hatch over-use.
- Roughly **six new properties** (P21, P1038, P27, P140, P69, P463,
  minus the deferred), **four–five classes**, **three event subclasses**,
  and one query-library kinship calculus — most of the kinship work is
  documentation + queries, not graph writes.

## What needs your ruling (the ★ forks)

1. **Kinship compound-assertion fallback** — generic `relative` +
   verbatim type (rec), decompose-to-primitives, or gap-log.
2. **Persecution** — event subclasses (rec) vs relation properties.
3. **Sheltering** — derive co-residence + a `gave refuge to` property
   (rec), an event, or defer.
4. **Singleton classes** (monument/dynasty) — defer (rec) or mint.

The other items (P21, P27, P140, P69, P463; family/vessel/legal-
instrument/military-unit classes; deportation/imprisonment/release
events) I'd take as the recommended defaults unless you say otherwise.
