# ADR-0036: Marriages as entities bounded by events

Status: **accepted** (owner decision 2026-09-29, #311). Like ADR-0031,
the owner's merge of the PR that adds it is its acceptance. It settles
#311's questions for marriages and amends ADR-0014 in place (§1's stored
`spouse` edge, §6's deferred divorce event). #394 implements it with
#385's extraction revision (ADR-0035, accepted), before scratch run 1.

Amended by #408 (2026-09-29): the GEDCOM adapter makes unions as §5
says. A MARR TYPE names a civil ceremony by `civil`, `civil ceremony`,
`civil marriage`, `Standesamt` or `standesamtlich`, and a religious one
by `religious`, `religious ceremony`, `religious marriage`, `church`,
`church wedding` or `kirchlich`, in any case; a further TYPE of another
kind is counted, as any other TYPE is. A DIV or an ANUL states its event
as GEDCOM 5.5.1 has one asserted, by a DATE or a PLAC with a value or a
Y, and one that states none is counted. An end of unstated kind is
labelled as the end of its union. Two unions of one couple, or their
events, get labels of their own, told apart by a number, since the
writer found an item by its label (amended by #109, 2026-10-06: the
writer finds it through the identity map; the numbers keep the labels a
reader reads apart), and an entity whose id another
entity holds (a family whose xref is another union's event's, or a
person's) takes a numbered one. Each union and each event is counted
by kind in the adapter's outcomes, and so is an undated divorce or
annulment, one whose DATEs give no date among them.

Amended by #408 (2026-09-29; decided by the orchestrator, the owner may
override):

- **A MARR states its wedding as a DIV states its divorce.** `MARR N`,
  and a MARR that states nothing (no Y, and no DATE or PLAC with a
  value), makes no wedding and is counted as `MARR`, and a family with
  no MARR that states one is a partnership, unless a DIV or ANUL of it
  states its event (below, 2026-10-02).
- **Weddings are grouped by kind, not by TYPE.** The table's "MARRs of
  one TYPE" reads "of one kind": MARRs whose TYPEs name one kind of
  ceremony (`civil` and `Standesamt` alike) are one wedding, and a MARR
  whose TYPE names no known kind ("Elopement") joins the wedding of
  unstated kind, its TYPE counted as `MARR.TYPE`.
- **Two partners under one tag.** A family with two HUSBs and no WIFE,
  or two WIFEs and no HUSB, naming two people, has those two as its
  couple: a partnership, or a marriage where a MARR states a wedding,
  counted as `FAM two HUSB/WIFE`. The second stays a partner only, never
  a parent: the family's children take the first HUSB as father and the
  first WIFE as mother, as ADR-0021's #322 amendment has it, since a
  second father or mother "would give each child a second parent, which
  the record cannot mean". (Amended 2026-10-02.)

Amended by #353 (2026-10-23; owner decision, overriding the 2026-10-02
line above): **a family's first two partner lines are its couple, and
each is a parent as its tag states.** GEDCOM 5.5.1 exports write a
same-sex couple as two HUSBs or two WIFEs, so the adapter reads a
family's first two HUSB and WIFE lines, in whatever combination and
order, as its couple, a HUSB before a WIFE (still counted as `FAM two HUSB/WIFE` when they
share a tag), and each is the children's parent as its tag states: a
HUSB their `father`, a WIFE their `mother`, so two HUSBs give each child
two fathers. A third partner line is counted (`FAM.HUSB`, `FAM.WIFE`),
not read, even where it is the family's only WIFE (or HUSB); a line
that restates one before it (same tag, same person) is counted and
passed over, so the next line takes its place. "One
person as HUSB and WIFE" applies to the two partner lines.

Amended by #408 (2026-10-02; decided by the orchestrator, the owner may
override): **a divorce or an annulment ends a marriage.** A family whose
DIV or ANUL states its event, with no MARR that states a wedding, is a
`marriage` with no wedding, its couple `spouse`s, counted as `union:
marriage without a wedding`; the source asserts the end of a marriage,
not of a partnership. A family whose HUSB and WIFE name one person has
no couple, as two HUSBs naming one person have none.

Amended by #408 (2026-10-02; decided by the orchestrator, the owner may
override): **one person as HUSB and WIFE.** A family whose first HUSB and
first WIFE name the same person gives no union and no parent claim for its
children, counted as `FAM one person as HUSB and WIFE`: one person as
both father and mother is a reading the record cannot mean, so it is not
guessed.

Amended by #461 (2026-10-05; decided by the orchestrator, the owner may
override): **a parent listed as their own child.** A family whose CHIL
names its HUSB or its WIFE gives no claim that the person is their own
father or mother, counted as `FAM one person as parent and CHIL`; the
other parent's claim and the family's union stand. Validation refuses a
claim whose value is its own subject (ADR-0035 §3.5), so the adapter
leaves that one link out rather than failing the whole document.

Amended by #409 (2026-09-29): §3's union that names only one of its
couple is ambiguous with every other union of that person, whether or
not a second union of theirs is known, since clustering cannot count a
person's unions before it has clustered them. A union's couple are its
participants as spouses or partners, or every participant where none
has such a role.

Amended by #409 (2026-09-29), decided by the orchestrator (the owner may
override): a union that names only one of its couple stays ambiguous,
as the line above says; that can only keep two unions apart.

Amended by #410 (2026-10-16): the writer refuses a claim of the retired
`spouse` itself, before the first write, naming it in its `refused` list
with a warning, as validation does, since `yizkr write` takes a batch
without validating it; the rest of the batch is written. A union and its
events, from extraction as from the GEDCOM adapter, are written through
the writer's general path: their items, minted through the identity map
with their kinds, the couple's `participant` statements with their
roles, `started_by`, `ended_by` (a partner's own `death` event
included), and each event's own date, place and `carried_out_by`, each
with both references (ADR-0003).
Superseded by ADR-0040 (2026-10-24, the owner's decisions and answers of
2026-10-24): "`ended_by` (a partner's own `death` event included)" no
longer holds.
- A union ended by a death points to the partner who died, through
  `ended_by_death_of`: a person value with no date of its own.
- It is written only where a source specifically states that the couple
  were married when that partner died. Where the end is only implicit,
  views derive it and nothing is written (ADR-0040's Q2).
- Readers take that partner's own death statements and derived "died
  by" dates, so nothing is copied.
- `ended_by` keeps the divorce, the annulment and an end of unstated kind
  (§1, §2, §3, §5, §6).

## Context

MODELING.md dates a marriage as Wikidata dates spouse (P26): a statement
per spouse, the wedding its start time, the end its end time, one circa
for both, and the wedding's place its `location`. #311's ten questions
show what that leaves open: which end a circa is on, an end's cause and
bound, where a wedding's date and place and a divorce's place belong, an
undated divorce, a ceremony's kind, couples no source says married, and
`FROM … TO …` on a MARR. They share a cause: a wedding and its end are
events, each with its own date, place and people, pressed into the
qualifiers of one statement. ADR-0014 §6 deferred a divorce event until
a source narrated one; the universe's divorce judgment is such a source.

## Decision

A marriage is its own entity, bounded by events; the spouse link is
derived from it. A *union* below is any partnership, a marriage included.

### 1. Vocabulary

| Key | What it is | Wikidata |
|---|---|---|
| `partnership` | class, new: a couple's relationship, of any kind. A source that presents two people as a couple without saying they married gives a partnership, which may be a marriage no source identifies; the couple take part with a new role, `partner` (ADR-0035 §3.2) | none |
| `marriage` | class, kept and re-described: a subclass of `partnership`, no longer an `event`; the couple take part with role `spouse` (ADR-0035) | Q8445 |
| `wedding` | event class, new: a ceremony that starts a marriage | an item |
| `civil_wedding`, `religious_wedding` | subclasses of `wedding`: its kind, where a source states it | an item each |
| `divorce`, `annulment` | event classes, new: an end that a court or another authority pronounces | an item each |
| `started_by` | union → the event that began it: its wedding, or each of two ceremonies. It accepts `wedding` and its subclasses (ADR-0035 §3.5) | none (P26 takes a start time, P580) |
| `ended_by` | union → the event that ended it: a divorce, an annulment, a partner's own `death` event, or an `event` of unstated kind. It accepts `divorce`, `annulment`, `death` and `event` | none (nearest: end cause, P1534, which names a cause's class) |
| `spouse` | property, retired: it accepts nothing, so validation refuses any claim of it, and no prompt offers it. The spouse link is a query | P26 |
| `ended_by_death_of` | (amended by ADR-0040, 2026-10-24) union → the partner whose death ended it, a person, with no date of its own. It is written only where a source specifically states that the couple were married when that partner died (ADR-0040's Q2). `ended_by` no longer accepts `death`, and reads "a divorce, an annulment, or an `event` of unstated kind" | none (nearest: end cause, P1534) |

- **`marriage` keeps its key, label and Wikidata id**, and becomes a
  subclass of `partnership` instead of `event`; each scratch run's reset
  bootstraps an empty graph (ADR-0025), so no stale statement survives.
- **The union points to its events**, so its item holds its couple and
  its bounds, as a P26 statement holds its dates, and no event needs a
  link back. A death that ends a union is the partner's own `death` event
  (ADR-0020 §2, ADR-0035 §3.2): `ended_by` points to it, never to a copy.
  Superseded by ADR-0040, 2026-10-24: there is no death event.
  - A death that ends a union is named by `ended_by_death_of` → the
    partner who died, written only where a source states that the
    couple were married when that partner died (ADR-0040's Q2).
  - Otherwise the end is derived in views from the partners' death
    statements (ADR-0040 §2.6), and nothing is written.
  - Where the batch names the couple, validation checks that the value
    is one of them.
- **Each event has its own date** (`point_in_time` at its precision,
  with `earliest_date`, `latest_date` and its own circa), **place**
  (`location`) **and participants** (roles `witness`, `officiant`); a
  divorce names the court that acts by `carried_out_by` (ADR-0031;
  defined in ADR-0035 §3.4). The couple take part in the union alone,
  and its events reach them through it.
  (Amended by ADR-0040, 2026-10-24, its Q6, rule (a): a wedding's
  registrar and its non-kin witnesses are left out, `extract` false. A
  kin witness or officiant stays, with that role.)
- **The spouse link is a query**: two participants of one marriage with
  role `spouse`. The partner link reads every partnership, marriages
  included, and either role: a married couple are partners too. A spouse
  term gives a marriage, as "Dein Vater" gives `father` (ADR-0035 §3.3).

**Where this departs from Wikidata**, which has no item per marriage and
qualifies each spouse's P26 with a start time, an end time, an end cause
and a place of marriage (P2842): each bound here has its own date, range,
circa, place and people, which qualifiers cannot keep apart. An export
can still generate P26 so qualified. #394 adds the new classes' ids.

### 2. What sources give

| The source writes | It gives |
|---|---|
| "Die am 8. Februar 1947 vor dem Standesbeamten in Flörburg geschlossene Ehe der Parteien wird geschieden" (the 1957 judgment) | Leopold and Agnes's marriage; its civil wedding on 8 February 1947 at Flörburg; its divorce on 2 January 1957, carried out by the Landgericht Rekau |
| "Vor dem Standesbeamten erschienen zum Zwecke der Eheschließung" (the 1905 certificate) | Sebastian and Hildegard's marriage, and its civil wedding on 15 December 1905 at Flörburg |
| "Witwer der Cäcilie Löbig geb. Tarle" (the burial register) | Eugen and Cäcilie's marriage, `ended_by` Cäcilie's death event, which ADR-0035 §3.1 makes of the same words |
| (amended by ADR-0040, 2026-10-24) "Witwer der Cäcilie Löbig geb. Tarle" | Eugen and Cäcilie's marriage, `ended_by_death_of` Cäcilie (ADR-0040's Q2), since "Witwer der" states that they were married when she died; the same words give Eugen's `civil_status` `widowed`, and no death statement for Cäcilie, whose "died by 16 March 1909" is derived (ADR-0040's Q7, Q5; §2.7) |
| (added by ADR-0040, 2026-10-24) "dessen verstorbener Ehefrau Cäcilie" (the 1905 certificate) | Eugen and Cäcilie's marriage, with no end: the words state a relation and a death, not that she was still his wife when she died. Views derive the end (ADR-0040's Q2, Q12) |
| "Husband: Felix LÖBIG, died 1931" (Ida's 1945 card) | an undated marriage: a source that merely names a spouse dates nothing, and the card's own date observes nothing, since Felix died in 1931 |
| (amended by ADR-0040, 2026-10-24) "Husband: Felix LÖBIG, died 1931" | still no end: the card does not state that they were married when he died, so views derive the end (ADR-0040's Q2) |
| a directory or a census listing a couple as married, with no start or end | an observation: a `point_in_time` on the union, never a wedding date. A union has no date of its own, so clustering never keys on it and the disagreement report never compares it |
| "Löbig, Ursula, Wwe." (the 1912 directory) | no union, since no spouse is named: ADR-0035's `civil_status` |
| a couple presented as a couple, with no word of a marriage | a partnership, which may be a marriage no source identifies. Two people named only as a child's parents are not thereby a couple |
| a spouse entry in a list (a descendant list, a family register) with no marriage stated (amended by #627) | a partnership: the list presents a couple. A marriage the source states stays a marriage |

### 3. Identity across documents

- **A union is keyed by its couple**, whatever its class, once people
  are clustered. Unions of one couple link without a date, as two births
  of one person do (#361); one that names only one of its couple is
  ambiguous where that person has two unions.
- **Wedding dates tell two unions of one couple apart**: where they
  cannot overlap, the pair is ambiguous and a scratch run keeps it apart
  (ADR-0025), since no rule tells a remarriage from a wedding dated two
  ways, and a wrong merge would give one marriage's divorce to the other.
- **Events are keyed by union, kind and a date that agrees** (#361's
  event rule, the union standing for the participants). A wedding of
  unstated kind meets a civil or a religious one, but a civil and a
  religious wedding are two ceremonies; an end of unstated kind meets
  any end. A death stays its principal's own (ADR-0035 §7).
  (Superseded by ADR-0040, 2026-10-24: "A death stays its principal's
  own" reads "A death is the person's own statements": no death event
  is keyed. `ended_by_death_of` is not lifted as a key, since the couple
  already key the union. Clustering's death comparison reads it for the
  late partner's derived span.)

### 4. Neutral coexistence (ADR-0002)

- **Two sources that date one wedding differently** give two dated
  statements on one wedding, which the disagreement report compares as
  any event's own date (ADR-0033): the judgment's 8 February 1947 and the
  1984 tree's "= (1947)" differ only in resolution, and are not listed;
  a circa 1946, which clustering widens to meet them, would be.
- **A partnership and a marriage of one couple agree.** The GEDCOM
  export's family of Felix and Ida has no MARR, so it gives a partnership;
  Ida's card and the tree make it, more specifically, a marriage. Both
  `instance of` statements stand, each referenced (ADR-0007, as amended
  by #233), as do different ends that two sources give a union.

### 5. The GEDCOM adapter

| The export gives | The adapter makes |
|---|---|
| a family with two partners | a union: a `marriage` when the family has a MARR (or, amended by #408, a DIV or ANUL that states its event: a marriage with no wedding, counted), otherwise a `partnership`, which may be a marriage the export does not identify |
| each MARR | a wedding. A TYPE that names a civil or a religious ceremony gives that kind; any other TYPE stays in the excerpt and is counted. A family's MARRs of one TYPE, or of none, are one wedding, each alternative date or place a statement of its own (ADR-0021) |
| each DIV, each ANUL | a divorce, an annulment (a family's DIVs are one divorce), with its PLAC as the event's `location` |
| a DATE's range or circa | the range or circa of that event's own date |
| `FROM … TO …` on a MARR, on a DIV | a wedding dated by the FROM and an end of unstated kind dated by the TO; the divorce's own start and end time, as an imprisonment is dated |
| an undated `DIV Y` | an undated divorce, as an undated death is a death event (ADR-0020 §2), so no unknown value is needed |
| a partner's DEAT | no end: GEDCOM does not say that the marriage lasted until the death |
| (amended by ADR-0040, 2026-10-24) a partner's DEAT | still no end written; views derive the end (ADR-0040's Q2) |
| (amended by ADR-0040, 2026-10-24) an undated `DIV Y` | still an undated divorce, an event. But "as an undated death is a death event (ADR-0020 §2), so no unknown value is needed" no longer holds, since an undated death is a `date_of_death` of unknown value (ADR-0040 §2.2). A divorce is the couple's event, with its own court and place, so it stays one |
| a SOUR beneath a MARR, DIV or ANUL | a citation of that event's claims, folded as the spouse claims fold it now (ADR-0021) |

### 6. Privacy and display

- **Privacy.** A union and the events that start and end it are
  withheld whenever any of its couple may be living, as a family is
  (#374): the export keeps them only when the union names both and the
  possibly-living check releases each; the site badges them otherwise.
  A partner's death, a witness and an officiant follow the person rule,
  and taking part in a union or its events is person evidence (ADR-0020).
  Amended by ADR-0040, 2026-10-24:
  - The partner's death is that partner's own statements.
  - The partner named by `ended_by_death_of` follows the person rule
    through `PERSON_VALUE_PROPERTIES`, never as a bound: it stays out of
    `backup.UNION_BOUNDS`.
  - `ended_by_death_of` is death evidence for the partner it names
    (ADR-0040's Q9). A partner it alone releases is kept when the union
    is left out, without the statement that released them.
- **Display.** A person's timeline (`person-timeline.rq`) lists the
  events that start and end the person's unions, so weddings and
  divorces are events of both spouses. The site shows each person's
  derived spouses and partners, each with every source that states it
  (R6). Weddings and divorces reach the places map by their `location`.
  (Amended by ADR-0040, 2026-10-24: a union with no stated end shows
  "no source states how it ended", with each partner's death statements
  and their sources, and beside them the end derived from the partners'
  death spans, labelled as derived (ADR-0040 §2.6).)

### 7. Residences, occupations and other periods

These keep today's dated statements: their ends seldom carry facts of
their own. A circa on one end stays a whole-statement flag, which widens
the claim and never narrows it; the quoted excerpt shows which end is
meant. `latest_start_date` and `earliest_end_date` stay, for them alone.

## Consequences

- **What #394 changes.** MODELING.md moves with the code, so this ADR
  leaves it alone. #394 amends its "Marriages, residences and other
  periods" (marriages get a section of their own), "Kinship" (the spouse
  edge is derived), "Core classes", "Uncertainty and time",
  "Contradictions", "Identity and reconciliation" and "Living persons and
  privacy"; the manifest and prompts, with #385; the GEDCOM adapter;
  clustering; the disagreement report; the timeline and kinship queries;
  the site, privacy check and export; the universe, fixture corpus and
  docs/EXAMPLES.md; and, by dated lines, ADR-0020, ADR-0021, ADR-0033.
- **Cost.** A union and an item per event replace a statement per spouse.
- **Risks.** Genealogy programs often leave out a MARR they cannot date,
  as the universe's export does for Felix and Ida, so a couple known from
  an export alone shows as partners until a source identifies their
  marriage. A wedding dated two ways that cannot overlap stays split in a
  scratch run, so the report compares those dates once a review joins them.

## Alternatives considered

- **Spouse statements with qualifiers**, today's Wikidata-style form:
  rejected for #311's reasons, among them one circa for two ends and no
  home for a divorce's place, a ceremony's kind or the court.
- **A marriage entity with dates of its own**, and "married at" and
  "divorced at" links: each end gets its own circa, but every other fact
  of a wedding or a divorce needs a property of its own, and a marriage
  ended by a death copies the death's date, a copy that can drift.
- **A union bounded by events**, chosen: it reuses the event machinery
  (dates, places, roles, `carried_out_by`, the report's comparison of an
  event's date, the places map, privacy's person evidence), so what
  happened at a place is found alike for every event, with no new property.
