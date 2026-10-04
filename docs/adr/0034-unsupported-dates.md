# ADR-0034: Dates before the common era and in the Julian calendar are unsupported, and refused with an explicit failure

Status: **accepted** (owner decision 2026-09-28, #338). Like ADR-0031
and ADR-0036, the owner's merge of the PR that adds it is its
acceptance. It amends ADR-0033 (§2's count of a century before the
common era) and replaces the handling #333 and #340 gave such dates.

## Context

Wikibase can store a date before the common era (a negative year) and a
date in the Julian calendar (calendar model `Q1985786`), and the project
had begun to handle both: the claim schema took a negative year and a
`julian` calendar, the site's `format_time` showed a century before the
common era with a minus sign (#333), the disagreement report shifted a
century before the common era by a year to compare it with the query
service's XSD 1.1 count, in which 1 BCE is year 0 (#241, ADR-0033 §2),
the possibly-living check read such a birth as no birth (#340), and the
review sheet marked a Julian date.

Family records never need either. The handling was edge cases with
correctness questions of their own (which count a year is served in,
whether a Julian date is shown as stored or converted, what a reader
compares a Julian date with), and tests for conditions the archive will
never meet. The owner, on 2026-09-28: "please adopt a preference for
treating BCE and Julian dates as explicitly unsupported throughout the
project, implementing an explicit hard failure should one of these be
encountered to enforce the safety of features where otherwise
correctness questions might be raised."

## Decision

### 1. What is supported

A date is supported only if its year is 1 CE or later and its calendar
is the proleptic Gregorian, Wikibase's `Q1985727`. Any other date, before
the common era (a year of 0 or less; xsd:dateTime's year 0 is 1 BCE) or
in the Julian or any other calendar, is unsupported. It is never
converted to another calendar or year, never guessed at and never shown:
wherever the project meets one, it fails explicitly, naming where.
`yizkr.core.dates` holds the rule, its one error (`UnsupportedDate`, a
`ValueError`) and the checks every layer shares.

### 2. Producers refuse one

- **The claim schema.** `TimeValue` refuses a year below 1 and any
  calendar but `gregorian`, each with a reason that begins
  `unsupported:`. An extraction claim carrying one is a reported reject
  like any claim that fails validation; the schema's description, which
  the extraction prompts carry, says so.
- **The GEDCOM adapter** fails the whole file, before any claim is
  made, when any DATE line states a date before the common era (a
  `B.C.`, `BC` or `BCE` epoch, a year written with a minus, or a year
  the grammar reads as 0) or in the Julian calendar (the `@#DJULIAN@`
  escape, or GEDCOM 7's `JULIAN`). The error names each such line by its
  number, never what it says. Of the two outcomes #338 offered, failing
  the file is the hard failure the owner asked for, and needs no census
  entry: a file that states one is not read at all, so nothing downstream
  meets it. A curator who wants the rest of the file read edits the
  export.
- **The review sheet's correction path** refuses a correction to a year
  before the common era (`0`, `-44`), naming it as unsupported; it never
  takes a calendar, so a correction is always Gregorian.

### 3. The writer refuses to write one

`BatchWriter.write_batch` refuses a batch holding an unsupported date,
as any claim's value or any qualifier's, before it reads from or writes
to the instance, in a dry run too, as it refuses an unapproved depicts
claim: `UnsupportedDate` names every such claim by its review-sheet id
and property, and nothing is written. The schema refuses such a date, so
only a batch built around validation can hold one. `TimeValue.to_wikibase`
refuses one too, so no path stores it.

### 4. Readers of the graph stop at one

A curator can still enter such a date in the wiki. Every reader of the
graph raises `UnsupportedDate`, naming the item and the statement, and
the command stops rather than show or compare a date whose correctness
is in question:

- **The disagreement report** fetches each time with the calendar model
  its value node states (the value's and each qualifier's), and
  `read_statements` stops at one it does not support. `period` refuses a
  time before the common era too.
- **The site** first asks the graph for any unsupported time, as a
  statement's value, a qualifier or in a reference
  (`unsupported_in_graph`), and stops before writing anything. Its
  `format_time` refuses a time before the common era, so neither the
  person and artifact pages, the timeline nor the inconsistencies page
  can show one.
- **The possibly-living check** reads each birth's calendar model with
  its time, and stops at one it does not support. The non-privileged
  export and the site's badge read births through it, so the export
  stops: the failure closes it, rather than releasing or withholding a
  person on a date nobody can read.
- **Reconcile's roster**, whose birth and death dates are the
  adjudication facts for items already in the graph, is read only after
  the same graph-wide check. The facts of a batch's own entities come
  from its claims, which the schema has checked.
- **The review sheet** shows only the batch's claims, which the schema
  has checked; it has no calendar to mark.

### 5. What is removed

The handling that existed only for these dates goes: the BCE century
count and the XSD one-year shift (`claims.century_of` and
`century_years`, `disagreement.century`), the BCE branch of
`format_time`, the possibly-living check's reading of a birth before the
common era as no birth, the review sheet's `[julian]` mark, and the BCE
rows of the century tests, unit and live. Centuries of the common era are
still counted as Wikibase counts them (MODELING.md, "Uncertainty and
time").

## Consequences

- No feature has to answer what a date before the common era or a Julian
  date means; each refuses one by name, and one test per layer
  (`tests/unit/test_unsupported_dates.py`,
  `tests/integration/test_live_unsupported_dates.py`) pins it.
- A curator who enters such a date in the wiki stops the site build, the
  disagreement report, the non-privileged export and the graph
  reconcile until it is removed, deprecated rank or not: rank is an
  overlay, and the date is still in the graph.
- A GEDCOM export with one Julian or BCE date anywhere is not ingested
  at all until it is edited.
- The extraction prompts' schema text changes (`calendar` takes only
  `gregorian`, and the description says so), a change to a stage before
  write, checked by the next scratch run's diff (ADR-0025).
- Supporting either kind of date later takes a new ADR, which would
  decide each reader's reading of it.
