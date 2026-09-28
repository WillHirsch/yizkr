# ADR-0021: GEDCOM source citations (SOUR) and multimedia (OBJE)

Status: **accepted** — implemented in the GEDCOM adapter
(`pipeline/gedcom.py`): SOUR citations fold into each claim's reference
(step A) and OBJE objects become un-held media items linked with the new
`image` property. Follows the honest census of the preceding change (issue
#8). Step B (promoting a cited SOUR to its own artifact item) remains
deferred to a future ADR (#168).
Amended by #308 (2026-09-28): the census reads each tag where it stands,
so a divorce's PLAC, a MARR's TYPE and an undated DIV are counted: see
Amendments.
Amended by #322 (2026-09-28): the adapter reads every occurrence of an
event, of a DATE, PLAC or CAUS beneath one where it reads that tag, of a
NAME's TYPE or NICK, and of a SEX or NCHI, so a repeated event or detail gives claims of its own,
and a further HUSB or WIFE is counted: see Amendments.
Amended by #323 (2026-09-28): a DEAT that states the death with no date,
place or cause is a `death` event with the person as participant, while
one that states no death (no Y, and no DATE or PLAC with a value) gives
no claim, not even its cause, and is counted; a family with one partner
or none has its MARR and DIV counted; a NAME without a value is read from
its parts; and a nickname that several NAMEs give is a claim of each: see
Amendments.

## Context

The GEDCOM adapter (`pipeline/gedcom.py`) maps the genealogical content a
family-tree export carries — names, dates, places, parentage, marriages,
occupations, and so on — deterministically into referenced claims. Two
common tag families it did **not** map are `SOUR` (source citations) and
`OBJE` (multimedia objects). They were once "counted" only where they sat
one level under an `INDI`/`FAM` record, which under-reported them badly;
the preceding change's census named the top-most unmapped tag in every
branch, making their true prevalence visible. Both are common in
family-tree exports: a well-sourced tree carries a `SOUR` citation behind
most of its facts, and an `OBJE` for each attached photo, scan or clipping.

That prevalence forces a principled question rather than a parser one,
because of what these tags *are*:

- A **`SOUR` citation** is the upstream source the export's own assertion
  rests on — a birth index (`Volume N, Page M`), a civil registration
  marriage index, a Find a Grave entry — carried as a pointer to a
  level-0 `SOUR` record plus a `PAGE` locator and an Ancestry
  `_APID`. This archive's founding principle (ADR-0002, CLAUDE.md) is that
  it *records what sources assert*. Ingesting the assertions while dropping
  the citations those assertions themselves cite is a standing tension with
  that principle: we keep "Gustav was born 7 Jan 1919" but discard "…per
  the birth index, Vol N p M, as cited by the tree".
- An **`OBJE` object** is a photograph, scan, or clipping — e.g. an
  *"Obituary notice in `<newspaper>`"*, dated and placed, with a
  `newspapers.com` URL. In an Ancestry export the **bytes are remote**: the
  file references media the archive does not hold. Registering media
  (ADR-0004/0010) keys on the SHA-1 of bytes we *have*, and the privacy
  rule (R7, CLAUDE.md rule 7) forbids fetching from third parties without
  explicit instruction. So `OBJE` cannot ride the normal media path, and
  must never trigger a background fetch of Ancestry/newspapers.com content.

Two guard-rails constrain any answer. First, **no auto-confirmation**
(ADR-0007/0010): the pipeline may not silently mint citation or media
*entities* as fact; identity is a reviewed judgement. Second, **every
statement carries a reference** (ADR-0003) — which the adapter already
satisfies by referencing each claim to the GEDCOM artifact itself with the
verbatim lines as excerpt. So the question is not "do these claims have a
reference" (they do) but "should the export's *own* upstream citation and
media pointers be preserved, and in what shape".

## Decision

### 1. `SOUR` → folded into the claim's reference (step A, implemented)

When a mapped claim's GEDCOM record carries a `SOUR` citation, the
adapter's `source()` helper folds it into the claim's existing reference:
the `PAGE` becomes the reference's `page_number` locator (ADR-0003), and
the cited level-0 `SOUR` record's title is resolved from its opaque
`@S..@` pointer and appended to the transcription excerpt (`… | cited:
Adressbuch der Stadt Flörburg 1912`), so the reviewer
reads the source's name, not a pointer. The verbatim `SOUR`/`PAGE` lines
remain in the excerpt. No new entities are minted, so nothing is
auto-confirmed. The upstream source is **resolved, never fetched**.

**Step B (deferred).** Promoting each level-0 `SOUR` record to its own
artifact / artifact-fragment item (a *cited source*, e.g. the birth index),
referenced `stated in →` from every claim that cites it and de-duplicated
like places, is real modeling work — citation identity, dedup, a review
step (never auto-merge). It gets its own ADR + review flow when
prioritised; it is not built here. Tracked in #168.

### 2. `OBJE` → a lightweight un-held media item, never a fetch

Each `OBJE` becomes an entity of kind **`artifact`** (not `photograph`: we
hold none of the bytes and cannot vouch it is a photo — many objects are
document scans — and an un-held stub stays out of the gallery/faces flows,
which key on held media). Its label is the object's `TITL` (else file
name); it carries **no `media file` and no `media SHA-1`** — the absence
of a content hash *is* the un-held marker. The person links to it with the
new **`image`** property (P18 semantics: media of the subject),
`point_in_time`-qualified when the object is dated. Objects are deduped by
xref so several people share one item. `image` is used, not `depicts`,
because `depicts` is the faces-confirmation predicate (ADR-0005) that must
never be auto-written (CLAUDE.md), and an attached obituary is not a facial
depiction.

Referencing is not fetching: this records the `FILE` URL/name and the
metadata the export already holds with **zero bytes held and zero
third-party fetch**. Acquiring the bytes stays a separate,
explicitly-instructed step (#168), and only then does the medium join the
normal SHA-1-keyed artifact path (ADR-0010). This preserves the person↔media
linkage the future faces/gallery flows (ADR-0005) will want, without ever
pulling personal media off Ancestry or newspapers.com on the archive's own
initiative.

## Rulings

1. **`SOUR` step A adopted.** Citations fold into the citing claim's
   reference (`page_number` + resolved title in the excerpt); no new
   entities. Step B (cited-source-as-item) is deferred to its own ADR.
2. **`OBJE` is a lightweight un-held media item** (`artifact` kind, no
   `media file`/`media SHA-1`), linked with `image`, not `depicts`.
3. **Hard line confirmed.** The pipeline **never** fetches `OBJE` bytes
   from a third party without an explicit, separate instruction.
4. Once handled, `SOUR`/`OBJE` leave the unsupported-tag census — it now
   reports only the genuine unmapped tail (`REPO`, `SUBM`, `_`-tags).

## Consequences

- Source-first fidelity improves: the upstream citations a tree carries are
  preserved on the very claims they support, name and locator both.
- Privacy is preserved by construction: `OBJE` is referenced, never
  fetched; no personal media leaves or enters via a background download.
- No auto-confirmation: neither citations nor media become asserted
  *depiction/identity* facts without the ordinary human review
  (ADR-0005/0007).
- A new `image` property joins the manifest (P18 semantics, item-valued);
  bootstrap mints it idempotently on the next run.
- The heavier citation-as-entity modeling (Step B) is named and deferred to
  its own ADR, not smuggled into the adapter.
- Docs move with code: the adapter docstring and MODELING.md
  ("Names"/media) are updated in this change.

## Amendments

Amended by #308 (2026-09-28): **the census reads each tag where it
stands.** The census took a tag as read under every record once the
adapter read it under one, so three things went uncounted, and so were
dropped silently: a PLAC under a DIV (a birth's PLAC is read), a TYPE
under a MARR (a NAME's TYPE is read) and a DIV without a DATE (a dated DIV
is read). The census now holds what the adapter reads beneath each tag it
maps. A tag unread where it stands is counted there, and when the adapter
reads that tag beneath other tags it is named with its parent's tag
(`DIV.PLAC`, `MARR.TYPE`, and likewise `INDI.NICK` for a NICK beside a
NAME rather than beneath it), so that the entry never reads as though
every PLAC or TYPE went unread. A DIV without a DATE is counted as
`DIV`, which stands for its PLAC too. Each is counted rather than mapped,
since the model has no property that fits it:

- a divorce's place is not the spouse statement's `location`: the adapter
  writes a MARR's PLAC there as the wedding's place (`pipeline/gedcom.py`),
  and a second `location` could not say which place was the wedding's and
  which the divorce's. Which property a wedding's place takes is #311's
  question;
- an undated divorce says that the marriage ended at an unknown time,
  which Wikidata states as an end time of unknown value; the claim schema
  and the writer have no unknown value;
- a MARR's TYPE (civil, religious) names a kind of marriage, and the model
  has no property for one.

The spouse claims' verbatim excerpt still shows their lines, within its
400 characters.

Amended by #322 (2026-09-28): **every repeated event and detail is read
or counted.** The census took every occurrence of a tag it descends into
as read, but the adapter read only the first BIRT, DEAT, MARR, DIV, SEX
and NCHI of a record, and the first DATE, PLAC, CAUS, TYPE and NICK
beneath a tag, so the others were dropped silently. Among them was the
alternative date, which GEDCOM states by repeating the event. The adapter
now reads every occurrence of these, and each gives claims of its own,
with its own date, place and circa, so that the alternatives coexist
(ADR-0002):

- a repeated BIRT or DEAT, or a repeated DATE, PLAC or CAUS beneath one,
  gives a date, place or cause claim of its own, and a repeated SEX,
  NCHI, NAME TYPE or NICK a claim of its own. Two that differ coexist, as
  competing counts do (docs/MODELING.md). One that restates another line
  for line, its citation included, gives the same claim, which the batch
  holds once; the same value under another citation is a claim of its
  own in the batch, with that citation's excerpt and locator (CLAUDE.md
  rule 1). The writer writes the two as one statement, with a reference
  block for each citation (#335);
- where one claim holds both an event's date and its place (BURI, RESI,
  OCCU, MARR), each of the event's dates is paired with each of its
  places, and each DATE of an OBJE dates an image claim of its own;
- a family's first marriage (a date with a place) and its first divorce
  date one pair of spouse claims, as a family's only MARR and DIV do, and
  every other marriage or divorce dates a pair of its own (docs/MODELING.md,
  "Marriages, residences and other periods"), save one that restates
  another's date and place: spouse claims cite the family record, so it
  gives no pair of its own, whatever its citation. Pairing only the first
  keeps each MARR's and each DIV's date on one pair of claims, rather than
  pairing every marriage with every divorce;
- what gives nothing to claim (`1 BIRT Y`, `1 MARR Y`, a date the
  grammar cannot parse) adds no claim beside one that does, so it never
  hides one, while an undated MARR that gives a place keeps it on a pair
  of its own. An undated DIV is still counted as `DIV`, as above.

These are mapped rather than counted because each is something the record
states that a claim of the model can hold, and a claim of its own keeps
it apart from the others. A family's HUSB and WIFE are the exception: the
adapter reads a family's first of each alone, since a family has one
husband and one wife, and mapping a second would give the other partner
a second spouse and each child a second parent, which the record cannot
mean. A further one is counted, named with its parent's tag (`FAM.HUSB`,
`FAM.WIFE`), since the adapter reads the first beneath the same tag.

Within the SOUR citations and OBJE objects that the adapter incorporates
wholesale (Decision above), some tags are still read once: a citation's
PAGE, a SOUR record's TITL, and an OBJE's FILE and TITL. The census does
not look inside SOUR or OBJE, so a second one of these goes uncounted;
#324 tracks it.

Amended by #323 (2026-09-28): **an undated death, a family with one
partner, a NAME without a value and a repeated nickname are mapped or
counted.** Each was dropped silently, and a DEAT that asserts no death
gave its cause of death all the same:

- **A DEAT states the death only as GEDCOM 5.5.1 has an event
  asserted, and one that states it and nothing else is a `death`
  event.** GEDCOM 5.5.1's notes on INDIVIDUAL_EVENT_STRUCTURE have an
  event asserted by a DATE or a PLAC with a value, or else by a Y on the
  event's own line, and let a note or a source be attached to it
  "without implying that the event occurred". A DEAT that states the
  death with no date, place or cause, by its Y alone (`1 DEAT Y`) or by
  a DATE the grammar cannot parse, gave no claim. It now gives a `death`
  event with the person as `participant`, the form in which ADR-0020 §2
  and docs/MODELING.md ("Living persons and privacy") state "died,
  nothing else known". The person has one death event, labelled "Death
  of" and the person's label, and each such DEAT gives a participant
  claim on it that cites that DEAT, with its SOUR and PAGE. It does so
  beside what any other DEAT gives, so `1 DEAT Y` beside a dated DEAT
  keeps both the event and the date. A DATE the grammar cannot parse
  states the death only when the DEAT's own line has no value, so that a
  date the adapter cannot read never overrules another value, such as an
  N denying the death. A DATE it parses, or a PLAC, still gives its
  claim whatever that line says, as GEDCOM does not let an N deny what
  they state.

  A death event, like a cause of death, is death evidence (ADR-0020 §2):
  with either, the person is no longer taken as possibly living, so the
  public export includes them and the private site shows no badge. So a
  DEAT that states no death gives no claim at all, and is counted as
  `DEAT`, which stands for its branch. That is a bare DEAT; one whose
  only lines are a SOUR, a NOTE, an AGE, an OBJE, a TYPE, a RESN, a CAUS,
  or an empty DATE, PLAC or CAUS; and one whose own line gives another
  value (an N, or a date written there) with no DATE the grammar parses
  and no PLAC. Its CAUS goes unclaimed too, though the adapter claimed a
  DEAT's CAUS whatever the DEAT held: a cause of death releases the
  person as surely as the event does, and the possibly-living check,
  which reads it as death evidence, is not the adapter's to change. This
  fails closed. A tool may drop the Y once it attaches a citation to a
  death, and such a death then stays unstated, until evidence (a scratch
  run's census, say) shows real deaths lost that way. A PLAC of nothing
  but commas and spaces (`, , ,`, the jurisdictions of a place a tool
  leaves empty) names no place: under any event it gives no place claim
  and no place item, and under a DEAT it states no death. The person's
  summary, the item's description, gives a death date only from a DEAT
  that states the death.
- **A family with one partner or none has its MARR and DIV counted.** A
  family whose HUSB or WIFE is missing, or names no person the adapter
  maps (no INDI record, or a person without a name, below), gives no
  spouse claims, and its MARR and DIV were dropped. They are now
  counted, as `MARR` and `DIV`, each standing for its branch, as an
  undated DIV is (above), rather than mapped. A family's MARR and DIV
  date the spouse statements between its two partners (docs/MODELING.md,
  "Marriages, residences and other periods"). With one partner, no
  statement names the other, and the claim schema has no unknown value
  to name them with: the gap that keeps an undated DIV counted. A
  `marriage` event with the one partner as `participant` could hold the
  MARR's date and place, but was not taken. It would state a family's
  marriage one way with two partners and another way with one. A
  person's second such marriage would share the first's label, and so
  its item, since the writer finds an item by its label. And the DIV
  would still go unmapped, since the model has no divorce event. With
  unknown values, a one-partner family's MARR and DIV would date a
  spouse statement whose value is unknown, as an undated DIV would end
  one.
- **A NAME without a value is read from its parts.** A NAME whose value
  is empty, or `//`, gave no name, while the census took its GIVN and
  SURN as read. A person whose only NAME was such was skipped entirely,
  and a family that named them made the adapter fail, since its claims
  named an entity the batch lacked. A NAME or `_MARNM` of `//` failed it
  too. Such a NAME is now read from its parts: NPFX, GIVN, SPFX, SURN
  and NSFX in the order GEDCOM writes a name, each part's pieces in turn
  (GEDCOM 5.5.1 separates a part's pieces with commas), provided a given
  name or a surname is among them. A GIVN Rudolf and a SURN Löbig give
  "Rudolf Löbig", a label or alias like any NAME's value, which the
  NAME's TYPE types. A prefix or a suffix alone (`Wwe.`) names no one,
  and as a label it would make everyone so named one item, since the
  writer finds an item by its label, so it gives no name. A NAME with a
  value is read from the value, as before, which its parts are taken to
  restate. A person whose first NAME has no value now takes their label
  from that NAME's parts, rather than from a later NAME, which changes
  the label the writer finds their item by. A NAME whose value and parts
  give no name gives none, though its NICKs still give nicknames. A person
  none of whose NAMEs gives a name is skipped, with a warning, as before,
  and so are the family links that name them, where they made the
  adapter fail. A family that names such a person as a partner has one
  partner (above).
- **A nickname that several NAMEs give is a claim of each.** The adapter
  gave a nickname once for each person, so the same nickname given by a
  second NAME, with its own citation, was dropped, citation and all
  (CLAUDE.md rule 1). A nickname is now a claim of each NAME that gives
  it, citing that NAME, as another repeated value is (above). One NAME
  that gives it twice, by a NICK and inline, gives one claim, as a claim
  restated line for line does.
