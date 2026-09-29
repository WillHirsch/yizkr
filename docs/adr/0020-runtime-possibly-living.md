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
release, and every item that states no type; and it refuses to run while
the query service, where the check runs, does not hold the dump's
revisions (§4, Ruling 6).

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
  still dropped, and excerpts still never leave.
- **Private static site** (`yizkr publish-site`): includes everyone but
  badges each subject the export would drop ("possibly living — never in
  public exports"), computed at render time with the same births query
  and predicate — the private site therefore shows exactly what the
  public export would remove, derived, never stored. (Amended by #344:
  except a person with no type, whom the export leaves out even when
  released; the site shows no badge. See below.)

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
  release, so for every person it lists its badge and the export's
  exclusion agree, except a person with no type, whom the export leaves
  out even when released (an untyped item with a date of death, say);
  the site shows no badge. The badge speaks for the check, which releases
  them; the missing type is the export's own rule, and a badge for it
  would need a wording of its own. The export also drops what the site
  lists as no person: an item that states no type and has no person
  evidence, and a redirect.

The rulings dated 2026-09-29 are the owner's, on the questions this
amendment raised.

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
   released, and every item that states no type, and refuses to run
   while the query service does not hold the dump's revisions (amended by
   #344).

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
- Docs move with the code: MODELING.md "Living persons and privacy",
  `deploy/README.md`, and `pipeline/README.md` are updated in the same
  change; ADR-0018's exclusion note now points here.
