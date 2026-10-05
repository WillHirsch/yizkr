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
a person the check releases, and about the other items it keeps (places,
organizations, artifacts, the vocabulary, and events all of whose
participants it keeps), text included. It leaves out every
possibly-living person, family and untyped item, as before, and with
them the events they took part in, every person event that names no
participant, and the events that start and end a union it leaves out.
It drops the extractor's judgement notes and any line it cannot read,
and refuses a dump that holds a sitelink or a property of a type the
model does not use (§4, Ruling 6).
Amended by #364 (2026-09-29): a statement whose value, or any
qualifier's value, is an item the export leaves out is left out whole:
its node, rank, qualifiers (a wedding's date among them) and references
(one a kept statement cites too stays with that one), and the truthy
triple it gave (for a statement of unknown value, every truthy triple of
unknown value of its item and property), even on an item the export
keeps.
An item named in a qualifier of an event's `participant` statement,
whatever the statement's value, an unknown one or none included, takes
part in that event for the event rule, without being person evidence or
attending a person event on that alone (§4, Ruling 6).
Amended by #364, orchestrator decisions (2026-10-02, privacy first, on
the questions the review of #364 raised): an event whose participants
are all of unknown value is left out, failing closed, as ADR-0036 §6
does for unions; "the events involving them" covers any link, so an
event that names an item the export leaves out as the value, or a
qualifier's value, of any of its statements is left out whole, through
the same fixpoint; a union (ADR-0036) is never an event for this rule:
its events go with it, not it with them; and the private site keeps its
badge on the person named, without marking the statements the export
drops whole, which the site paragraphs below list (§4, Ruling 6).
Amended by #364, orchestrator decisions on the re-review (2026-10-02):
"participants all of unknown value" means at least one `participant`
statement, every one of unknown value (or none); an event of no person
kind with no `participant` statement at all names no one and stays,
unless the link rule takes it, so impersonal events (a fire, a hearing
that names only places or organizations) stay. The link rule follows
only items left out to protect people: a possibly-living person, and an
item left out as one that may be a person (an organization with person
evidence, say). An item left out only as untyped, or as a family, takes
no event with it; the statement naming it still goes whole, so a
released person's birth at an untyped place, or a released person's
migration that a family carried out, stays without that statement. An
untyped item or a family that the participant or unattended rule would
also leave out, that may be a person (read through redirects), or a
statement of which names an item left out to protect people, is not
left out only as untyped or as a family. And an item typed human, at
normal or preferred rank, is never an event for the participant, link or
unattended rule (§4).
Amended by #364, orchestrator decision J (2026-10-02, privacy first;
corrected the same day, unknown means excluded): a family protects
people, and so takes with it an event that names it, unless it names at
least one member and every member it names is released. Its members are
read from the dump: the values of the family's own `participant`
statements (never what their qualifiers name, a role or a place), and
the items its `member of` statements name it, through redirects, at any
rank. So "Departure of the Lobigs, 1975", carried out by a family with a
possibly-living member, or with no recorded member, whose members cannot
be confirmed released, goes whole; a family that names members, all of
them released, is left out only as a family and takes no event with it,
as decision I says. A family stays a family, a `human` type beside its
`family` type notwithstanding (§4).
Amended by #364, orchestrator decisions Q and R (2026-10-02): an item
that anyone names with `member of` is a group for decision J, whatever
its type or whether it is typed at all, so stating less type information
never protects less; an untyped organization with a possibly-living
member takes events with it, an over-drop accepted, failing closed (Q).
And J applies to the participant rule too: a group that names at least
one member, all of them released, is a harmless participant, like a
released person, so "Emigration of the Lobigs" with Gustav and such a
family as participants stays; a group with a member not released, or
with no recorded member, still takes the event (`PARTICIPANT_LEFT_OUT`),
and a person kind of event (a migration, a death, any person kind) is
its members' own, so a group taking part takes it with it even when the
check released every member, the group itself included (R, corrected
the same day: such a group is not a person the check never assessed,
as first written, as the check can release it) (§4).
Amended by #364, orchestrator decisions U and V (2026-10-02, privacy
first): a group with a `participant` statement of unknown value counts
as not confirmed released, so it takes events with it and is never a
harmless participant: Gustav and an unknown participant shield (U). And
a group's members are read through the groups among them, transitively,
through redirects, ending on cycles: a group whose members, so read,
include at least one person, every one released, counts as released;
a member group with a member not released, or of unknown value, makes
the outer group take events with it (V) (§4).
Amended by #364, orchestrator decisions AA to AE (2026-10-02, privacy
first, on the fifth to seventh re-reviews' owner questions): J
holds at every level of the walk, so a member group that names no one,
has a participant of unknown value, or has person evidence and was not
released by the check keeps the outer group unreleased; and a `member
of` may name a person (a household recorded under its head), who is
then a member whose standing counts, and whose own members are read too,
so Florence recorded under Gustav keeps the Lobigs unreleased; a person
is never a released group (AA). A class the dump makes a subclass of a
person kind of event, at any rank and through redirects, is a person
kind everywhere in the export's reading of the dump (the check's own
selection of people does not follow such classes, so a participant of
one that the dump alone makes a person is left out as not assessed,
failing closed) (AB). A group named by any statement of a
person kind of event takes it, released or not, as a group taking part
does (R): "Emigration of the Lobigs" carried out by the Lobigs goes,
whether or not their members are all released. This narrows decision
I's example: a released person's migration that a family carried out
now goes; an event of no person kind that a released group carried out
still stays, without the statement naming it (AC, confirmed on the
sixth re-review). An untyped item with members is judged as a group by
J, on its members: its own lack of a type makes it no possible person
whose standing counts, though it is left out as untyped all the same
(AD). A released person is judged on their own standing alone: an event
attributed to a released household head, Gustav, stays, even with
Florence, who may be living, recorded under Gustav, as the export keeps
everything on released people; only a group (a family, or an item
someone names a `member of`) is judged by its members (AE) (§4).
Amended by #412 (2026-10-02): the export's names say what it is, a
non-privileged view. It writes `nonprivileged.nt.gz` and
`nonprivileged-exclusions.json` (not `public.nt.gz` and
`public-exclusions.json`); the code is `export_nonprivileged`,
`filter_nonprivileged_export`, `nonprivileged_exclusions` and
`NonprivilegedExportRefused`, the base of `QueryServiceLag` and
`UnfilterableDump`, and the old Python names resolve with a
DeprecationWarning until the keeper run (#159), then go. The backup's
manifest records the export as `nonprivileged_export` and a refusal as
`nonprivileged_export_refused`; `--no-nonprivileged-export` skips it,
with `--no-public-export` an alias until the keeper run (#159), which
says on one stderr line that it is renamed. Backups made earlier keep
their files as named. The site's badge reads "possibly living — never
in the non-privileged export". Every refusal, `UnfilterableDump` as
well as `QueryServiceLag`, makes `yizkr backup` print the dumps it kept
and the reason on one line and exit 1, with no traceback; any other
failure (a dump command failing, say) propagates as before and is never
reported as a refusal. The export writes each of its two files under
its name plus `.partial` and moves it onto its name once whole, the
exclusions first, so `nonprivileged.nt.gz` never exists cut short or
without its exclusions; it removes any `.partial` file an earlier run
left before anything else, refusals included, and a failure removes
what it left, and once the exclusions were moved, them and any dump
under its name. A live test
(`tests/integration/test_live_nonprivileged_export.py`) runs the query
service's own munger (wikibase/wdqs:2, no network) over the export and
the full dump, and requires that it logs no error and drops no entity
(§4). The body's earlier names stand as written.
Amended by #412 (2026-10-02): the export keeps what the query service
can load. Its munger reads the dump entity by entity and keeps a value
or reference node only in the block of an entity whose lines name it;
dumpRdf.php writes each such shared node once, in the first entity's
block that uses it, so a node a kept statement shares with one left out
could sit in a block that does not name it, and the munger would drop
it from the kept statement. The filter leaves out exactly what it left
out before, and moves each kept node, once, all its lines together, to
the end of the first kept block that names it (a value node that only a
reference names goes with the reference). Unit tests check every
export they write against an offline mirror of the munger's blocks, and
so does the live test, before the munger runs.

Amended by #437 (2026-10-05): the check's cutoff year, and the backup
run's directory name, are read from one clock (`now`, the system's UTC
clock by default), which tests set. A clock that gives a time with no
time zone is refused. A run's directory is named by the UTC second it
starts, or, when that would not sort after every run already there (a
second run in one second, or a clock set back), by the second after the
latest. So two runs never share a directory, and `--keep N` never
prunes the run just made.

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
  and an excerpt leaves with the statement it attests; see below.
  Amended by #364: a statement naming an item left out, as its value or
  a qualifier's, goes whole, and so does an event that any of its
  statements links to an item left out.)
- **Private static site** (`yizkr publish-site`): includes everyone but
  badges each subject the export would drop ("possibly living — never in
  public exports"), computed at render time with the same births query
  and predicate — the private site therefore shows exactly whom the
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
  evidence, a redirect, an event that an item it drops took part in or
  that any of its statements links to such an item, a person event that
  names no participant, an event whose participants are all of unknown
  value, an event that starts or ends a union it drops, and a statement
  of an item it keeps that names one it drops, as its value or a
  qualifier's (amended by #364; the orchestrator, 2026-10-02: the site
  badges the person named, not the statement).

The rulings dated 2026-09-29 are the owner's, on the questions this
amendment raised.

Amended by #363 (2026-09-29): the export is a non-privileged view. The
owner decided (2026-09-29): "I don't intend to make a completely public
copy, so any non-privileged view can include all information on
confirmed-deceased people." The export, still written as
`public.nt.gz`, is that view. The rule below was presented to the owner
on 2026-09-29 with its consequence: items that are not people are
included, and a document's title, or a text about a deceased person, can
name a living relative. The owner raised no objection. What the export
keeps of the items it keeps follows:

- **Everything about a released person stays, text included**: their
  labels, descriptions, aliases, names and titles, their statements'
  values, and the excerpts of the references those statements keep. So
  does everything about the other items it keeps: a place, an
  organization, an artifact, the vocabulary, an event whose
  participants it all keeps. Families and untyped items stay out, as
  above. A transcription excerpt leaves with the statement it attests,
  and goes with a statement dropped: a reference that no statement kept
  derives from is swept. The consequence: free text about a released
  person, or a document's title, can name a possibly living relative,
  and it is visible in the non-privileged view. A public copy would need
  rules of its own (#163).
- **A statement that names an item left out goes whole** (#364). A
  statement whose value, or any qualifier's value, is an item the export
  leaves out (a redirect to one included), at any rank, is left out with
  its node, its rank, its qualifiers and its references, on an item the
  export keeps too: a released person's spouse statement naming a
  possibly living partner takes its start time, the wedding's date, and
  its excerpt with it, and an appellation whose `perspective` is a
  person left out goes, its text included. A statement's node carries
  its subject's QID, not its value's, so these are read apart
  (`backup.statements_left_out`), a property's statements as an item's.
  A reference or value node that a kept statement uses too stays with
  that statement. A truthy triple goes with the statement that gave it,
  unless a kept statement of the same item and property, at normal or
  preferred rank, holds the same value ("no value" counts as one). An
  unknown value is a blank node (or a skolem IRI) of its own
  statement's, and the truthy triple's is another, so which truthy
  triple of unknown value a statement gave is not read from the dump
  (the export does not reproduce Wikibase's blank-node labels): when a
  statement of unknown value goes, every truthy triple of unknown value
  of that item and property goes with it, a kept statement's included,
  whose node stays. A deprecated statement gives no truthy triple, so
  one that goes takes none. An item named only in a reference's snak
  of a kept statement loses that snak alone, and
  `public-exclusions.json` lists items only.
- **An event goes with its participants** (#363). An event stands for
  those who took part in it, as a family stands for its people: its
  date and place are theirs, and its label is written after them ("Death
  of" and the person's label, ADR-0021). So the export leaves out every
  item with a `participant` statement, at any rank, whose value is an
  item it leaves out, a redirect to one included, whatever the event's
  kind (`public-exclusions.json`: "a participant the export leaves out
  (the QIDs): what this event records is theirs too";
  `PARTICIPANT_LEFT_OUT`). An item that a qualifier of a `participant`
  statement names takes part as much, whatever the statement's value,
  an unknown one or none included (#364). A released group (a family,
  or any item named a `member of`, that names at least one member, read
  through the groups among its members, every one released, and no
  participant of unknown value; decisions J, Q, U and V) takes part
  harmlessly, as a released person does, except in a person kind of
  event, which is its members' own (decision R, as corrected). Each
  group so read must name a member, have no participant of unknown
  value, and, if it has person evidence, have been released; a person
  named a `member of` is a member whose standing counts, and whose own
  members are read too, and never a released group (decision AA). A
  person kind is one of `PERSON_EVENTS` or a class the dump makes a
  subclass of one (decision AB). And an event goes with
  every item any of its statements names, as its value or a qualifier's,
  whatever the property, at any rank: a divorce carried out by an
  organization the export leaves out for its person evidence goes with
  it (`public-exclusions.json`: "a statement of it names an item the
  export leaves out (the QIDs): what this event records is theirs too";
  `LINKED_LEFT_OUT`; the orchestrator, 2026-10-02). It follows only
  items left out to protect people, a possibly-living person or an item
  that may be one: an item left out only as untyped, or as a released
  group (decision J, as corrected), takes no event with it, though the
  statement naming it goes whole; but a released group takes a person
  kind of event that names it (decision AC). An untyped item or a family
  that may be a person (read through redirects), that the participant
  or unattended rule leaves out too, or a statement of which names an
  item left out to protect people, is not left out only so; nor is an
  item named a `member of` that is not a released group (decision Q).
  An event is an item typed `event`, a subclass of it (the manifest's, or
  the dump's at any rank), or a person kind of event, at any rank, or an
  item with a `participant` statement; but never a union (a partnership
  or a marriage, or a class the dump makes a subclass of one at any
  rank, ADR-0036),
  an entity whose events go with it and not it with them: a released
  couple's marriage whose wedding goes stays, without the statement
  naming the wedding. Nor is an item typed human, at normal or preferred
  rank, unless typed a family too, an event for the participant, link or
  unattended rule. An event so left out
  may itself be a participant, or an item an event names, so the rule
  applies again until no more go, and a redirect to such an event goes
  with it. An event all of whose participants, and all the items its
  statements name, are kept stays.
- **A person event that names no participant goes** (#363; unknown means
  excluded, Ruling 2). An event of a person kind (`PERSON_EVENTS`) with
  no `participant` statement, or only one of unknown value, as a claim
  refused, left unconfirmed or cut short after the type leaves it,
  records a birth, a death or a marriage of someone unknown, who may be
  living. So does an event of any kind that has `participant`
  statements, every one of an unknown value (or none): whom it involved
  is unknown, so
  it fails closed, as ADR-0036 §6 does for unions (the orchestrator,
  2026-10-02; `public-exclusions.json`: "an event that names no
  participant (of a person kind), or whose participants are all of
  unknown value: unknown means excluded"; `UNATTENDED`). An event of no
  person kind with no `participant` statement at all names no one and
  stays, unless a link takes it. What a
  qualifier of such a statement names, the role it gives (`principal`)
  or an item it says took part, is no person evidence and attends no
  event on that alone (#364). Once #385's participant roles land, the
  stronger form is that a person event whose principal the export does
  not keep goes; that belongs to #410.
- **A union's bounds go with it** (#363; ADR-0036 §6). An event that a
  union the export leaves out points to by `started_by` or `ended_by`,
  read through redirects and at any rank, goes too: its wedding, divorce
  or annulment, with its label and date, is the couple's
  (`public-exclusions.json`: "it starts or ends a union the export
  leaves out (the union's QID): it is the couple's"; `BOUND_LEFT_OUT`).
  A partner's own `death` event follows the person rule instead
  (ADR-0036 §6). The keys land with #394 (`backup.UNION_BOUNDS`); until
  then nothing is read. The rest of §6 is #410's: a union that names only
  one partner is withheld too, and with it its events; until then, a
  union is left out by the event rule, when one of the partners it names
  is left out, or as a person event that names no participant.
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
   non-privileged view: it keeps everything about released people and the
   other items it keeps, text included; it drops every event that an item
   it drops took part in, every person event that names no participant,
   the events that start and end a union it drops, and the judgement
   notes; and it refuses a dump that holds a sitelink or a property of
   another type than the model's (amended by #363). A statement whose
   value or a qualifier's value is an item it drops goes whole; an item a
   participant statement's qualifier names takes part in the event; an
   event goes with every item its statements name; and an event whose
   participants are all of unknown value goes (amended by #364).

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
  person or another item it keeps leaves with it, a source's excerpt
  included, so a text can name a possibly living relative. A person
  event that names no participant is left out, whatever else it
  records. An event with a participant left out goes whole: a marriage
  of a released person and a possibly living one, and, while every
  family is left out, every event a family took part in. An item that
  is no event but carries a `participant` statement goes by the same
  rule. A dump that holds a
  sitelink or a property of another type than the model's makes the
  export refuse until it is removed: the backup keeps its other dumps and
  records the refusal.
- A released person's statement that names an item left out goes whole
  (amended by #364): their spouse statement naming a possibly living
  partner leaves out its dates, qualifiers and references too, so the
  statement no longer shows the partner or the wedding's date, though an
  excerpt that a kept statement also cites, or a description, can still
  name them (#363). A kept item may so lose a statement whose own value
  is kept: an appellation whose perspective is a possibly living person,
  or a type whose qualifier names an untyped item, and with it the
  truthy triple, unless another kept statement of the same item and
  property, at normal or preferred rank, gives the same value.
  While every family is left out, a released person's `member of`
  statement naming a family goes whole too, with its dates and
  references, and so does an appellation from a family's perspective. A
  statement of unknown value that goes takes every truthy triple of
  unknown value of its item and property, a kept statement's included,
  whose node stays. An event goes when a participant it leaves out is
  named only in a qualifier of a `participant` statement, as an identity
  judgement on a participant of unknown value names one; when another
  statement of it names an item left out (a divorce carried out by an
  organization left out); and when its participants are all of unknown
  value, whatever its kind.
- Docs move with the code: MODELING.md "Living persons and privacy",
  `deploy/README.md`, and `pipeline/README.md` are updated in the same
  change; ADR-0018's exclusion note now points here.
