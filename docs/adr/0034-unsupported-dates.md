# ADR-0034: Dates before the common era and in the Julian calendar are unsupported, and refused with an explicit failure

> **Temporary deprecated material — TODO [#733](https://github.com/WillHirsch/yizkr/issues/733):** this document still mixes current design details with development history and superseded prescriptions. Its historical material is not authoritative for new policy; use [REQUIREMENTS](../REQUIREMENTS.md), [CONTRACT](../CONTRACT.md) and the implemented code. Remove this note only after rewriting the document as current guidance with necessary rationale, or consolidating it elsewhere and deleting this file. The linked task tracks removal of all remaining historical and obsolete passages.

Status: **accepted** (owner decision 2026-09-28, #338). Like ADR-0031 and ADR-0036, the owner's merge of the PR that adds it is its acceptance. It amends ADR-0033 (§2's count of a century before the common era) and replaces the handling #333 and #340 gave such dates.

## Context

Wikibase can store a date before the common era (a negative year) and a date in the Julian calendar (calendar model `Q1985786`), and the project had begun to handle both: the claim schema took a negative year and a `julian` calendar, the site's `format_time` showed a century before the common era with a minus sign (#333), the disagreement report shifted a century before the common era by a year to compare it with the query service's XSD 1.1 count, in which 1 BCE is year 0 (#241, ADR-0033 §2), the possibly-living check read such a birth as no birth (#340), and the review sheet marked a Julian date.

Family records never need either. The handling was edge cases with correctness questions of their own (which count a year is served in, whether a Julian date is shown as stored or converted, what a reader compares a Julian date with), and tests for conditions the archive will never meet. The owner, on 2026-09-28: "please adopt a preference for treating BCE and Julian dates as explicitly unsupported throughout the project, implementing an explicit hard failure should one of these be encountered to enforce the safety of features where otherwise correctness questions might be raised."

## Decision

### 1. What is supported

A date is supported only if its year is 1 CE or later and its calendar is the proleptic Gregorian, Wikibase's `Q1985727`. Any other date, before the common era (a year of 0 or less; xsd:dateTime's year 0 is 1 BCE) or in the Julian or any other calendar, is unsupported. It is never converted to another calendar or year, never guessed at and never shown: wherever the project meets one, it fails explicitly, naming where. `yizkr.core.dates` holds the rule, its one error (`UnsupportedDate`, a `ValueError`) and the checks every layer shares.

### 2. Producers refuse one

- **The claim schema.** `TimeValue` refuses a year below 1 and the calendar `julian`, each with a reason that begins `unsupported:`. The schema the extraction prompts carry still names `julian`, and its description tells the model to give a Julian (old style) source date as written, with that calendar: so such a date is stated as one and becomes a reported reject, like any claim that fails validation, rather than passing as Gregorian or being left out. A calendar name the schema does not have fails the schema's own check, not as an unsupported date.
- **The GEDCOM adapter** fails the whole file, before any claim is made, when any DATE line states a date before the common era (a `B.C.`, `BC` or `BCE` epoch, its letters spaced or not, a year written with a minus or as 0, or a year the grammar reads below 1, at either end of a range) or in any calendar but the Gregorian: a GEDCOM 5.5.1 escape other than `@#DGREGORIAN@` (`@#DJULIAN@`, `@#DHEBREW@`, `@#DFRENCH R@`, `@#DROMAN@`, `@#DUNKNOWN@`), or GEDCOM 7's `JULIAN`, `HEBREW`, `FRENCH_R` or an extension calendar. A date phrase in parentheses (from the first `(` to the last `)`, or to the end of the value) is free text and is not read for these markers. Free text outside a phrase that names a calendar ("St Julian's day 1836") is read as naming it, and fails the file: the scan errs on the side of refusing (CN, decided with the build; the owner may veto it at merge). The error names each such line by its number, never what it says. Of the two outcomes #338 offered, failing the file is the hard failure the owner asked for, and needs no census entry: a file that states one is not read at all, so nothing downstream meets it. A curator who wants the rest of the file read edits the export.  
  Amended by #478 (2026-10-05): the Gregorian calendar named before a date (`@#DGREGORIAN@`, or GEDCOM 7's `GREGORIAN`) states the one supported calendar, so the adapter reads the date as written without it, whether it stands before the whole date or before each date of an approximation, a range or a period; a date phrase in parentheses stays free text.
- **The review sheet's correction path** refuses a correction to a year before the common era (`0`, `-1836`), naming it as unsupported; it never takes a calendar, so a correction is always Gregorian. As with any correction it cannot read, the claim is then left out, with a warning naming it, and the rest of the sheet is applied.

### 3. The writer refuses to write one

`BatchWriter.write_batch` refuses a batch holding an unsupported date, as any claim's value or any qualifier's, before it reads from or writes to the instance, in a dry run too, as it refuses an unapproved depicts claim: `UnsupportedDate` names every such date by its claim's review-sheet id and property and the part that holds it (the value, or a qualifier by its property), and nothing is written. The whole batch is refused, unlike a claim whose text the instance cannot hold, which is refused alone: an unsupported date in a batch means the batch was built around validation, so none of it is trusted. The schema refuses such a date, so only a batch built around validation can hold one. `TimeValue.to_wikibase` refuses one too, so no path stores it.

### 4. Readers of the graph stop at one

A curator can still enter such a date in the wiki. Every reader of the graph raises `UnsupportedDate`, naming the item and the statement, and the command stops rather than show or compare a date whose correctness is in question:

- **The disagreement report** fetches each time with the calendar model its value node states (the value's and each qualifier's), and `read_statements` stops at one it does not support. `period` refuses a time before the common era too, as a backstop.
- **The site** first asks the graph for any unsupported time, as a statement's value, a qualifier or in a reference (`unsupported_in_graph`), and stops before writing anything. Its `format_time` and the timeline's feed refuse a time before the common era, as a backstop, so neither the person and artifact pages, the timeline nor the inconsistencies page can show one.
- **The possibly-living check** reads each birth's calendar model with its time, and stops at one it does not support. The site's badge reads births through it.
- **The non-privileged export** first runs the same graph-wide check, before it assesses anyone, so it stops on an unsupported date anywhere in the graph, at any rank (a deprecated birth, a date of death, a qualifier or a reference included), and not only on a birth the check reads: the failure closes it, rather than releasing or withholding a person, or copying out a date, nobody can read. The refusal takes the export's own refusal path (`UnsupportedDateRefused`, a `NonprivilegedExportRefused` and an `UnsupportedDate`): the backup keeps its full dumps, records the refusal in its manifest, prunes old runs, and `yizkr backup` reports it on one line and exits 1.
- **Reconcile's roster**, whose birth and death dates are the adjudication facts for items already in the graph, is read only after the same graph-wide check. The facts of a batch's own entities come from its claims, which the schema has checked.
- **The review sheet** shows only the batch's claims, which the schema has checked; it has no calendar to mark.

### 5. What is removed

The handling that existed only for these dates goes: the BCE century count and the XSD one-year shift (`claims.century_of` and `century_years`, `disagreement.century`), the BCE branch of `format_time`, the possibly-living check's reading of a birth before the common era as no birth, the review sheet's `[julian]` mark, and the BCE rows of the century tests, unit and live. Centuries of the common era are still counted as Wikibase counts them (MODELING.md, "Uncertainty and time").

## Consequences

- No feature has to answer what a date before the common era or in a calendar other than the Gregorian means; each refuses one by name, and one test per layer (`tests/unit/test_unsupported_dates.py`, `tests/integration/test_live_unsupported_dates.py`) pins it.
- A curator who enters such a date in the wiki stops the site build, the disagreement report, the non-privileged export and the graph reconcile until it is removed, deprecated rank or not: rank is an overlay, and the date is still in the graph.
- A GEDCOM export with one date before the common era or in a calendar other than the Gregorian anywhere is not ingested at all until it is edited.
- The extraction prompts' schema text changes (the description says that dates before 1 CE and other calendars are refused, and asks for a Julian date as written with calendar `julian`), a change to a stage before write, checked by the next scratch run's diff (ADR-0025).
- The query pages published on the wiki (ADR-0004's exploration tier) run in the query service, not here, and show a curator-entered unsupported date as stored; only the project's own readers refuse it.
- The shared development instance held a date before the common era that an earlier live test wrote while such dates were supported, and every whole-graph reader refuses it once this decision lands: a scratch reset follows the merge (docs/TESTING.md).
- Decided with the build (BW, which the owner may veto at merge): every calendar but the Gregorian fails explicitly, as §1 says, so the GEDCOM adapter refuses all of them, not only the Julian.
- Supporting either kind of date later takes a new ADR, which would decide each reader's reading of it.
