# ADR-0020: Possibly-living is a runtime determination, not a stored flag

Status: **accepted** — implemented (`yizkr.core.privacy`, the reworked
`backup.py` exclusion, the `publish-site` badge, and the review CSV with
its `living` column removed and `warning` renamed `critique_notes`).
Supersedes the living-person mechanism that ingestion originally shipped
(a curated `living person` flag set from a review-CSV column).
Amended by #334 (2026-09-28): births fail closed. Every birth statement a
curator has not deprecated is read, each at the latest year it allows,
and a person is released by birth only when all of them lie at or before
the cutoff (§1, §4).
Amended by #344 (2026-09-29): the public export fails closed. It keeps a
person only once the check has released them: the check assesses every
item with person evidence, read at every rank; the export reads the same
evidence from its own dump and drops every such item the check did not
release, every item that states no type, and every family; and it
refuses to run while the query service, where the check runs, does not
hold the dump's revisions (§4, Ruling 6).
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): a stated
civil status (`civil_status`) is person evidence for the check (ADR-0035
§3.1).
Amended by #363 (2026-09-29): the export is a non-privileged view, not
a public copy (the owner, 2026-09-29). It keeps everything recorded about
a person the check releases, and about every item that is no person,
text included, and leaves out every possibly-living person with the
events they took part in. It drops the extractor's judgement notes and
any line it cannot read, and refuses a dump that holds a sitelink or a
property of a type the model does not use (§4, Ruling 6).

## Context

Privacy is non-negotiable (R7, CLAUDE.md rule 7): the archive holds
living people, and nothing about a possibly-living person may leave the
instance. The first implementation modelled this as a **stored flag** —
a `living person` property (item-valued, `yes`/`no`) written as a curated
claim from a `living` column in the claim-review CSV, with the review CSV
warning when a subject had no death data and recent activity so the
curator would set it. The public export then excluded a person on an
explicit `living person = yes`, and *included* an otherwise-excluded
person on an explicit `living person = no`.

Three problems with the flag:

1. **It is a curation duty with no payoff.** Every possibly-living
   subject raised a warning the reviewer had to act on, one at a time,
   forever — a standing tax on review whose only output was a boolean the
   graph could already infer. The flag encodes no source assertion; it is
   a derived fact stored as if it were data.
2. **It fights the source-first principle (ADR-0002/0003).** Every other
   statement records what a *source asserts*, referenced. "Living" is not
   asserted by any artifact; it is a property of *now* — a function of the
   current date and the absence of a death record. Storing it freezes a
   time-varying inference into the graph, where it silently goes stale
   (a person "living" in 2026 is not in 2060) and where a `living = no`
   override can *force-include* someone with no positive death evidence —
   a foot-gun pointed at privacy.
3. **It missed real death signals.** The exclusion counted only a death
   date or a dated death event, so a person with a *place of burial*, a
   *cause of death*, or an undated death event was still treated as
   possibly living and excluded — the graph proved them dead and the
   filter did not listen.

The correct shape is the one the data already supports: **do not store the
inference; compute it when it matters.** "Possibly living" is only ever
needed at a privacy-sensitive boundary (the public export; a future
internet-facing publish, #163). At that boundary the graph is right there to
query.

## Decision

Remove the stored `living person` flag entirely and make possibly-living
a **runtime determination** from the absence of death evidence, in one
shared place.

### 1. One shared predicate (`yizkr.core.privacy`)

`is_possibly_living(births, has_death_evidence, cutoff_year)` is the
single source of truth. A person is possibly living **unless** the graph
holds positive death evidence, or births that all lie at/beyond the
cutoff (by default `DEFAULT_CUTOFF_YEARS` years, a value #72 settles).
**Conservative: unknown means possibly living, hence excluded.** There
is no override in either direction — the answer is exactly what the
graph's own referenced statements assert.

Amended by #334 (2026-09-28): a birth releases a person only at the
latest year it allows, and only when every birth does. The check first
read one birth sampled from the truthy values and took its stored year,
whatever its precision. So a birth in the 20th century, stored as 1901
(or 1900, its round hundred), counted as that year, before the cutoff;
and a sample could pick an early birth over a later one, or a preferred
early birth hide a normal later one. Now:

- **Every birth statement.** `yizkr.core.privacy.BIRTHS_QUERY` reads
  every `date of birth` statement a curator has not deprecated, through
  its statement node, with its earliest and latest dates. A deprecated
  statement counts for nothing, either way.
- **Its latest year** (`latest_birth_year`). A range its source states
  bounds the birth, as the disagreement report reads a date's span
  (MODELING.md, "Contradictions"), since a range is stored with its first
  date as the value: a latest date's last year, when it states one, and
  none when it states only an earliest date ("after 1850" is open at its
  late end). Otherwise the value's last year at its precision: the year,
  a decade's last year, or a century's or a millennium's as Wikibase
  counts them (1801–1900 is the 19th century), a round hundred being read
  as the first year of the century it opens (1900–1999), the reading that
  allows the later birth. A statement that contradicts itself, whose
  value or an earliest date lies wholly after its latest date, is read
  without that latest date, which fails closed: with an earliest date it
  then allows no latest year.
- **Unknown still means excluded.** A statement allows no latest year
  when a latest date in it cannot be read, or, where no latest date
  bounds it, when its value is unknown, unreadable (one before the common
  era included) or coarser than a millennium, or it states only an
  earliest date. A readable latest date bounds even a statement whose
  value is unknown, unreadable or coarser than a millennium. A statement
  that allows no latest year keeps its person possibly living, as does a
  person with no birth at all.

### 2. Death evidence is any death-presupposing assertion

`has_death_evidence` is true when the person has any of a **date of
death**, **place of death**, **cause of death**, or **place of burial**,
or is a **participant in a `death` event**. Crucially, none of these
requires *both* a date and a place — so a person can be recorded as
**deceased with no time or place of death**: a burial place, a cause of
death, or an even-undated death event each marks them dead. Death is an
event like any other (ADR-0014), so "died, nothing else known" is simply
a death event with the person as participant. This is the affordance that
replaces `living person = no`: to include someone, assert their death the
ordinary source-first way, not by flipping a flag.

### 3. No curation duty

The claim-review CSV loses its `living` column. Nothing about living
status is asked of the reviewer. The `warning` column — which had merged
the (now-deleted) living warning with the extraction critic's concerns —
is renamed **`critique_notes`** to reflect its now-single role (ADR-0013).

### 4. Consumers share the predicate

- **Public export** (`yizkr backup`): `exclusion_report` queries the death
  signals above, reads births with the shared module's query, and calls
  the shared predicate; every triple mentioning an excluded person is
  still dropped. (Amended by #363: the export is a non-privileged view,
  and an excerpt leaves with the statement it attests; see below.)
- **Private static site** (`yizkr publish-site`): includes everyone but
  badges each subject the export would drop ("possibly living — never in
  public exports"), computed at render time with the same births query
  and predicate — the private site therefore shows exactly what the
  public export would remove, derived, never stored. (Amended by #344:
  except a person with no type, whom the export leaves out even when
  released; the site shows no badge. A family is always badged. See
  below.)

Amended by #344 (2026-09-29): the export fails closed on who is a person,
as it does on whether they may be living. The check selected people by
their truthy `human` type, and the export dropped only the people the
check had assessed, so a person the check never selected went into
`public.nt.gz` in full: one whose `human` type a curator deprecated, or
outranked with a preferred other type; one with no type at all; and one
written after the query service last caught up, since the dump comes
from the database while the check reads the query service. Now:

- **Person evidence, at every rank.** `yizkr.core.privacy.PEOPLE_SELECTION`
  selects every item with person evidence, each read through its
  statement node at every rank, deprecated included, since a deprecated
  statement still records that a source said so:
  - an item typed `human`, a type that names a redirect to `human` (a
    class a curator merged into it) included;
  - the subject of a property only a person has: date or place of birth,
    date or place of death, place of burial, cause of death, father,
    mother, spouse, child, sibling, relative, birth name, married name,
    nickname, referred to as (ADR-0011, ADR-0018), sex or gender, number
    of children, occupation, employer, educated at, country of
    citizenship, residence;
  - the value of father, mother, spouse, child, sibling, relative or gave
    refuge to, in a statement or in a qualifier;
  - a participant in an event of a kind that happens to people: a birth,
    a death, a marriage, a migration, military service, a deportation,
    an imprisonment or a release, whatever the rank of the event's type
    (but see the next point).

  None of these is evidence: an official name, which a place or an
  organization can have too; a religion or a membership, which an
  organization shares; a part in an event of any other kind; an
  identity judgement (said to be the same as), a depiction and an
  appellation's perspective; any other qualifier; and the values of
  place, organization, artifact and vocabulary properties. So places,
  artifacts and the vocabulary are not taken for people. An unknown value
  is no item, and a redirect has no statements of its own. A type is read
  through the redirects it names, in the dump and at the query service
  (`ps:P/owl:sameAs*`): a class a curator merged into `human`, an event
  kind or a non-person class types as the one it now redirects to. The
  check and the site select people with it.
- **A non-person type outweighs a part in an event** (owner,
  2026-09-29). A ship or a firm takes part in a migration, so an item
  typed as a vessel, a military unit or an organization
  (`NON_PERSON_CLASSES`), at normal or preferred rank, is not taken for a
  person on its part in a person event alone. Mixed signals fail closed:
  such an item is a person on any other evidence, a `human` type at any
  rank, a person-only property or a person-valued link (a firm with an
  occupation, say). A non-person type a curator deprecated sets nothing
  aside, and a participant with no type is a person. A family is
  deliberately not among these types: its label is a surname and its
  description names its members, and a person known by a surname alone
  can be typed as one. Nor is the unclassified entity, where a mistyped
  person lands.
- **Only released people leave.** The export reads the same evidence
  from its own dump (`yizkr.backup.read_dump`) and drops every item that
  has any of it, in the dump or at the query service, unless the check
  released it (`public_exclusions`). An item the check never assessed is
  dropped, and `public-exclusions.json` says so. A redirect stands for
  the item it redirects to, and is dropped when that item is: every
  triple that names the redirect goes, the link to it from another item's
  statement included, while that statement's own dates and references
  stay, as they do for any excluded value.
- **What states no type is left out** (owner, 2026-09-29). What an item
  that states no type is cannot be told, and a label alone can be a
  living person's name, so the export drops every such item, whatever
  else it holds, a person the check released included
  (`public-exclusions.json`: "no type"). An item states no type when
  none of its `instance of` statements at normal or preferred rank names
  an item: an unknown or no value names none, and a type a curator
  deprecated counts for nothing. The vocabulary is kept: the manifest's
  items, which bootstrap mints untyped, and every item that is the value
  of an `instance of` or `subclass of` statement, at any rank, a class a
  curator makes included, and the class such a value redirects to, so
  that neither the class nor the typing of the items that name it is
  dropped. A redirect goes with its target, as above. Nothing in the
  pipeline is untyped by design: an artifact and a page fragment are
  typed by the registrar, derived media in the same write that creates
  them, an entity the extraction, a GEDCOM file or a face match proposes
  by its kind, and an extraction or place-hierarchy run as a run. A
  write cut short between an item's label and its type, or an extracted
  reference that gives no kind, leaves an item untyped, and dropping it
  is right: it is missing from the public copy until it is typed. So are
  the retired vocabulary (§5) and the smoke test's item.
- **A family is never public** (owner, 2026-09-29). A family is private
  when the people it stands for may include a living person: whether it
  refers to a specific set of people, whether that set is fully known,
  and whether any member may be living. A family's members are the
  people recorded as `member of` it, and the model cannot state that a
  family's membership is complete (MODELING.md: there is no household
  statement enumerating occupants), so today every family is private:
  the export leaves out every item typed `family` at normal or preferred
  rank, its type read through redirects like any other, whatever its
  evidence, released or not (`public-exclusions.json`: "family: its
  membership is not known to be complete, ..."; `FAMILY_CLASS`). A
  redirect to a family goes with it. The full rule, with a way to mark a
  family's membership complete, is #374. `family` stays out of the
  non-person classes above: a family in a person event is still a
  person there, and is left out either way.
- **The query service must hold the dump.** The export reads the
  revision of every item and property from the dump and waits, up to
  `LAG_TIMEOUT`, for the query service to hold exactly those revisions (a
  redirect aside, as the dump gives it a revision and the query service
  need not), then runs the check, and compares again once it has run.
  Properties count as much as items: the check reads a qualifier's
  precision through its property, and an earliest date whose precision
  is unread can release a birth that contradicts itself. If the two
  differ at either point, the export refuses to run (`QueryServiceLag`):
  nothing public is written, and the backup keeps its other dumps and
  records the refusal. The export does not assess people from the dump
  itself, so that the site and the export keep one births query and one
  predicate (Ruling 5). The match is exact (owner, 2026-09-29): an entity
  whose revision never reaches the query service makes every export
  refuse, naming the entity, until that entity is edited again or the
  query service is reloaded from a full dump; after a restore, or a reset
  of the query service alone, a full reload is needed, since the updater
  replays recent changes only.
- **The site lists the same people.** `yizkr publish-site` lists every
  item the selection finds, and badges each one the check does not
  release, and every family, whatever the check says; so for every
  person it lists its badge and the export's exclusion agree, except a
  person with no type, whom the export leaves out even when released (an
  untyped item with a date of death, say): the site shows no badge. The
  badge speaks for the check, which releases them; the missing type is
  the export's own rule, and a badge for it would need a wording of its
  own. The export also drops what the site lists as no person: an item
  that states no type and has no person evidence, a family with no person
  evidence, a redirect, and an event that an item it drops took part in.

The rulings dated 2026-09-29 are the owner's, on the questions this
amendment raised.

Amended by #363 (2026-09-29): the export is a non-privileged view. The
owner decided (2026-09-29): "I don't intend to make a completely public
copy, so any non-privileged view can include all information on
confirmed-deceased people." The export, still written as
`public.nt.gz`, is that view, and what it keeps of the items it keeps
follows:

- **Everything about a released person stays, text included**: their
  labels, descriptions, aliases, names and titles, their statements'
  values, and the excerpts of the references those statements keep. So
  does everything about an item that is no person: a place, an
  organization, an artifact, the vocabulary, an event whose
  participants are all released. A transcription excerpt leaves with the
  statement it attests, and goes with a statement dropped: a reference
  that no statement kept derives from is swept. Accepted consequence:
  free text about a released person, or a document's title, can name a
  possibly living relative, and it is visible in the non-privileged
  view. A public copy would need rules of its own (#163).
- **An event goes with its participants** (#363). An event stands for
  those who took part in it, as a family stands for its people: its
  date and place are theirs, and its label is written after them ("Death
  of" and the person's label, ADR-0021). So the export leaves out every
  item with a `participant` statement, at any rank, whose value is an
  item it leaves out, a redirect to one included, whatever the event's
  kind (`public-exclusions.json`: "a participant the export leaves out
  (the QIDs): what this event records is theirs too";
  `PARTICIPANT_LEFT_OUT`). An event so left out may itself be a
  participant, so the rule applies again until no more go, and a
  redirect to such an event goes with it. An event whose participants
  are all kept stays.
- **The judgement notes never leave** (ADR-0035): the export drops every
  snak of the `judgement` and `judgement_note` properties, in any form,
  a reference's included (`backup.DROPPED_PROPERTIES`). They are the
  extractor's remarks on its own reading, not information on anyone. A
  property the instance's vocabulary does not yet hold drops nothing.
- **The export reads the dump strictly.** A line that is not a triple as
  dumpRdf.php writes one (a subject, a predicate and an object, one space
  apart, and a closing dot) is dropped, whatever it holds, and lines end
  at a newline alone.
- **It refuses a dump it is not made to filter** (`UnfilterableDump`,
  recorded by the backup as a lagging query service is). A sitelink: its
  page's own triples (its name, its language, its site) never name the
  entity it is about, so leaving the entity out would leave its page's
  name behind. Or a property of a type the model does not use, anything
  but an item, a string, a monolingual text, an external identifier, a
  time, a quantity or a globe coordinate (a URL, a media file, a shape),
  whose values the export has not been reviewed for. A unit test holds
  the manifest to the model's types. The unit, calendar and globe of a
  quantity, a time or a coordinate are IRIs that carry no text: Wikibase
  takes each only as an entity's IRI.

### 5. Vocabulary retired

The `living person` property and the `yes`/`no` items it alone used are
removed from `model/manifest.yaml`. Bootstrap is idempotent and additive,
so this does not touch any already-minted items on a live instance; such
legacy statements simply linger as orphaned data (the static site skips
them defensively) and no longer affect the privacy determination — which
is strictly safer, since the determination is now absence-of-death only.

## Rulings

1. **No stored living/deceased flag.** Possibly-living is computed at the
   privacy boundary, never persisted.
2. **Conservative, override-free.** Unknown means excluded; there is no
   `living = yes/no` escape hatch in either direction.
3. **Death is asserted, not flagged.** A date/place/cause of death, a
   burial place, or a death-event participation each marks a person
   deceased; a bare death event needs no date or place.
4. **No reviewer duty.** The review CSV has no `living` column; `warning`
   becomes `critique_notes`.
5. **One predicate, shared.** `yizkr.core.privacy.is_possibly_living` is
   the sole implementation; export and site both call it, with the births
   its one query reads (amended by #334).
6. **Only released people leave.** The public export drops every item
   with person evidence, read at every rank, that the check has not
   released, every item that states no type, and every family (#374
   holds the full rule for families), and refuses to run while the query
   service does not hold the dump's revisions (amended by #344). It is a
   non-privileged view: it keeps everything about released people and
   items that are no person, text included, drops every event that an
   item it drops took part in and the judgement notes, and refuses a dump
   that holds a sitelink or a property of another type than the model's
   (amended by #363).

## Consequences

- Review loses a standing per-subject chore; privacy stops depending on
  curator diligence and can no longer be wrongly overridden.
- The determination self-updates with the calendar and with new death
  evidence — no stale flags, no re-flagging as the cutoff moves.
- The export now honours burial places, causes of death, places of death,
  and undated death events, so genuinely-documented deaths are no longer
  spuriously excluded.
- Existing instances may carry legacy `living person` statements from the
  old flow; they are inert (ignored by the new code, skipped by the site).
  A one-off cleanup can delete them, but nothing requires it.
- The public export can refuse to run (amended by #344): while the graph
  is being written, when the query service lacks entities for good (a
  query service reset that replayed only recent changes), or when an
  entity's revision never reaches it. The refusal names the entities, and
  the backup's other dumps are kept; `yizkr backup` reports it on one
  line and exits non-zero. Pruning counts a refused run, so `--keep N`
  after N refused nights keeps no public export. An item left untyped, by
  a write cut short or by hand, is missing from the public copy until it
  is typed. An item that a person-only statement marks is dropped as a
  person (a place with a nickname, a firm with an occupation), and that
  statement keeps it out at any rank: fail closed, accepted.
- The export is a non-privileged view, not a public copy (amended by
  #363; the owner, 2026-09-29): every text recorded about a released
  person or an item that is no person leaves with it, a source's excerpt
  included, so a text can name a possibly living relative. An event with
  a participant left out goes whole: a marriage of a released person and
  a possibly living one, and, while every family is left out, every
  event a family took part in. An item that is no event but carries a
  `participant` statement goes by the same rule. A dump that holds a
  sitelink or a property of another type than the model's makes the
  export refuse until it is removed: the backup keeps its other dumps and
  records the refusal.
- Docs move with the code: MODELING.md "Living persons and privacy",
  `deploy/README.md`, and `pipeline/README.md` are updated in the same
  change; ADR-0018's exclusion note now points here.
