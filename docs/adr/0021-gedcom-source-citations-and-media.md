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
Superseded by ADR-0040 (2026-10-24): a DEAT that states the death with
no date, place or cause gives a `date_of_death` of unknown value citing
that DEAT, not a `death` event. `1 DEAT Y` gives it, and a DATE that
says the date is unknown gives no claim (owner, 2026-10-24). The other
cases are as ADR-0040's Q4 decides:
- `1 DEAT Y` with `2 DATE ?` gives it;
- a DATE the grammar cannot read gives no claim, counted;
- a PLAC with no word, or of placeholder words only, gives no claim,
  counted.

A BURI follows the same rules. A BURI that states a burial without a
place gives a `burial_place` of unknown value (ADR-0040's Q3).
Amended by #324 and #350 (2026-09-29): a SOUR beneath a MARR or DIV is
folded into the spouse claims it dates or places; a SOUR that no claim
folds, an OBJE that no image claim maps (within a folded citation, a
cited source record or a mapped object too), a level-0 SOUR or OBJE
record that nothing the adapter maps cites, and an object's further FILE
are counted; every PAGE and TITL is read, and a page number holds at
most 400 characters; and a clipped excerpt keeps its citations and the
lines they stand beneath, and ends with a key of its whole text: see
Amendments.
Amended by #408 (2026-09-29): a family's MARR, DIV and ANUL are events of
its union (ADR-0036 §5), whose citations fold into those events' claims;
a divorce's PLAC and a MARR's TYPE naming a ceremony are read, and an
ANUL is read; a couple of two HUSBs or two WIFEs is a union's couple,
but the second is never a parent; the HEAD's LANG gives the texts'
language: see Amendments.
Amended by #86 (2026-10-06): a person's summary, which gives the dates
of their birth and death, is no longer their item's description: the
writer describes every item it makes by its kind and the kind of its
source alone, as "human named in a genealogical report"
(docs/MODELING.md, "General approach"), and the summary stays in the
batch's manifest.
Amended by #353 (2026-10-23): a family's first two HUSB and WIFE lines,
in either combination, are its couple and its children's parents as
their tags state, so two HUSBs are two fathers; a further one is
counted: see Amendments.
Amended by #583 (2026-10-23): an object's own DATE is when it was made,
an `inception` claim on its media item (ADR-0039 §2.2), not a date of a
person's `image` of it: see Amendments.
Amended by #626 (2026-10-24): the OBJE DATE's subject stands under
ADR-0039 §10's general rule, not an adapter exception: see Amendments
(#583's).

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
name), with ` [media object]` at its end when an item registered with a
hash, or a page fragment of one, holds that label, as it never joins
one, or when a person, a place or any other item that is no artifact
holds it, as a portrait titled with its sitter's name never becomes
the person's item (ADR-0010 as amended by #444, 2026-10-02). The
media object's item carries **no `media file` and no `media SHA-1`**
(a person or place whose name an artifact holds is tagged in the same
way, ` [not an artifact]`, one tag for every kind, its name an alias;
amended by #444, 2026-10-03) — the absence
of a content hash *is* the un-held marker. The person links to it with the
new **`image`** property (P18 semantics: media of the subject), undated;
the object's own DATE is its media item's `inception` (amended by #583).
Objects are deduped by
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

The excerpt of the spouse claims that a MARR or a DIV dates still shows
its lines, a TYPE or a PLAC among them, within its 400 characters; an
undated DIV dates no pair, and its lines are in no spouse claim's excerpt
(#324).

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
  places, and each DATE of an OBJE dates an image claim of its own
  (each is now an `inception` claim of its own on the object's media
  item: amended by #583);
- a family's first marriage (a date with a place) and its first divorce
  date one pair of spouse claims, as a family's only MARR and DIV do, and
  every other marriage or divorce dates a pair of its own (docs/MODELING.md,
  "Marriages, residences and other periods"), save one that restates
  another's date and place under the same citations, which gives no pair
  of its own (#324 has one under other citations give a pair of its own,
  since the spouse claims now fold a MARR's and a DIV's citations).
  Pairing only the first
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
wholesale (Decision above), some tags were still read once: a citation's
PAGE, a SOUR record's TITL, and an OBJE's FILE and TITL. The census did
not look inside SOUR or OBJE, so a second one of these went uncounted.
#324 reads each PAGE and TITL, and counts a further FILE (below).

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
  summary (the item's description until #86, 2026-10-06) gives a death
  date only from a DEAT that states the death.

  Superseded by ADR-0040, 2026-10-24: "one that states it and nothing
  else is a `death` event" and "It now gives a `death` event with the
  person as `participant`" no longer hold.
  - `1 DEAT Y` gives a `date_of_death` of unknown value on the person,
    beside what any other DEAT gives. It is cited to that DEAT with its
    SOUR and PAGE, and the DEAT's lines are its excerpt.
  - There is no death event and no "Death of" label.
  - A DATE that says the date is unknown gives no claim (owner,
    2026-10-24).
  - The other cases are as ADR-0040's Q4 decides: `1 DEAT Y` with `2
    DATE ?` gives the unknown value; a DATE the grammar cannot parse,
    and a PLAC with no word or of placeholder words only, give no
    claim, counted.

  The rest stands: a DEAT that states no death gives no claim, its CAUS
  included.
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
  its item, since the writer found an item by its label (amended by
  #109, 2026-10-06: the writer finds it through the identity map, where
  the two are two mentions). And the DIV
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
  writer found an item by its label (amended by #109, 2026-10-06: it
  would give them one name, on which identity keys), so it gives no
  name. A NAME with a
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

Amended by #324 and #350 (2026-09-29): **every citation and object is
folded, mapped or counted, and a clipped excerpt keeps citations apart.**
The census skipped every SOUR and OBJE, wherever it stood, as
incorporated wholesale, while the adapter folded only the SOURs directly
beneath the record a claim cites and mapped only a person's own OBJE, so
the rest were dropped silently. And an excerpt was cut at its end to its
400 characters, which could cut off the lines that told two citations
apart:

- **A SOUR beneath a MARR or a DIV is folded into the spouse claims it
  gives.** A family's spouse claims cite the family record, whose own
  SOURs they folded, but a MARR's or a DIV's they did not. Each pair of
  spouse claims now folds the citations of the MARRs and DIVs that give
  it: those that give its start and its end, and, when no MARR or DIV
  dates or places the family's one pair, each MARR (`1 MARR Y` with a
  SOUR beneath it). Of the family record, a pair's excerpt holds the
  lines that give it: the family's own line, its first HUSB and WIFE, its
  own SOURs, and those MARRs and DIVs, each with every line beneath it.
  It held the whole record, so every pair of a family held every MARR's
  and DIV's citations, and two pairs dated alike by two citations whose
  titles and pages agree would have been one claim. The family's parent
  claims hold the whole record, as before, and fold only its own
  citations: a MARR's citation attests the marriage, not the children's
  parents, and stands in their excerpt as a line of the record, with no
  page number and no cited title.
- **A MARR or DIV that restates another's date and place under other
  citations gives a pair of its own.** The #322 amendment had it give
  none, whatever its citation, as the spouse claims cited the family
  record alone. Now that they fold a MARR's citations, a second citation
  of the same wedding gives a pair of its own, citing it and dated by its
  own start, as every marriage but the first is, so that the citation is
  kept (CLAUDE.md rule 1), as the same value under another citation is a
  claim of its own elsewhere. One that restates another under the same
  citations, line for line, gives none, and its lines join the other's
  pair; the same source at another page is another citation.
- **A person's OBJE link that holds lines of its own**, such as a SOUR
  citing the link, gives them ahead of the object's in the image claim's
  excerpt, and its SOURs are folded into the claim, as the object's own
  are. A bare link gives the object's lines alone, as before.
- **The census counts every SOUR that no claim folds and every OBJE that
  no image claim maps.** It runs once the claims are made, and skips a
  SOUR only where a claim's reference folds it, and an OBJE only where an
  image claim maps it: a person's link, and the object it points to. Any
  other is counted where it stands, named with its parent's tag, since
  the adapter folds a SOUR beneath other tags:
  - a SOUR directly beneath an INDI (`INDI.SOUR`): the person's own
    citation, which no one claim holds;
  - a SOUR beneath a DATE, PLAC, HUSB, WIFE, CHIL, FAMC or FAMS
    (`DATE.SOUR`, `CHIL.SOUR`), where GEDCOM 5.5.1 gives no citation;
  - a SOUR beneath an event or a fact that gives no claim: `BIRT.SOUR`
    beneath `1 BIRT Y`; `NAME.SOUR` beneath a NAME that gives no claim,
    such as one with no nickname and no TYPE, or a TYPE the adapter maps
    to no property, which gives a label alone;
    `MARR.SOUR` beneath a MARR that neither dates nor places the wedding
    when another MARR or a DIV dates or places the family's pairs;
    `DIV.SOUR` beneath a DIV whose date the grammar cannot parse. A person
    the adapter skips, having no name, gives no claim, so their citations
    and objects are counted too;
  - an OBJE beneath a FAM or an event (`FAM.OBJE`, `DEAT.OBJE` beneath
    `1 DEAT Y`). A person's link to an object the export lacks, which the
    adapter warns of, is counted as `OBJE`, as is a level-0 OBJE record
    that no image claim cites;
  - a level-0 SOUR record that no folded citation cites, counted as
    `SOUR`, beside any counted citation of it;
  - within what the census takes whole, a citation it folds, a source
    record a folded citation cites and an object it maps, a SOUR folded
    into no claim and an OBJE mapped to no media item: `SOUR.OBJE` for a
    scan beneath a citation or a source record, `FILE.SOUR` or
    `NOTE.SOUR` for a citation beneath an object's FILE or a NOTE of a
    person's link. Its line stands in an excerpt, or in none, and no
    claim holds what it cites or shows.

  A counted tag stands for its branch, as ever, so a DEAT that states no
  death, an undated DIV and a one-partner family's MARR stand for the
  SOURs beneath them. Ruling 4 holds where a SOUR is folded or an OBJE
  mapped. Each of these is counted rather than folded, since no claim
  states just what it cites: the person's own citation is the tree's
  source for the person as a whole, and folding it into each of their
  claims would cite it for facts it need not state. A citation of a DATE
  or a PLAC cites that detail alone, and folding it into the event's
  claims would cite it for the event's other claims, a place claim for a
  date's citation. A citation of a HUSB, WIFE, CHIL or FAMC line may cite
  the link or the person it names, which GEDCOM does not say. A MARR that
  neither dates nor places the wedding, beside a pair another dates or
  places, attests the marriage and not its date or place: folded into
  that pair, its citation would be cited for what it does not give, and
  a pair of its own would state the marriage twice (#322). Promoting
  cited sources to items of their own (step B, #168) would carry each.
- **Every PAGE and TITL is read, and a further FILE is counted.** Each
  PAGE of a citation is a locator of the reference's page number, joined
  with ` ; ` as the pages of several citations are, each distinct page
  once and in NFC, as the instance stores it; an empty PAGE is none. A
  page number, a Wikibase string, holds at most 400 characters, and past
  them the write fails: a longer one keeps the pages that fit, cut back
  to a whole page where it can, and ends with ` ; …`. Two claims whose
  pages differ past the cut stay two, as their excerpts give the
  citations whole, or their clip's key (below). Each distinct TITL of a
  source record names it, joined with ` / `: GEDCOM gives a source one
  title, and a record that gives two names one source two ways. An
  object's media item stands for its first FILE. GEDCOM 5.5.1 allows
  several, grouping files of one context, such as a photograph and a
  sound clip, and a further FILE is another medium, which the adapter
  maps to no item of its own, so it is counted, as `OBJE.FILE`, standing
  for its FORM and TITL. Each TITL of the first FILE and of the object is
  read: the first labels the item, as before, and each other distinct
  one is an alias of it, as a person's further NAME is.
- **A clipped excerpt keeps its citations, and a key of its whole text.**
  An excerpt is given in NFC, the form the instance stores text in and in
  which the writer counts its own cap (#335), and holds at most 400
  characters, as a Wikibase text value does. A longer one was cut at its
  end, so a citation's lines and its resolved title could be cut off,
  and two citations of one fact with no PAGE, beneath one long NOTE, were
  one claim, the second citation lost. Now a longer excerpt keeps every
  line of the citations folded into its claim, and their titles; then,
  where it fits, the line each citation stands beneath, so that it is
  read as that line's (a DIV's citation, not the MARR's before it); then
  each of its other lines, in order, that still fits, so that a line
  after one left out is kept where it fits. It marks each run it leaves
  out with `…`. Its first line, which names its record, is cut to the
  room left when its turn comes and it does not fit whole, and ends with
  `…`, provided the cut keeps its level and tag and something of its
  value: a long occupation keeps its start, as the clip before this
  change kept it. When even the citations do not fit whole, it keeps as
  many of all its lines, from the first, as fit, cut back to a whole line
  where it can.
  Either way it ends with `clipped, sha1` and the first 12 hex digits of
  the SHA-1 of the whole excerpt, so that clipping never makes one claim
  of two that the whole excerpts keep apart: two citations of one fact,
  or two places of the export that differ only past the clip, each of
  which the writer keeps as a reference block of its own (#335). A claim
  restated line for line is still one.

The example fixture gives the same 127 claims, each with its one
reference, and the same census: it carries no citation, its one object
is linked from people alone, and no excerpt of it reaches 400
characters, so no clip applies. Only the excerpts of the eight spouse
claims whose families list children change, losing their CHIL lines.
The live GEDCOM test's batch changes too, each of its two pairs of
spouse claims holding the lines of its own MARR and DIV alone, and its
first MARR now cites a marriage register at a page, which the spouse
statements it dates carry; its marker changes with it.

Amended by #408 (2026-09-29): **a family is a union, and its events are
items.** ADR-0036 §5 makes a coupled family a `marriage` (a MARR that
states a wedding, or a DIV or ANUL that states its event, a marriage
with no wedding) or a `partnership`, started by its wedding and ended
by its divorce or annulment, and the adapter writes no spouse claim. So
what #308, #323 and #324 say of the spouse claims is superseded:

- a SOUR beneath a MARR, a DIV or an ANUL is folded into that event's
  claims and the union's claim that points to it, and the family
  record's own SOURs into its couple's participant claims; each such
  claim's excerpt holds the family's own line, its HUSB and WIFE, and
  the lines of the record that gives it;
- a divorce's PLAC is the divorce's `location`, no longer counted as
  `DIV.PLAC`; a MARR's TYPE that names a civil or a religious ceremony
  gives the wedding's kind, and any other is still counted as
  `MARR.TYPE`; an ANUL is read as an annulment;
- a MARR, a DIV or an ANUL states its event as a DEAT does, by a Y, or a
  DATE or PLAC with a value: a `DIV Y` is an undated divorce, and one
  that states none (`MARR N`, a bare DIV) is counted as MARR, DIV or
  ANUL;
- a family with no WIFE and two HUSBs, or no HUSB and two WIFEs, has
  those two as its couple, so its second HUSB or WIFE is read as a
  partner; a further one is still counted. Decided by the orchestrator
  (2026-10-02; the owner may override): the second is never the
  children's parent, which the first HUSB and first WIFE alone are,
  since mapping a second "would give each child a second parent, which
  the record cannot mean" (#322 above); and a family whose HUSB and WIFE
  name one person gives no parent claim, counted (ADR-0036);
- the HEAD's LANG gives the language of every excerpt and name the
  adapter writes (ADR-0035 §6), `und` where the export states none the
  adapter knows.

Amended by #353 (2026-10-23; owner decision): **a family's first two
partner lines are its couple and its children's parents.** The #322
reading above (a family's first HUSB and first WIFE alone) and the #408
line that the second of two HUSBs or two WIFEs is never a parent are
superseded: GEDCOM 5.5.1 exports write a same-sex couple as two HUSBs or
two WIFEs, where a second father or a second mother is what the record
means. The adapter reads a family's first two HUSB and WIFE lines, in
whatever combination and order, as its couple, a HUSB before a WIFE
(ADR-0036 §5), and each
gives the children a `father` (HUSB) or a `mother` (WIFE) claim. A third
is counted as `FAM.HUSB` or `FAM.WIFE`, not read; a line that restates
one before it (same tag, same person) is counted and passed over; a
family whose two
partner lines are a HUSB and a WIFE naming one person still gives no
union and no parent claim, counted.

Amended by #583 (2026-10-23): **an object's own DATE is its
inception.** An OBJE's DATE says when the photograph or document the
export describes was made, which ADR-0039 §2.2 makes a source's
`inception`; it is not a date of the person's association with the
medium. Each DATE of an object is an `inception` claim on the object's
media item, its value and the range and circa its phrase states as
`parse_gedcom_date` reads them, at the GEDCOM's precision, never
narrowed (CLAUDE.md rule 5). The claim cites the object: its excerpt is
the object's lines alone and the object's own citations fold into it,
so the several people who link one object give one claim; a citation
beneath a person's OBJE link folds into that person's `image` claim
only, which is undated. A DATE the grammar cannot parse gives no claim
and a warning (`unparsed media date … — inception omitted`), given once
however many people link the object. ADR-0039 §2.2's subject rule admits
the media item as the subject as an interim exception keyed on the
adapter's model identifier (`STRUCTURED_ADAPTERS`, owner decision
2026-10-24): a model's batch never gives an artifact entity a
document-subject claim, pending an ADR-0039 amendment the owner has asked
for.

Amended by #626 (2026-10-24): **the OBJE DATE's subject stands under the
general rule.** ADR-0039 §10 lets a source state a document-subject
property about the artifact its own structured record makes, cited to
itself, where its passage conveys the fact; an OBJE's DATE stands in the
object's own record, so it conveys that object's date of making
(ADR-0039 §10.4). The interim exception keyed on the adapter's model
identifier (`STRUCTURED_ADAPTERS`) is replaced, once the build that
#626's draft names lands: the subject rule keys on the subject's kind,
an artifact kind, and an extraction may not name an artifact kind, so a model's batch still makes no artifact entity
(ADR-0039 §10.1(b), §10.6). `ADAPTER_IDENTIFIER` stays the batch's
model identifier, and the export's claims do not change. The un-held
media item is not the held scan of the same photograph: the fixture's
`@O1@` and the universe's `studio-photo-1890s` stay two items until a
curator links them (ADR-0010 §2–3, deferred; #162), so the export's
`inception` meets the scan's own only after that link.
