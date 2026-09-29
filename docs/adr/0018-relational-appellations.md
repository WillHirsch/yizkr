# ADR-0018: Relational and perspectival appellations

Status: **accepted** — implemented (manifest `referred_to_as` +
`perspective`, extraction-prompt routing, critic update). No retro-fit:
the model applies to a clean re-ingest, not a migration of existing rows
(see Rulings).
Amended by #222 (2026-09-27): in "Reciprocal / dyadic naming", the
example records the literal "Oma" as (A), `relative` + `kinship_type`, as
Ruling 2 routes it, and the section tells the two statements apart by
subject and ego (the `relative` object or the `perspective` qualifier).
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): a `relative` claim gains a controlled `kinship_class`,
with the side, degree and removes its term states, beside the verbatim
`kinship_type` (ADR-0035 §3.3).

## Context

Ingestion routes relational references to a person — "our great
grandmother", "meine Oma", "Nana Maisie", "my husband", "the deceased" —
into that person's **alias bag**. The entity-manifest extraction prompt
instructs it directly; its worked example unifies `"Cäcilie Löbig"`,
`"Cäcilie"`, and `"our great grandmother"` into one entry whose aliases list
"every other form, verbatim".

That is the wrong home, on three axes at once. ADR-0011 defines aliases as
"a flat, untyped, append-only bag of name strings" that carry "no
reference, qualifier, or rank". A relational reference is:

- **not a name** — "great grandmother" is a *description*, and the alias
  bag asserts it as an appellation *of* Cäcilie without saying so;
- **perspectival** — "our" / "meine" is relative to a speaker; the term is
  meaningless without the viewpoint the alias bag discards;
- **unreferenced** — no record of *who* used it or *which source*.

The cost is visible as **critic traffic**: `critique.py` is built to flag
"a descriptive phrase ('my husband', 'the deceased', 'our friends')
recorded as if it were a name". So every such alias draws an adversarial
finding to the review gate — the critic correctly objecting to a modelling
error the extractor was *told* to make. Fixing the model removes the
traffic at its source rather than triaging it downstream.

There is also an opportunity. A perspectival reference carries the very
context — the ego, the "our" — that lets a *later* bare reference ("Grandpa
took me…", "Tante said…") be resolved to a specific person. Captured as an
absolute alias, that context is thrown away; captured perspectivally, it
becomes a resolution key.

## Decision

A relational reference to a person is **never an alias**. It is one of two
things, and the extractor routes it accordingly:

### (A) A derivable kinship description → `relative` + `kinship_type`

"our great grandmother", "meine Oma", "mein Onkel", "Dad's cousin Leopold"
assert a **kinship relation** to an ego. These reuse the existing ADR-0014
machinery with no new vocabulary: `Cäcilie —relative→ <ego> ; kinship_type =
"great grandmother"`, referenced to the artifact. The ego (the "our") is
the *object* of the statement, so the viewpoint is captured structurally,
and the frontend re-renders the term from the derivation calculus
(gendered, degreed) rather than storing a string on the person.

### (B) A non-derivable appellation → a new perspectival property

"Nana Maisie" where Maisie is no relation (a courtesy title), "Bubele" (a pet
name), a family-specific term of address — these are genuine appellations
but not literal kinship. They get a **new, first-class, referenced
statement on the referent**:

- **`referred_to_as`** (monolingualtext) — the appellation, verbatim, in
  the source's wording. Mirrors the spirit of Wikidata **P1810 "subject
  named as"** (the term a specific context uses for a subject); it is
  deliberately *not* `nickname` (P1449), which is an *absolute* informal
  personal name ("Peppi", "Cilli") and would re-import the perspective
  problem.
- **`perspective`** (wikibase-item, qualifier, **optional**) — the person
  or `family` (ADR-0014) from whose viewpoint the appellation applies (the
  ego). Omitted when the only viewpoint is the source's own, which the
  statement's artifact reference already carries.

So "X is known to Y as Z" is `X —referred_to_as→ "Z" ; perspective = Y`,
referenced to the artifact that records it.

#### The `family` perspective-holder is opportunistic, never fabricated

A `family` item enters the graph exactly like any other entity — the
entity-manifest pass mints one **only when a source names a collective**
("the Löbigs", "the Lobig family", "the Lobig couple"), after which
it flows through reconcile → review → write and its members attach via
kinship or `member of`. A bare possessive — "*our* great grandmother",
"*meine* Oma" — is a pronoun, not a named collective, and must **never**
mint a family (that would be inventing an entity from a pronoun, exactly
the guessing rules 5–6 forbid). Consequently `perspective` points at a
family only when ADR-0014's extractor has *independently* named that
family; the appellation layer is never a back-door reason to create one.

Where a family item *does* legitimately exist, it is the **preferred**
perspective value over any single ego: a later "our Oma" from that family
then resolves transitively to any member's grandmother, not just one
person's. Where no named collective exists, `perspective` falls back to a
specific person (the author/informant, when identifiable) or is left
empty — leaning on the source reference for viewpoint. (Family membership
rides `member of`, whose scope is widened here to include a `family`, not
only an organization, so "who is *in* the Lobig family" is recordable.)

### The routing rule (conservative by default)

Promote to (A) only when the source supports a **literal** kinship reading;
otherwise use (B). `relative` + `kinship_type` *asserts a kinship edge*, so
sending a courtesy "Tante" there would fabricate a relation. `referred_to_as`
asserts only that the term was used — it never narrows or invents (rules 5
& 6). When in doubt, (B) is safe. Aliases keep only genuine **name** forms
(spellings, maiden/married names); the label stays the fullest real name.

### Reciprocal / dyadic naming (each party names the other)

The model must hold the case where *both* members of a pair have a
relational name for the other worth recording — a grandmother known to her
grandchild as "Oma" while the grandchild is known to her as "Bubele"; two
sisters who each call the other "Schwesterchen". This falls out of the
design because **every appellation is a statement whose SUBJECT is the
referent and whose ego is the other party**:

- The two directions are **two independent statements on two different
  subjects** — `grandmother —relative→ grandchild ; kinship_type =
  "Oma"` and `grandchild —referred_to_as→ "Bubele" ; perspective =
  grandmother`. There is no dyad object to build and nothing to keep in
  sync; the subject and the ego (the `relative` object on one side, the
  `perspective` qualifier on the other) distinguish them.
- A **symmetric term** ("Schwesterchen" both ways) is still two statements,
  disambiguated by subject — never one conflated value. Neither shadows the
  other.
- A reciprocal pair may be **mixed type**: "Oma" is derivable kinship → (A)
  on one side, "Bubele" is a pet name → (B) on the other. The two
  mechanisms compose without special-casing.
- For the (A)/kinship direction, the **inverse is derived, not stored**:
  if `grandmother —relative→ grandchild` is recorded, the calculus yields
  the grandchild→grandmother direction, so a reciprocal kinship pair need
  be asserted only once (recording both is allowed but redundant — a
  match, per ADR-0014, corroborates).
- One source frequently records **both directions at once** ("your loving
  Oma" over a signature, a letter that names writer and recipient); both
  statements cite the same artifact, kept apart by subject and ego (the
  `relative` object or the `perspective` qualifier).

### Decoding otherwise-context-free references

The two layers give two resolution paths, and the **ego is the key** each
one needs:

- **Compositional terms** ("Grandpa said…") decode by **kinship derivation
  given the ego**: query the person who is grandfather-of-ego over the
  primitive graph. This resolves references the source never spelled out,
  as long as the viewpoint is anchored — the anchor the alias bag discarded
  and this model preserves.
- **Idiosyncratic terms** decode via the `referred_to_as` + `perspective`
  statements as an explicit table: the same "Tante" that denotes different
  women across families' letters resolves correctly because the perspective
  scopes it.

### Neutral coexistence and privacy

Both properties are multi-valued and source-attributed: many people may
call one person many things, each appellation its own referenced statement
at normal rank, none canonical (ADR-0002). Appellations of possibly-living
people are personal data and ride the existing possibly-living exclusion
(ADR-0020, MODELING.md) — dropped with every other mention of an excluded
person; no special handling, but noted.

## Rulings

1. **`perspective` value type.** Item-valued, optional, taking a **person
   or a `family`** — the family value used only when the collective is
   independently named (see above), never fabricated from a pronoun.
2. **(A) vs (B) boundary.** Route **literal** kinship terms to `relative` +
   `kinship_type` (ego as object); reserve `referred_to_as` for
   non-kinship appellations. Courtesy and idiosyncratic terms default to
   (B), which asserts only that the term was used and so never invents a
   relation.
3. **Ego-identification policy.** Fill `perspective` when an entity (person
   or named family) is available; otherwise **omit it** and lean on the
   source reference. Never guess an ego, and never mint an entity to have
   one.
4. **No retro-fit.** The model applies to future ingests only; existing
   role/relational aliases already in the graph are **not** migrated. The
   combined noise from recently-fixed extraction/review pathologies (this
   one included) makes a **single clean re-ingest from scratch** more
   efficient than a migration pass, so none is built.

## Consequences

- The critic stops flagging relational references (they are no longer
  mis-typed as names), removing a standing source of review-gate traffic.
- Aliases regain their meaning as a name-match index; reconciliation
  blocking no longer carries perspective-free descriptive strings that
  never match anything.
- The archive gains a queryable appellation layer and, with the ego
  anchored, the ability to resolve bare relational references — including
  ones no single source disambiguates.
- One new property (`referred_to_as`) and one qualifier (`perspective`),
  plus a widened `member of` (now person → organization *or* family); the
  derivable-kinship path reuses ADR-0014 unchanged.
- No migration code: the next run is a clean re-ingest, so the model takes
  effect corpus-wide without a retro-fit pass.
