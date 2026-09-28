# Query library

Checked-in SPARQL for the W1 exploration tier (ADR-0004): queries are
published to wiki pages with run/embed links into the query service UI
(`Project:Queries` on the instance).

The `.rq` files are **templates**: they reference the model vocabulary
by manifest key (`wdt:{P:instance_of}`, `wd:{Q:human}`), because local
P/Q ids differ per instance and never live in this repo.
`yizkr publish-queries` resolves the keys against the target instance
and prepends `PREFIX` declarations bound to the instance's concept
URI — the WDQS build ships built-in prefixes pointing at wikidata.org,
so without the redeclaration every pattern silently matches nothing.
It then writes the pages:

```sh
uv run yizkr publish-queries --dry-run   # render and print only
uv run yizkr publish-queries             # or: make publish-queries
```

(Uses `deploy/.env` for the connection.)

## Queries

- **disagreement-report** — the flagship R2 view, as the values it
  compares: every statement of the compared properties, one row per
  stated-in source, so the values that different sources attribute to one
  entity and property are listed side by side. Rows here are a healthy
  resting state (ADR-0002), not errors. Each row carries the statement's
  rank, its value with its precision, the earliest and latest dates its
  source gives, its start time, end time and point in time, and the
  artifact each source belongs to (a page fragment belongs to its
  artifact). The compared properties are the single-valued ones (dates
  and places of birth and death, parents, birth name, an entity's own
  point in time, start and end time, a place's identity properties), and
  residence and spouse, whose dates are compared. The query pairs and
  filters nothing (ADR-0033): `yizkr.disagreement` pairs the values
  that different artifacts attribute and sets aside differences of
  resolution, and the static site's inconsistencies page shows its rows.
  See MODELING.md "Contradictions" for the rules; the offline tests are
  `tests/unit/test_disagreement.py`.
- **place-containment** — the recorded hierarchy the disagreement report
  reads: every location and part-of edge (best rank only, as truthy
  triples hold them) and every merge redirect, flat. A place value inside
  another, or the same item through a redirect, differs from it only in
  resolution (ADR-0009).
- **person-timeline** — every dated assertion about each person
  (statements, dated qualifiers such as a marriage's point in time, and
  events participated in), plural-by-default with sources (R6).
- **kinship** — how each person is related to a subject, derived from
  the kinship primitives and gendered by the relative's `sex_or_gender`
  (ADR-0014), beside the terms sources assert with `relative` +
  `kinship_type`. A row reads "?relative is ?subject's ?relationship".
  An asserted term names the statement's subject and has the ego as its
  value (ADR-0018), so `Klara —relative→ Nicholas ; kinship_type =
  "aunt"` is Nicholas's row, with Klara as his aunt
  (`tests/integration/test_live_kinship.py`).
- **places-map** — coordinates + what connects to each place; renders
  as a map view in the query UI.
- **unreferenced-statements** — QC report; every row is a
  missing-reference defect
  (ADR-0003 §3). Expected result: empty.
- **artifact-assertions** — inventory of everything each artifact
  asserts, for source re-examination.
- **under-witnessed-statements** — the low-scrutiny reappraisal queue
  (ADR-0023): machine-written statements with a single stated-in
  source and no curator statement on the same subject+property, one
  row per statement, time values with their stated precision. A
  queue, not a defect report; scope it with a `VALUES ?prop` filter.

## Adding a query

Add `<name>.rq` next to `publish.py` with a leading `# comment` block
describing it (it becomes the wiki page description), reference
vocabulary via `{P:key}`/`{Q:key}`, and re-run the publisher. The
offline tests (`make test`) verify that every placeholder matches a
real manifest key, so templates and model cannot drift apart; the live
tests (`make test-live`) execute every query against WDQS, assert
the zero-unreferenced-statements invariant, and pin the rows of the
under-witnessed queue and the disagreement report on fictional fixtures
(`tests/integration/test_live_under_witnessed.py`,
`tests/integration/test_live_disagreement.py`).
