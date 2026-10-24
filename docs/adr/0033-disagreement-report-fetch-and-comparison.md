# ADR-0033: Disagreement report: SPARQL fetch, Python comparison

Status: **proposed** — drafted for #266, which carries out the owner's
decision of 2026-09-28 to land #227 as reviewed and then split the report.
Cites ADR-0002 (coexistence), ADR-0004 (the exploration tier), ADR-0009
(the place hierarchy) and ADR-0023 §4 (coexistence is cross-source only),
which points here.
Amended by #231 (2026-09-28): the open question of which qualifier carries
a marriage date is settled (MODELING.md, "Marriages, residences and other
periods"), so a residence's or a spouse's start and end each have a range
of their own, the fetch lists the two bounds that are new, and
`compared_as_stated` is gone (§1, §2, §5).
Amended by [ADR-0038](0038-place-containment-as-source-claims.md)
(accepted 2026-10-23, #515; §5, §8.1): `place-containment.rq` lists every
location and part-of statement as an edge, with its rank and whether it
has a stated-in source, and the comparison follows only an edge a source
states and a curator has not deprecated, at any time it carries (§1).
Amended by #241 (2026-09-28): the open question of which century a
century-precision date names is settled (MODELING.md, "Uncertainty and
time"): centuries are counted as Wikibase counts them, so `century` gives
the 19th century the span 1801 to 1900 (§2).

## Context

The disagreement report is the flagship R2 view: the values that
different artifacts attribute to one entity and property, side by side.
After #227 its whole comparison lived in one SPARQL query of about 260
lines, `disagreement-report.rq`. The query paired statements and filtered
the pairs by every rule in MODELING.md "Contradictions":

- different artifacts only, a page fragment counting as its artifact;
- no deprecated statement, and no need for a preferred one;
- the single-valued properties, and the start and end times of one
  residence place or one spouse;
- names compared as text, letter case and language tag aside;
- date spans by precision (the century convention included), stated
  ranges and open ends, and dates stored alike told apart by their ranges;
- residence and spouse times beside a range compared as stated;
- place containment through merge redirects.

That had three costs.

- **The rules could be tested only live.** Their behaviour showed only
  against the query service; the unit tests could inspect nothing but the
  query's text.
- **Every correctness change moved the query plan.** Each review round of
  #227 re-measured speed on its own benchmark. The PR took six agents and
  about 11.5 agent-hours, with rounds alternating between correctness
  fixes and speed regressions, and its last correctness fix (dates stored
  alike) made it slower on graphs where sources agree.
- **Its text outgrew a GET request.** The query service refuses a request
  line over about 8 KB (HTTP 414). #227 hit that in its live tests and
  moved only the site's runner and the tests' helper to POST.

## Decision

### 1. The fetch is flat SPARQL

Two library queries fetch what the report compares. Neither pairs
anything, so each costs time in proportion to what it lists.

- **`disagreement-report.rq`** lists every statement of the compared
  properties, one row per stated-in source: the entity, property,
  statement and rank; the value with its precision (an item with the label
  of the item a merge redirect resolves to); the earliest and latest dates
  its source gives, each with its precision; the start time, end time and
  point in time it carries as qualifiers, and the latest start date and
  earliest end date that bound a residence's or a spouse's start and end
  (amended by #231); and each stated-in source with the artifact it
  belongs to (a page fragment resolves to its artifact).
  It filters nothing, not even by rank: rank is an overlay the comparison
  reads, and a statement with no stated-in source is listed with none.
- **`place-containment.rq`** lists every location and part-of statement
  as an edge, with its rank and whether it has a stated-in source, and
  every merge redirect (amended by ADR-0038: it listed the truthy edges,
  best rank only). The comparison follows an edge a source states at
  normal or preferred rank.

Both stay templates in `src/yizkr/queries/`, rendered and published by
`yizkr publish-queries`.

### 2. The comparison is a pure Python module

`yizkr.disagreement` reads the two queries' rows and compares them. It
does no I/O: `disagreements(run, properties, items, concept_uri)` takes a
runner. It groups statements by entity and property, and for a residence
or a spouse by the place or person (through merge redirects) and the date
compared. It pairs the values within each group, and lists a pair unless
every source of both values belongs to one artifact. Sources that state
one value alike are listed together.

Each rule of MODELING.md "Contradictions" is one function, with offline
tests on the fictional universe (`tests/unit/test_disagreement.py`):

| Rule | Function |
|---|---|
| a deprecated statement is left out; a preferred one is compared like any other | `in_contention` |
| a statement with no stated-in source takes no part | `attributed` |
| single-valued properties compare their values, a time once per stated range and any other value once, with no range; residence and spouse compare the times `dated_times` names, each end with its own range: a start from its earliest date to its latest start date, an end from its earliest end date to its latest date (amended by #231) | `compared_values`, `dated_times` |
| a name compares as text, letter case and language tag aside; an item through its merge redirect | `comparison_key` |
| a date's span: the stated range, open-ended when one bound is given, or the period its precision names | `span`, `period`, `century` |
| a precision coarser than a century names no span; a span inside the other's differs in resolution | `named`, `spans_nest` |
| two dates stored alike are compared through their stated ranges | `stored_alike_order`, `paired` |
| a place recorded inside the other differs in resolution | `place_within` |
| two readings of one artifact are not two sources, nor are two readings of one statement's value, however many artifacts it cites | `from_different_artifacts`, `one_claim` |

The two open questions each touch one function, and neither is decided
here. #241 (which century a century-precision date names) is `century`,
which keeps the convention claims.py writes: the 19th century stored as
1800 names 1800 to 1899. The site's `format_time` follows the same
convention for display. #231 (which qualifier carries a marriage date) is
`dated_times`, which compares a spouse's start and end times and not its
point in time. The fetch already lists the point in time, so either answer
changes that function alone.

Amended by #231 (2026-09-28): #231 settled that question, and with it
which end a residence's or a spouse's earliest and latest dates bound
(MODELING.md, "Marriages, residences and other periods"). A marriage's
date is the spouse statement's start time and its end the end time, and
a point in time dates an observation, which is still not compared. Each
end has a range of its own, as on Wikidata's spouse (P26): a start from
its earliest date to its latest start date, an end from its earliest end
date to its latest date. So the answer reached beyond `dated_times`: the
fetch lists the latest start date and the earliest end date, and with
each end's range known, a residence's or a spouse's start and end are
compared like any other date, through `span`, `stored_alike_order` and
`paired` as they stand, and `compared_as_stated`, which kept every
difference of resolution beside such dates while their end was unknown,
is gone.

Amended by #241 (2026-09-28): #241 settled which century a
century-precision date names (MODELING.md, "Uncertainty and time").
Centuries are counted as Wikibase and Wikidata count them, from year 1:
the 19th century runs from 1801 to 1900, whichever of its years is
stored, and a claim gives it by its first year, 1801. So `century` gives
a date stored as 1801 or as 1900 the span 1801 to 1900, and one stored
as 1800 the 18th century's span, 1701 to 1800, and the site's
`format_time` names the same century as the wiki. In the report the
answer stayed in that function, as above, with one addition: the query
service serves a year, a month or a day before the common era one year
on, in XSD 1.1's count where 1 BCE is year 0, but a century's year as
stored, so a century before the common era is given its span in the
count of the dates it is compared with.

Amended by #338 (2026-10-04): dates before the common era and in the
Julian calendar are unsupported (ADR-0034). The fetch now gives each
time the calendar model its value node states, `read_statements` stops
with `UnsupportedDate`, naming the entity and the statement, at a time
before the common era or in a calendar other than the proleptic
Gregorian, and `century`, with its one-year shift for a century before
the common era, is gone: `period` counts a century of the common era as
above.

### 3. The published query page lists attributed values

The query page on the wiki (the exploration tier, ADR-0004) now shows the
attributed values of the compared properties side by side, one row per
statement and source, rather than the filtered pairs. A reader weighing
the evidence sees every source's value, including a deprecated one with
its rank, and a statement with no stated-in source. The paired and
filtered view is the static site's inconsistencies page. The scratch-run
report (#124) can read the same rows through `disagreements()`, and
`counts_by_property` gives them as aggregate counts, with no row about
any one entity. *Amended (#575):* the run report reads the run directory
alone (ADR-0025 §3), so it never calls `disagreements()`: the site stage
keeps `counts_by_property` of the rows it compared on the written graph,
with their total, in its record (`corpus/site/record.json`), and the run
report and the diff read them from there.

### 4. One SPARQL client, which POSTs

`yizkr.core.sparql` sends every query as a form-encoded POST body, so no
query's length matters, and reports a refused query with the engine's
own message. The engine's answer echoes the whole query before it names
the exception (a MalformedQueryException, a TimeoutException), so the
client keeps the line that names it, whatever the query's length, and
the start of an answer that names none. The static site and the live
tests' `run_sparql` use it. The other readers (smoke, backup, places,
reconcile) send fixed-size queries by GET and move to the client when
they are next touched.

### 5. Behaviour is preserved, where the query kept to its documentation

#227's live tests pass with their fixtures and expected rows unchanged;
only their plumbing moved to the comparison. On the query sandbox's
synthetic graphs (#251), the old query and the new fetch and comparison
list identical rows: the same entities, properties, places, qualifiers,
values, precisions, stated ranges and sources, 400 rows at 250 persons,
653 at 500 and 1,346 at 1,000.

In three places #227's query differed from its own documentation, and the
comparison follows the documentation:

- **Residence and spouse times stored alike.** The query never paired two
  start or end times stored alike. MODELING.md says that when either
  statement carries earliest or latest dates, the times are compared as
  stated, with no difference of resolution suppressed. So an end on 1
  October 1939 beside an end in October 1939 (both stored as 1939-10-01)
  is now listed when either statement carries such dates, as an end on 2
  October 1939 would have been. Without them the day still refines the
  month. A time stored alike at a precision coarser than a century, which
  names no span, is listed beside a finer one with or without such dates,
  as two such dates of a single-valued property are. Amended by #231
  (2026-09-28): those dates now bound one end each, so two start or end
  times stored alike are told apart by the ranges stated for that end,
  as two dates of a single-valued property are (§2). The last sentence
  above never held for a single-valued property beside a century: a time
  coarser than a century is given its century's period, so beside a
  century-precision time stored alike, with no range on either, it names
  the same period, and the two are one value. A residence's or a spouse's
  start or end now compares the same way, so there too such a time is one
  value with a century stored alike, and is listed beside a decade, a
  year or a day stored alike.
- **Letter case.** The query lowercased names. "Differ only in letter
  case" is Unicode case folding, which also makes a sharp s and SS one
  value, and a long s (ſ) and s.
- **A statement that carries two earliest (or latest) dates.** Such a
  statement puts up one reading of its value per date. The query never
  paired a statement with itself, so when another statement shared one of
  those readings, the row listed that value without the first statement's
  source. Each value is listed with every source that states it, so the
  comparison pairs any two readings. Two readings of one statement's
  value alone are one claim, which every artifact the statement cites
  states alike, so they are never listed (`one_claim`).

None of the three changes a row of the live tests, whose fixtures hold no
such case, or of the synthetic graphs, where the two lists match.

## Consequences

- Every rule is tested offline, with mutation evidence that each test
  fails under the regression it guards, and a rule change no longer moves
  a query plan.
- The fetch's time grows at most linearly with the graph (Timings,
  below), and the comparison takes about a second at 1,000 persons.
- The report now needs two queries and a Python pass wherever it is read.
  The site does this; a reader running the published page by hand sees
  the values, not the pairs.
- The comparison walks containment paths in Python over every location
  and part-of edge, which the containment query lists whole. A family
  archive's hierarchy is small, and the walk is cached per place.

## Timings

Measured in the query sandbox (#251) on its synthetic graphs at three
sizes, with a fresh sandbox for each size. Each fetch ran 16 times, and
the settled time is the median of the last five. The first run on each
fresh sandbox took 24 to 61 s; the table gives the settled times. Other
sessions kept the machine's load average between 5 and 10, so the times
are indicative rather than exact. The comparison is one Python pass over
both queries' rows.

| Graph | Fetch rows | Fetch (settled) | Containment rows | Containment (settled) | Comparison | Rows listed |
|---|---:|---:|---:|---:|---:|---:|
| 250 persons | 2,743 | 5.7 s | 464 | 0.20 s | 0.23 s | 400 |
| 500 persons | 5,442 | 9.5 s | 903 | 0.32 s | 0.73 s | 653 |
| 1,000 persons | 11,041 | 13.4 s | 1,751 | 0.45 s | 0.96 s | 1,346 |

The fetch's rows double with each size, while its settled time grows by
less: 2.1, 1.7 and 1.2 s per thousand rows. So its time grows at most
linearly with the graph. The containment query stays under half a
second, and the comparison takes about a second at 1,000 persons. The
rows listed are the same as the old query's on each graph (§5).

## Alternatives rejected

- **Tune the pairing query further.** Each rule change would still move
  the plan and need its own benchmark, and the rules would stay testable
  only live.
- **Pair in SPARQL and filter in Python.** Pairing is still quadratic per
  subject inside the engine, and its plan still moves with each filter
  that stays there.
- **Walk containment in SPARQL, seeded per place value.** It evaluates a
  property path per value inside the engine; the flat edge list is
  linear and small.
