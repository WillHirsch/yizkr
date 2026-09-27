# ADR-0011: Typed, referenced names

Status: accepted (typed name properties + GEDCOM emission + reconciler
evidence implemented; given/family components and promoting the primary
name to a referenced statement deferred, see "Later")

## Context

A person carries many names, and *which kind* each is matters to
identity: a name registered at birth, a maiden name, a name taken on
marriage, a formally/legally changed name, an informal nickname. That
typing is exactly the evidence that resolves alias questions — "Ida
Sörne" (birth) and "Ida Löbig" (married) are one person; a
matching *birth* name is stronger evidence of identity than a matching
married name, and a differing birth name is evidence of distinctness.

Before this ADR the model could not express any of it. Names lived in
just two places, both **MediaWiki term-store entries, not statements**:

- the entity **label** — mechanically "the fullest form the document
  gives", not a semantic choice;
- the **aliases** — a flat, untyped, append-only bag of name strings.

This has two costs. First, no typing: a maiden name, a married name and
a nickname are indistinguishable strings in one bag. Second — and in
direct tension with the archive's non-negotiable rule #1 (never a
statement without a reference) — aliases carry **no reference,
qualifier, or rank**. You cannot record *which source* used a name,
*when* a name applied, or that a name change happened on a date. The
provenance the system demands everywhere else is absent for names, and
the reconciler is left matching flat strings with no structure to weigh.

## Decision

Add **typed, referenced name properties** as a second layer over the
alias bag — additive, not a replacement.

### Two layers

- **Aliases stay the match index.** Every name form a document uses is
  still contributed to the item's aliases (append-only), because aliases
  are also the reconciliation blocking substrate and the label lookup
  key. Unchanged.
- **Typed name statements are the source-first record.** When a source
  states a name's *type*, that becomes an ordinary referenced statement
  — carrying the `stated in → artifact` reference (and, for machine
  writes, the extraction-run reference) like every other claim, and
  datable with `start time` / `point in time` qualifiers.

### Vocabulary (mirrors Wikidata, monolingual text)

- **birth name** (P1477) — name/maiden name given at birth
- **married name** (P2562) — family name taken on marriage
- **nickname** (P1449) — informal/pet name or "aka"
- **official name** (P1448) — a formally adopted or legally changed name

Monolingual text keeps the source's exact wording; the language tag is a
datatype requirement, defaulted pragmatically (the ingest language / the
GEDCOM adapter's "en"), not a content assertion.

### GEDCOM adapter emits what the export marks

Deterministically, never guessing:

- **NAME TYPE** subtag → the matching property (`birth`/`maiden` →
  birth name; `married` → married name; `aka`/`nick` → nickname;
  `official`/`legal`/`change`/`immigrant`/`adopted` → official name); an
  absent or unrecognised TYPE leaves the name a plain label/alias.
- **NICK** subtag and the non-standard **_MARNM** tag → nickname /
  married name.
- **Inline convention** — a quoted or single-word parenthetical token in
  a name ("Ida (Peppi) Sörne", "Cäcilie 'Cilli' Tarle") is the
  near-universal way genealogy data writes a nickname, and is the *only*
  way the real Ancestry export expresses them. Extracted conservatively
  (≤ two words, no digits, so "(born 1900)" is never mistaken for a
  name). The full form stays the label; the nickname is added alongside.

Each name claim is referenced to the exact GEDCOM record that states it.
A plainly-named person (untyped, unquoted) gets no name claim — only
their label and aliases, as before.

### Reconciler consumes typed names as evidence

- **Batch side, free:** the adjudicator already renders every claim of a
  batch entity into the prompt (`entity_facts`), so a GEDCOM's or an
  LLM's typed name claims appear as evidence automatically (monolingual
  values now render as plain text).
- **Roster side:** the roster prefetch query and `RosterEntity.facts()`
  now include existing items' birth/married/nickname/official names, so
  the adjudicator can line them up across the new document and the graph.
- **Guidance:** the adjudication prompt is told that a married name
  equalling another's birth/maiden name (a marriage name change), or a
  shared nickname, is strong identity evidence; a differing birth name
  points to distinctness.

### Extraction path

The four properties join the manifest, so they appear in the extraction
prompt's property list automatically; a short instruction tells the
model to use them only when a source marks a name's type, and to leave
an ordinary name in the entity manifest's label/aliases (not restate it
as a claim).

## Consequences

- Names become *expressive*: the model can now answer "what was she
  called at birth vs after marriage vs informally", attribute each name
  form to the source that used it, and (via date qualifiers) record a
  formal change as a dated event — closing the "names are unreferenced"
  gap for typed names.
- Reconciliation gets structured evidence instead of flat-string
  overlap, on both the new-document and graph-cleanup passes.
- Frontends can display plural, source-labelled name forms like any
  other property (the static site renders monolingual values already).
- **Deliberately still flat:** an *ordinary* untyped name is not
  promoted to a referenced `name` (P2561) statement — the label/alias
  index remains its home, to avoid doubling every person's claim count
  for no new information. Recording the principal name as a referenced
  statement is a later option if the unreferenced label ever needs
  provenance.
- **Not built:** structured given/family-name components (P735/P734, the
  GEDCOM GIVN/SURN the export carries) as shared name *items* — heavier
  (mints name items) and lower reconciliation value; deferred. Maiden
  names are only recorded when a source marks them (TYPE maiden); they
  are never *inferred* from gender + marriage, which would be guessing.
