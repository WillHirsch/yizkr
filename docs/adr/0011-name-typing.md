# ADR-0011: Typed, referenced names

Status: accepted (typed name properties + GEDCOM emission + reconciler
evidence implemented; given/family components and promoting the primary
name to a referenced statement deferred, see "Consequences" and #171)
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): the GEDCOM adapter no longer reads a bracketed part of
a name as a nickname, since a bracket may hold a maiden name, a later
name or a note; it becomes an alias with no type, counted, and only a
quoted part stays a nickname (ADR-0035 §6).
Amended by #408 (2026-09-29): implemented as ADR-0035 §6 says; the label
leaves the bracketed part out, and the language tag of a GEDCOM name is
the export's HEAD.LANG, or `und`, no longer a fixed "en".
Amended by #435 (2026-10-02, the owner's decision on the bracket
question): a name form a source gives without stating its type becomes a
referenced statement of a generic **name** property (P2561), in addition
to its alias; see "Name forms of no stated type" below.

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
  (amended by #434 (2026-10-02): a name the source gives as what the
  person is called, by anyone; a relational appellation is ADR-0018's
  `referred_to_as`)
- **official name** (P1448) — a formally adopted or legally changed name

Monolingual text keeps the source's exact wording; the language tag is a
datatype requirement, defaulted pragmatically (the ingest language / the
GEDCOM adapter's "en"), not a content assertion.

### Name forms of no stated type

Amended by #435 (2026-10-02). Where a source gives a name form without
stating its type, the type is unknown, and the alias alone would lose
both that uncertainty (rule 5) and the fact that *this* source used the
name (rule 1). Such a form becomes a statement of a generic **name**
property (P2561, monolingual text, verbatim), referenced like every
claim: `stated in` the artifact and its locator, plus the run reference
for a machine write. It is in addition to the alias, not instead of it:
aliases stay the match index.

- **Where it comes from.** In a GEDCOM export: a bracketed part of a
  NAME ("Ida (Peppi) Sörne"; ADR-0035 §6's bracket rule says a bracket
  may hold a nickname, a maiden name, a later name or a note), a further
  NAME with no TYPE ("Hilde Löbig"), and a NAME whose TYPE the adapter
  does not recognise, each cited to its NAME and in the language of the
  adapter's other texts. In a document: a name form beside the label's
  whose type the source does not mark, such as a bracketed name in a
  register the model cannot place.
- **Where it never goes.** Never for a form the source types (birth,
  married, nickname, official), anywhere in the person's record, whether
  or not a typed claim is made of it: a NAME whose TYPE the adapter
  recognises but makes no typed claim of (a NAME without slashes, a
  typed NAME with a placeholder) and a `_MARNM` without slashes are
  typed by their source. Never for the label's own form. The label and
  the typed forms are compared in NFC, whitespace collapsed and letter
  case ignored, as alias hygiene ignores case ("IDA SÖRNE" is the label
  "Ida Sörne"); accents are kept, so a spelling variant is a name of its
  own (orchestrator decision BO, 2026-10-04). In a GEDCOM export, never
  for a form the source is unsure of (counted, `untyped name form with a
  placeholder`): the display form of a NAME that held a placeholder
  ("Eugen /?/"), which would read as the whole of the name, or a bracket
  that held one. A complete bracket of such a NAME still gives its
  `name` ("Ida (Peppi) /?/" gives "Peppi"; decision BP). In a document,
  only a distinct name whose kind the source leaves open: a shortening,
  an initial, a spelling variant or a part of the label is an alias
  alone, and an unmarked nickname use follows the nickname and
  `referred_to_as` rules (decision BQ). "Primary name as a referenced
  statement" (#171) stays deferred.
- **A cue is no type.** A bracket holding a cue or a note ("(geb.
  Laudler)", "(died young)") gives a `name` with its text verbatim: code
  never types a name from its wording (ADR-0035), so "geb." is no stated
  type here; a curator may add the typed statement beside it (decision
  BN).
- **Validation** refuses a `name` claim whose subject is no person, and
  one that restates its subject's label (on the same comparison), as
  counted rejects (decision BR). A subject whose kind or label the batch
  does not give is not checked.
- **Curation.** A curator who identifies the type later adds the typed
  statement (e.g. `nickname`) citing the same source, beside the untyped
  one, which stays (rule 3). A curator may lower its rank.
- **What reads it.** The writer writes it as any monolingual-text claim;
  the privacy check counts it as person evidence, as it does `nickname`,
  and so does the non-privileged export; the run report counts the
  statements by source kind.

### GEDCOM adapter emits what the export marks

Deterministically, never guessing:

- **NAME TYPE** subtag → the matching property (`birth`/`maiden` →
  birth name; `married` → married name; `aka`/`nick` → nickname;
  `official`/`legal`/`change`/`immigrant`/`adopted` → official name); an
  absent or unrecognised TYPE leaves the name a plain label/alias
  (amended by #435 (2026-10-02): and, beyond the label's own form, a
  `name` statement of no type).
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
their label and aliases, as before (amended by #435 (2026-10-02): a
further untyped NAME, or a bracketed part, gives a `name` statement).

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
- **Deliberately still flat:** an *ordinary* untyped name (the label's
  own form) is not promoted to a referenced `name` (P2561) statement
  (amended by #435 (2026-10-02): a further form of no stated type is,
  above) — the label/alias
  index remains its home, to avoid doubling every person's claim count
  for no new information. Recording the principal name as a referenced
  statement is a later option if the unreferenced label ever needs
  provenance (the deferred-on-trigger register, #171).
- **Not built:** structured given/family-name components (P735/P734, the
  GEDCOM GIVN/SURN the export carries) as shared name *items* — heavier
  (mints name items) and lower reconciliation value; deferred (#171). Maiden
  names are only recorded when a source marks them (TYPE maiden); they
  are never *inferred* from gender + marriage, which would be guessing.
