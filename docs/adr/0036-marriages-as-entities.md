# ADR-0036: Marriages as entities bounded by events

Status: **accepted** (owner decision 2026-09-29, #311). Like ADR-0031,
the owner's merge of the PR that adds it is its acceptance. It settles
#311's questions for marriages and amends ADR-0014 in place (§1's stored
`spouse` edge, §6's deferred divorce event). #394 implements it with
#385's extraction revision (ADR-0035, accepted), before scratch run 1.

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

- **`marriage` keeps its key, label and Wikidata id**, and becomes a
  subclass of `partnership` instead of `event`; each scratch run's reset
  bootstraps an empty graph (ADR-0025), so no stale statement survives.
- **The union points to its events**, so its item holds its couple and
  its bounds, as a P26 statement holds its dates, and no event needs a
  link back. A death that ends a union is the partner's own `death` event
  (ADR-0020 §2, ADR-0035 §3.2): `ended_by` points to it, never to a copy.
- **Each event has its own date** (`point_in_time` at its precision,
  with `earliest_date`, `latest_date` and its own circa), **place**
  (`location`) **and participants** (roles `witness`, `officiant`); a
  divorce names the court that acts by `carried_out_by` (ADR-0031;
  defined in ADR-0035 §3.4). The couple take part in the union alone,
  and its events reach them through it.
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
| "Husband: Felix LÖBIG, died 1931" (Ida's 1945 card) | an undated marriage: a source that merely names a spouse dates nothing, and the card's own date observes nothing, since Felix died in 1931 |
| a directory or a census listing a couple as married, with no start or end | an observation: a `point_in_time` on the union, never a wedding date. A union has no date of its own, so clustering never keys on it and the disagreement report never compares it |
| "Löbig, Ursula, Wwe." (the 1912 directory) | no union, since no spouse is named: ADR-0035's `civil_status` |
| a couple presented as a couple, with no word of a marriage | a partnership, which may be a marriage no source identifies. Two people named only as a child's parents are not thereby a couple |

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
| a family with two partners | a union: a `marriage` when the family has a MARR, otherwise a `partnership`, which may be a marriage the export does not identify |
| each MARR | a wedding. A TYPE that names a civil or a religious ceremony gives that kind; any other TYPE stays in the excerpt and is counted. A family's MARRs of one TYPE, or of none, are one wedding, each alternative date or place a statement of its own (ADR-0021) |
| each DIV, each ANUL | a divorce, an annulment (a family's DIVs are one divorce), with its PLAC as the event's `location` |
| a DATE's range or circa | the range or circa of that event's own date |
| `FROM … TO …` on a MARR, on a DIV | a wedding dated by the FROM and an end of unstated kind dated by the TO; the divorce's own start and end time, as an imprisonment is dated |
| an undated `DIV Y` | an undated divorce, as an undated death is a death event (ADR-0020 §2), so no unknown value is needed |
| a partner's DEAT | no end: GEDCOM does not say that the marriage lasted until the death |
| a SOUR beneath a MARR, DIV or ANUL | a citation of that event's claims, folded as the spouse claims fold it now (ADR-0021) |

### 6. Privacy and display

- **Privacy.** A union and the events that start and end it are
  withheld whenever any of its couple may be living, as a family is
  (#374): the export keeps them only when the union names both and the
  possibly-living check releases each; the site badges them otherwise.
  A partner's death, a witness and an officiant follow the person rule,
  and taking part in a union or its events is person evidence (ADR-0020).
- **Display.** A person's timeline (`person-timeline.rq`) lists the
  events that start and end the person's unions, so weddings and
  divorces are events of both spouses. The site shows each person's
  derived spouses and partners, each with every source that states it
  (R6). Weddings and divorces reach the places map by their `location`.

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
