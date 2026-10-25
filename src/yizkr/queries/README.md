# Query library

Checked-in SPARQL for the W1 exploration tier (ADR-0004): queries are
published to wiki pages with run/embed links into the query service UI
(`Project:Queries` on the instance).

The [query architecture diagram](../../../docs/ARCHITECTURE.md#queries-and-private-exploration)
shows how these templates feed the query UI and the private static site.

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
  rank, its value with its precision (and a time's calendar model, which
  the comparison checks: ADR-0034), the earliest and latest dates its
  source gives, its start time, end time and point in time, the latest
  start date and earliest end date that bound a residence's or a
  marriage's start and end, and the artifact each source belongs to (a
  page fragment belongs to its artifact). The compared properties are the
  single-valued ones (dates and places of birth and death, birth name, an
  entity's own point in time, start and end time, a document's inception,
  a place's identity properties), the parents (a child may have two of one
  gender, which one source states together), and residence and spouse,
  whose dates are compared. The query pairs and filters nothing (ADR-0033):
  `yizkr.disagreement` pairs the values
  that different artifacts attribute and sets aside differences of
  resolution, and the static site's inconsistencies page shows its rows.
  See MODELING.md "Contradictions" for the rules; the offline tests are
  `tests/unit/test_disagreement.py`.
- **place-containment** — the recorded hierarchy the disagreement report
  reads: every location and part-of statement as an edge, with its rank
  and whether it has a stated-in source, and every merge redirect, flat.
  A place value inside another, or the same item through a redirect,
  differs from it only in resolution (ADR-0009). The comparison follows
  an edge a source states and a curator has not deprecated, at any time
  (ADR-0038 §5).
- **person-timeline** — every dated assertion about each person
  (statements, the dates their statements carry as qualifiers, and events
  participated in, by each event's point in time and its own start and
  end time), plural-by-default with sources (R6). An event's start or end
  time is a date value its earliest and latest date bound, and its row
  names the time beside the event's types ("imprisonment (start time)").
  An event's types are folded into one cell (`?eventTypes`), each by its
  English label (its id when it has none), URI-encoded and `|`-separated,
  so that a second type adds no row; its `?what` names its start or end
  time and is unbound for its point in time, and the site shows the
  types sorted and joined ("event, military service (start time)"). An
  event's types and participants are read through `p:`/`ps:` at any rank
  but deprecated, so that a preferred type or participant hides no other
  (#540).
  A qualifier's row names the statement's property, its value and the qualifier: a
  marriage is its spouse statement's start time, its end the end time,
  and a point in time on a spouse or residence statement an observation
  (docs/MODELING.md, "Marriages, residences and other periods"). Each row
  carries the range its source states for that date: a start's earliest
  date and latest start date, an end's earliest end date and latest date,
  a date value's or a point in time's earliest and latest date
  (`tests/integration/test_live_period_dates.py`). It also names the two
  qualifiers that bound the date (`?low` for `?earliest`, `?high` for
  `?latest`), stated or not, and the static site's timeline page and feed
  show each stated bound beside the date under its qualifier's label
  ("1946 (earliest date: 1946; latest start date: 1947)"). Each row also
  carries its statement's sourcing circumstances (`?circumstances`: circa
  qualifies the whole statement, so its start and end rows both carry
  it), folded into one cell, each by its English label (its id when it
  has none), URI-encoded and `|`-separated, so that a second value adds
  no row; the site shows them after the range ("1833 (sourcing
  circumstances: circa)"). Rendered and URL-encoded, it is about 14 KB,
  more than the query service accepts in a GET request line, so a client sends it by POST, as the site and the
  live tests do.
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
  asserts, and where in it, for source re-examination: a row for each
  statement and reference block that cites the artifact, with the
  statement (two same-value statements that differ by a qualifier stay
  two, ADR-0002) and the block's page number, printed page number, image
  region and transcription excerpt, so a statement citing one artifact in
  two blocks gives rows that say where. A statement citing a fragment is
  listed under its whole, the fragment as `?source` and its label as
  `?sourceLabel`; time values carry
  their stated precision.
- **unclassified-entities** — the input to the next vocabulary round
  (ADR-0013 §A): entities held under the holding class
  `unclassified_entity`, one row per English type hint (ADR-0035),
  compared in lower case, with how many entities carry it
  (`?entities`), how many artifacts state it (`?artifacts`) and one
  entity as an example; an entity held without a hint is counted under
  the empty hint. A deprecated kind or hint is not read; an entity a
  curator has also given a modelled kind stays listed. A queue for the
  owner's mint-or-defer decisions, never a defect report
  (`tests/integration/test_live_unclassified.py`). The relation gaps
  beside it are rolled up per run by `yizkr gaps rollup` (ADR-0013 §B).
- **under-witnessed-statements** — the low-scrutiny reappraisal queue
  (ADR-0023): machine-written statements with a single stated-in
  source and no curator statement on the same subject+property, one
  row per statement, time values with their stated precision. A
  queue, not a defect report; scope it with a `VALUES ?prop` filter.

- **died-by** — what the shared "died by" derivation reads (ADR-0040
  §2.4, `yizkr/died_by.py`): each date of death, dated or of unknown
  value, each union ended by a partner's death with the union's
  partners, each civil status of widowed with its point in time, and
  each artifact's own date of making, every one at any rank with its
  stated-in artifact. The derivation works out each "died by" date in
  Python at view time and stores nothing; the site, the disagreement
  report and reconcile's roster read it.

## Adding a query

Add `<name>.rq` next to `publish.py` with a leading `# comment` block
describing it (it becomes the wiki page description), reference
vocabulary via `{P:key}`/`{Q:key}`, and re-run the publisher. The
offline tests (`make test`) verify that every placeholder matches a
real manifest key, so templates and model cannot drift apart; the live
tests (`make test-live`) execute every query against WDQS, assert
the zero-unreferenced-statements invariant, and pin the rows of the
under-witnessed queue, the disagreement report and the unclassified
entities on fictional fixtures
(`tests/integration/test_live_under_witnessed.py`,
`tests/integration/test_live_disagreement.py`,
`tests/integration/test_live_unclassified.py`).
