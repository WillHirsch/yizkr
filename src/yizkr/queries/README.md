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

- **disagreement-report** — the flagship R2 view: values that different
  artifacts attribute to the same entity and property, side by side,
  each with every source that states it. Rows here are a healthy resting
  state (ADR-0002), not errors. Two readings of one artifact are not two
  sources (ADR-0023 §4), and a page fragment counts as its artifact; a
  deprecated statement is left out, and nothing needs a preferred rank.
  It compares the single-valued properties (dates and places of birth and
  death, parents, birth name, an event's own point in time, start and end
  time, a place's identity properties), and the start and end times of
  one residence place or one spouse that sources date differently (a
  point in time on those statements is not compared). A date whose span
  lies inside another's and a place recorded inside the other are
  differences of resolution, and are not listed: a date's span is the
  period its precision names or the range its source states with
  earliest and latest dates, which the report shows beside the value.
  Two dates stored alike are compared through those stated ranges. When
  either residence or spouse statement carries earliest or latest dates,
  their start and end times are compared as stated, with no difference
  of resolution suppressed. Spellings of a name that differ only in case
  or language are one value. See MODELING.md "Contradictions" for the
  full rules.
- **person-timeline** — every dated assertion about each person
  (statements, dated qualifiers such as a marriage's point in time, and
  events participated in), plural-by-default with sources (R6).
- **kinship** — how each person is related to a subject, derived from
  the kinship primitives and gendered by the relative's `sex_or_gender`
  (ADR-0014), beside the terms sources assert with `relative` +
  `kinship_type`. A row reads "?relative is ?subject's ?relationship".
  A parent is read from a `father` or `mother` statement on the child or
  a `child` statement on the parent, spouse and sibling statements
  either way, and two children of one parent are siblings. The
  second-order relations are paths of those steps, among them the aunt
  or uncle by marriage (a parent's sibling's spouse), the niece or
  nephew by marriage (a spouse's sibling's child) and the child-in-law.
  Every value at normal or preferred rank is read and a deprecated one
  never, so no preferred value is needed and none hides another. An
  asserted term names the statement's subject and has the ego as its
  value (ADR-0018), so `Klara —relative→ Nicholas ; kinship_type =
  "aunt"` is Nicholas's row, with Klara as his aunt, and Klara gets the
  inverse row, with Nicholas as her nephew, gendered by his sex. A term
  the query's table does not invert gives "relative", except a plain
  cousin term ("second cousin", "cousin once removed"), which is its own
  inverse; a cousin term with any other word ("Dad's cousin", "cousin's
  wife", "great-cousin") gives "relative"
  (`tests/integration/test_live_kinship.py`). The query is built from
  the query service's named subqueries (`WITH { … } AS %name`,
  `INCLUDE %name`), which the bundled WDQS and its query UI run and
  other SPARQL engines do not. For one subject, add `VALUES ?subject {
  wd:Q… }` to its main WHERE; the named sets are still computed over the
  whole graph. Rendered, it is longer than the query service accepts in
  a GET request line, so a client sends it by POST, as the live tests
  do.
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
