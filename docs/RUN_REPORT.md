# The run report

A whole-corpus scratch run is unattended and long (ADR-0025), and its run
directory records everything it did. The run report reads that record
after the fact, or while the run is still going, and says what the run
did, per stage and per document: `report.html` and `report.json` for the
operator, which stay local, and `report.md`, the aggregates alone, which
is what goes on an issue or a PR (#145, and the scratch-diff evidence of
docs/AGENT_PROTOCOL.md §5).

Code: `yizkr.pipeline.report`. The run directory's files are described in
[`docs/LLM_CACHE.md`](LLM_CACHE.md) (The run directory) and
[`docs/RUN_MANIFEST.md`](RUN_MANIFEST.md); the live monitor, which shows a
run as it unfolds, in [`docs/MONITOR.md`](MONITOR.md).

## Using it

```
uv run yizkr scratch report [<run>] [--runs DIR]
```

`<run>` is a run id in the runs directory (`--runs`, default
`var/scratch`) or a run directory's path; left out, it is the latest run
there. Any run directory will do, an `ingest run` state directory
included, and so will a run in any state: finished, still running,
stopped by the control file, paused for the subscription, interrupted,
refused, failed, or killed. The command writes three files into the run
directory's `.report` folder and prints where:

| File | Holds | Leaves the machine |
|---|---|---|
| `report.md` | the aggregates alone: counts, rates and times, but for the documents per language | yes: it is what is posted |
| `report.html` | the aggregates (the documents per language among them), each invocation and control request with its reason, and every document one by one (a drill-down per document) | never |
| `report.json` | everything the page shows, as data | never |

Run again, it replaces each file whole. The folder is made private to its
owner (mode 700) every time, one there already included; a `.report` that
is a symbolic link, or no folder, is refused. The folder is opened once,
and each file is written through it as it was opened, never through its
path again, so a folder swapped for another, or for a link, once opened
takes none of them; each file is private to its owner (mode 600). No
document's directory can take the name: the orchestrator names them by
slugs, which never start with a dot, and a directory it sets aside by
`.<name>.superseded-<n>`.

A run directory is refused, with a message, when it holds no `run.json`,
when its `run.json` cannot be read or holds no JSON object, or when its
`events.jsonl` cannot be read: the report reads the run by its plan, its
invocations and its events. An `alerts.jsonl` or `control.jsonl` that
cannot be read leaves its section "not available". Anything else of
another shape than the pipeline writes (a field of another type in a
log, a view or `run.json`, a line that is no JSON object) counts as
nothing and is passed over: a malformed file never stops the report.

**It reads the run directory, and the model manifest.** It never reads
the wiki or its query service, the engine, the LLM cache or the corpus: a
unit test writes the report under the refusing wiki of
`tests/unit/test_no_wiki_before_write.py` with an engine factory that
fails if asked. It reads:

- `run.json`: the plan (each document's SHA-1, file, kind and stages),
  what shaped the run, and each invocation's start, end and outcome;
- `events.jsonl`, `alerts.jsonl` and `control.jsonl`;
- each document's directory, found by the SHA-1 its `state.json`
  records: its `transcription.json`, `batch.json`, `critique.json` and
  `extraction/coverage.json`. A directory set aside is passed over, and
  of two that name one SHA-1 (a file with another's bytes), the one that
  is no duplicate holds the views;
- a corpus stage's outputs: the clustering stage's
  `corpus/clustering/identity-map.json` (#105), its summary alone, and
  the write stage's `corpus/write/record.json` (#200), its counts alone;
- and one file outside the run directory: the model manifest,
  `src/yizkr/model/manifest.yaml` as the code has it when the report is
  made. Its properties and item keys are the names `report.md` may give
  claims and entities (below), so a property or kind the run's batches
  hold that today's manifest does not is counted as `other`.

The logs are read a line at a time and folded as they are read, so a
whole-corpus run's log is never held in memory: a unit test reads a log of
several megabytes within a third of its size. They are read as
`rundir.run_state` reads them, run.json before the events and again after
them, and all of it again while an invocation starts or ends in between,
and a stage an ended invocation left started is interrupted by the rule
the monitor and `run_state` use (`rundir.interrupted`). So the report's
statuses agree with `yizkr stages state` and the monitor's. Unit tests
check both for a run that finished, one still running, one paused with a
stage cut short, one stopped, and one whose first or latest invocation
was killed.

## What it shows

**The run.** Its mode and project stage; whether the latest invocation
is running or how it ended, and why (the class of the exception it
names, or the slices that stopped it); whether it went through every
stage; each invocation's span, summed into the run's wall time (a killed
invocation's runs to its last event, as in the monitor); the barrier; the
code's commit and version, and whether the code differed from its commit
(`dirty`); the engine and its CLI; and each role's model, effort, epoch,
refresh and client configuration hash. A run still running is a
snapshot, and says so.

**Documents.** The documents planned (a SHA-1 planned twice is one
document) by kind; languages, by the transcriptions read, and by kind
(in `report.html` and `report.json` alone: see below);
the transcription routes, by what made each transcription (`llm-vision`,
`pdf-pages` for a PDF read page by page, `pptx-adapter` for a deck,
`text-file`, `correction`); the pages of the PDFs read page by page, by
method, and the images placed within them and read on their own (PDFs
that place any, images placed, distinct images, images per PDF); the
images the decks embed on their slides and read on their own, counted
alike (`deck_images`); the excerpts among the documents; and
how each language was resolved (record, detected, fallback, override); the
documents that failed, each once: those whose stage failed, as their
latest events leave them, and those the latest invocation failed outside
their stages, with the class of the exception or the label their failure
leads with. The kind table counts the planned documents of each kind
that failed; a failed document the run does not plan (a source that
could not be read has no SHA-1 to plan) is counted apart, by the kind
its event gives. Also the views set aside to be made again, and the
documents with a view that could not be read.

**Stages.** One row per stage, in plan order. Its columns partition the
stage's documents, as their latest events leave them:

| Column | Counts |
|---|---|
| pending | not dispatched yet |
| held | pending, held by the control file |
| running | started by the running invocation |
| interrupted | started by an invocation that is over: the next starts it again |
| computed | completed, and made in the run: the engine answered at least one of its calls, or it makes none (a text file's transcription, the GEDCOM adapter) |
| reused | completed, and the LLM cache answered every call it made (by the answers' reasons: `key match`, `adopted`, or several joined by `+`), or its view came from before the run's events and stands (`view current`) |
| needs attention | completed with attention reasons (a fallback tier, no claims), however it came about |
| failed | its latest attempt failed |

A view made in one invocation and reused by a later one of the same run
(every stage a resumed scratch run takes again) counts as that
invocation made it: computed, or reused by the cache's reasons. The live
monitor counts it `view current`, since it shows what each invocation
does; the report says how the run came by the view. A scratch run keeps
its settings across its invocations (docs/RUN_MANIFEST.md), so every view
it counts as made was made with them. An `ingest run` state directory
need not: each `ingest run` records the settings it ran with in place of
the last one's, and a view one run made stands in the next while its
inputs are unchanged, whatever that run's settings. So the report counts
such a view as made, and cannot say with which settings, until each
invocation records its own (#255).

For each stage, the report gives its reuse by reason, its attention and
failure labels, its calls (to the engine, from the cache, and the
engine's call events) and its time (the attempts that ended, start to
end), with its cache share, its failure rate, of all its documents,
pending ones included, and its attention rate, of its completed ones.

**What sent a computed stage to the engine** is what the cache's events
record of its calls: each miss's reason, `absent` (the cache held no
answer under the call's key), `rejected` (a caller rejected the answer it
held) or `refresh` (the role was refreshed), and each replay of a
conversation's opening turn for its forks (`replay`, or `replay changed`
when it answered otherwise); `no call` for a stage that makes none, and
`no cache` for calls made without the cache. Which component of an absent
call's key changed (the prompt, a file, the model, the configuration, the
attempt, the epoch) is kept by the cache's entry, not the run directory,
and shows only against a baseline run's calls: the run-to-run diff (#198)
attributes a recompute to it (ADR-0025 §3, ADR-0032 §11).

**Corpus stages.** Each corpus stage the run registered or ran, with its
state and attention labels, or `not run`; a run with none says so. A
corpus stage's attention reasons are listed by label alone, each once
and with no count (#576): a corpus stage has one attempt, so a count
would be 1 whatever the reason holds, and the reason's own details,
which may quote a document, are never shown. The table points to the
Clusters section, which gives the clustering stage's figures (ambiguous
and conflicting clusters, ambiguous pairs, ...) from the identity map's
summary. A per-document stage's Needs attention column still counts
documents per label.

**Write.** Once the run's write stage has run (#200;
yizkr.pipeline.scratchwrite): the documents written, the statements
written new, those that gained a reference, the claims already there and
those the writer refused, and of them those whose item value is their
subject's item, as the identity map may make it
(`aggregate.content.write.claims_self_referencing`, absent for a write
that did not count them; #491), apart from extraction's rejects, and
the claims refused by reason
(`aggregate.content.write.claims_refused_by_reason`, #552): every reason
of `yizkr.pipeline.writer.REFUSAL_REASONS`, in that order, each refused
claim counted under the first that refused it, so they sum to the claims
refused: `retired_or_reference_property` (a retired or reference
property, ADR-0036, ADR-0035 §4), `subject_rule` (a document-subject
claim whose subject is not its own source, a caption whose subject its
source does not place, or an artifact subject on a property that takes
none: ADR-0035 §3.4, ADR-0039 §2), `text` (a text the instance cannot
hold as it is, the claim's own, a locator's or a mentioned entity's
label), `side_rule` (an entity the side rule blocks, ADR-0010) and
`self_reference` (an item value that is the subject's item, counted
again in `claims_self_referencing`); absent, not 0, unless every
document's write record counts them, and a reason absent unless every
record gives it a count. Then the items minted, the judgement notes
written (the noted run blocks, ADR-0035 §4; #410), the batches refused
whole before registration, the documents that failed, and whether the
instance-contents key was recorded; or that the instance held what the
run's write key makes, so nothing was reset or written. In both, the
depicts claims the write left out of the batches: no scratch run writes
a depicts statement (ADR-0005, ADR-0025 §1), and each one the batches
hold is counted here (`aggregate.content.depicts.left_out`). Counts
alone, never a document, a QID or a label.

**Place hierarchy.** Once the run's place stage has run (#201;
yizkr.pipeline.scratchwrite, ADR-0009, ADR-0038 §8.5), in
`aggregate.content.place_hierarchy`: the place questions asked (which
existing place a container a record states names) and the calls they
took; the answers that named a place, those `uncertain`, and the
questions left unanswered (`unanswered`); the stated links left out with
no place to name (`links_left_out_unnamed`: a container that names no
place, or a place that is none of the graph's); the links written as
statements of their own (`links_written`), those an equal statement
gained as references (`links_referenced`) and those already there; the
links a record states that no claim of its document gives
(`record_only_links`, a measure of extraction's recall on containment,
ADR-0038 owner question 3) and of them those written, with no time, each
citing its container's evidence (`record_only_links_written`); the links
an uncertain answer left with no inner place, or that followed a
container left out and so were never asked (`links_unresolved`); the
answer lines rejected by the answer's schema; the links refused as each
would close a cycle (`refused_cycles`) and the questions refused as
conflicts (answered twice differently); and whether the
instance-contents key was recorded; or that the instance held what the
run's key makes, so nothing was asked again. A stated link is counted
with its container's evidence: two records of one document that resolve
to one item, with one chain but other evidence, count as two links
(ADR-0038 §8.5). Counts alone, never a QID or a label. Each refused
cycle is named, by its two places' labels and the document that states
it, in report.json (`place_cycles`) and report.html alone, which stay on
the machine (ADR-0038 owner question 4). The place stage's calls count
under their own role, `places`, in the calls' lines by role, apart from
the extraction's; the clustering stage's adjudication of ambiguous pairs
counts under `adjudication`, apart from the critique's, whose engine,
model and effort it uses.

**Different-source contradictions.** Once the run's site stage has run
(#575; yizkr.pipeline.scratchwrite), in `aggregate.content.contradictions`:
the disagreements the site's inconsistencies page lists, as the stage
counted them on the written graph and kept them in its record
(`corpus/site/record.json`), since the next reset removes that graph:
`different_source`, their total, and `by_property`, their count by
property, keyed by manifest key (`other` for one the manifest does not
hold), the most frequent first. Counts alone, never a QID, a label or a
value. A run whose site stage has not run, one whose site record cannot
be read, and one whose site record does not count them (made before the
stage did, or holding a count that is no whole number) say they are not
available, with why, never 0.

**Anomalies.** The alerts by check and by stage, each finding (a
document or the run, a stage and a check) counted once: new when any of
its alerts was new (on an output the engine answered in the run), carried
otherwise (docs/ANOMALIES.md). Also the checks that failed and the
rechecks. Registration's flags count here under their checks,
`shared-file` and `unhashed-title`, and the stage `write`, as counts
alone: the findings count flagged writes (a document's write and a
check, once each), the alerts each flag raised; the QIDs of the items flagged and of those
they are flagged against appear only in a document's own alert rows in
report.json and report.html, which are local-only (docs/ANOMALIES.md,
"Registration"). The write's `self-reference` alerts, a self-loop the
writer refused, count here too, under their check and the stage
`write`, as counts alone (docs/ANOMALIES.md, "The write").

**Content**, from the views of the stages that completed:

- entities by kind, and those not extracted;
- claims by property, and the `name` claims, name forms of no stated
  type (ADR-0011), by source kind;
- rejects: the claims validation rejected and the objects the parser
  could not read, from each extraction's coverage, with their rate among
  the claims given; and how many extractions failed with every claim
  rejected or unread. A failed extraction's reason can quote a model's
  answer, which can quote a document, so the number of claims it gives
  stays with its document, in `report.html` and `report.json`. The
  rejects are counted by reason too (#456), from each coverage's
  `rejected_by_reason`: the codes of `claims.REJECT_REASONS`,
  `document_subject` (a document-subject property whose subject is not
  its own source), `placed_subject` (a caption whose subject is no
  artifact its source places), `artifact_subject` (another property
  with an artifact subject), `schema` (a claim that does not parse) and
  `manifest` (any other manifest problem), with the extractions whose
  coverage counts none (`aggregate.content.rejects.by_reason`,
  `by_reason_absent`);
- excerpts not in their source (#580): the claims each extraction kept
  whose `transcription_excerpt` its transcription does not hold,
  compared as core/verbatim.py compares a container's evidence (folded,
  typographic quotes and dashes read plain, a soft hyphen dropped,
  whitespace and line breaks collapsed, a hyphen at a line's end joined,
  or kept, with or without a space after it, for an excerpt that
  crosses it, whole words only; #612), from each
  coverage's `quotes`. A miss is a flag, never a reject: the claim is
  kept and written. The aggregate gives the
  claims checked, those with no excerpt, the misses and their rate among
  those checked, the documents with one, and the misses by property,
  each property folded into the vocabulary (`other` for one it does not
  know), with the extractions whose coverage predates the count counted
  apart, never as zero (`aggregate.content.quotes`: `extractions`,
  `checked`, `without_excerpt`, `not_in_source`, `documents_with_miss`,
  `rate`, `not_in_source_by_property`, `absent`). A document's own counts are in
  `report.json` and `report.html` (`coverage.quotes`, null for a
  coverage that predates them). Which claims missed, each with its
  excerpt and the transcription line nearest to it, stays in the
  extraction's local `quotes.json`, never in a report;
- what sources say of the artifacts themselves (ADR-0039): the captions
  given, a container's verbatim words about an image it places, and the
  claims a document gives about itself, by property
  (`aggregate.content.container_claims`: `captions`,
  `document_subject_by_property`);
- zero-claim documents, by kind, and batches with no entity; the chunks
  and the fallback tiers taken;
- the chunks asked again because every claim of their answer lacked the
  same required field (#501), summed from each extraction's
  `coverage.json` (`retried_chunks`), with the extractions that asked
  one, and those whose coverage predates the count counted apart, never
  as zero (`aggregate.content.retried_chunks`: `chunks`, `extractions`,
  `absent`). A document's own count is in `report.json` and
  `report.html` (`coverage.retried_chunks`, null for a coverage that
  predates it). A call the safety classifier refused (#621) is not
  counted in coverage: it is counted with the call errors, by its
  error's class (`ClassifierRefused`), and a refusal that fails a chunk
  or the extraction is named in the document's failure reason;
- mention records and judgement notes, summed from each extraction's
  `coverage.json` (ADR-0035 §4, §5, #420): the mention records given,
  and those by label status (`name`, `role`, `kinship`, `courtesy`,
  `placeholder`, `description`, `toponym`, `deictic`, `generic`,
  `adjective`, the
  statuses of `claims.PersonStructure` and `claims.PlaceStructure`;
  `report.RECORD_STATUSES`); the unsplit name forms the records given
  hold (a person's `unsplit`: forms whose given name and surname the
  source does not tell apart, ADR-0035 §2.1); the recordless mentions;
  the records set aside; the place containers checked, and those
  dropped alone from records that stand, by reason (`no-evidence`,
  `evidence-not-in-source`, `evidence-does-not-name`, `malformed`;
  `claims.CONTAINER_REASONS`, ADR-0035 §5 as amended by #569 and #607),
  a drop being no record set aside and no reason among theirs, never
  quoted (each container checked, with its evidence and the
  transcription line it was compared with, or a malformed one's entry
  and its validation error, stays in the extraction's local
  `containers.json`; a coverage written before #607 may also count the
  containers kept whose evidence is their name alone, which the report
  no longer reads); the reasons records were set aside by, by
  category (`schema`, `kind`, `addition`, `initial`, `no name part`,
  `no toponym`; `report.SET_ASIDE_REASONS`); the readings that set
  nothing aside, by category (`dropped`, `read as text`;
  `report.READING_REASONS`), counted apart from those reasons (#611);
  the notes by category (`kind`, `relation`, `date_reading`,
  `identity`, `other`, the categories of `claims.JudgementNote`); and
  the notes set aside. Counts only, never a label, a reason's text or a
  note's text.
  `coverage.json`'s `records.set_aside` counts reasons, not records: a
  record that does not parse gives one `schema` (besides any
  `read as text` or `dropped` reasons given before it), and one of the
  wrong kind one `kind`, but any other record gives one for each problem
  found in it (each part of a name that no form of the entity holds gives its
  own `addition`, and a place's name is checked as written and again by
  its toponym and its head), so the reasons can outnumber the records
  set aside, which `records.set_aside_records` counts, once each. A
  `dropped` reason counts a name part dropped for an empty string
  (ADR-0035 §5, #497), and the record it leaves often stands. A
  `read as text` reason counts an entry of a list of strings written as
  an object holding its text, read as that text (ADR-0035 §5, #514).
  Neither sets anything aside: the record then stands or is set aside on
  its other reasons. `coverage.json` counts both among `set_aside`, but
  the report counts them apart, as readings (#611): the headline
  "reasons records were set aside" (`set_aside_reasons`,
  `set_aside_by_reason`) counts only the reasons that set a record
  aside, and the readings have a line ("Fields read so their record
  could stand", `readings`) and a table ("Reading (nothing set aside)",
  `readings_by_reason`) of their own, and in a document's drill-down
  "readings, nothing set aside". So a change that reads more entries as
  text, and so sets fewer records aside, shows its reasons set aside
  falling and its readings rising, never the reasons rising. Three
  kinds of count stay apart, each in its own line and table: the
  reasons a record was set aside by, the readings that set nothing
  aside, and the reasons a place container was dropped alone, its
  record standing ("Container dropped reason",
  `containers_dropped_by_reason`, `malformed` among them, #607), which
  are never a record's reasons or readings. A
  record set aside on a mention of a kind that takes one (a
  person, place, organisation or family) leaves that mention recordless,
  so the recordless mentions are those given no record and those whose
  record was set aside. `coverage.json` counts an entity's note set aside
  among the same reasons, once, as `note`; the report counts it as a note
  set aside, never as a record's reason (a claim whose note fails is a
  counted reject instead). `records.appellation_labels` counts the
  persons whose label was one of the document's appellations of them and
  became the name their record gives, and `records.appellation_parts`
  the record name parts that are, whole, one of the document's
  appellations of the person (ADR-0018, ADR-0035 §8, as amended by
  #499). `coverage.json` also gives `records.appellation_label_names`,
  the `name` claims dropped for restating a new label, which the report
  does not sum. A reason, status or category the report does
  not know is counted as `other`, as everywhere in the aggregates; since
  `other` is also a note category, the notes' `other` holds both. An
  extraction whose coverage holds no such counts is counted apart,
  never as zero,
  and the report says how many there were of each: one whose entity
  manifest fell back to line ranges (the line-range tier counts no
  records, and all its mentions are recordless), and one whose coverage
  predates these counts (or holds its records without its notes, or its
  notes without its records, which the extraction never writes). So the
  recordless mentions the clustering stage counts (the identity map's
  `summary.recordless_mentions`, below) also include the line-range
  fallback's mentions, and that figure need not equal the records'
  recordless count. An
  extraction whose `coverage.json` is missing, cannot be read or holds no
  object is in none of these figures, as in none of the rejects'. Nor
  are the judgement notes counted that a line-range fallback's claims
  carry: its coverage counts no notes. A coverage that holds its records
  and notes but predates `records.set_aside_records`,
  `records.unsplit_forms`, `records.appellation_labels` or
  `records.appellation_parts`, or the place containers'
  `records.containers_checked` and `records.containers_dropped` (or
  gives one that is no count) still
  counts
  its other figures, but is counted in none of the counts it lacks,
  never as zero: the report says how many extractions it counted lack
  each (`absent.set_aside_records`,
  `absent.unsplit_forms`, `absent.appellation_labels`,
  `absent.appellation_parts`, `absent.containers`), and a document's own
  view gives the count as
  null and its drill-down says it is not counted. A reason, status or
  category whose count is no count (not a whole number of at least 0) is
  dropped, never a 0 row; a genuine 0 stays.
  Each document's own counts are in `report.json` and `report.html`, its
  reasons, statuses and categories folded as the aggregates' are, and
  `report.json`'s `aggregate.content.records` (`extractions`, `given`,
  `recordless`, `by_status`, `unsplit_forms`, `set_aside_records`,
  `appellation_labels`, `appellation_parts`, `set_aside_reasons`,
  `set_aside_by_reason`, `readings`, `readings_by_reason`,
  `containers_checked`, `containers_dropped`,
  `containers_dropped_by_reason`, `absent.predating`,
  `absent.line_range`, `absent.set_aside_records`,
  `absent.unsplit_forms`, `absent.appellation_labels`,
  `absent.appellation_parts`, `absent.containers`) and
  `aggregate.content.notes`
  (`total`, `by_category`, `set_aside`) sit beside the rejects, chunks
  and fallback tiers, where the run-to-run diff (#198) is to compare them
  with the other extraction counts;
- critic findings by axis and by category: the statement-part each
  doubts (subject, predicate, value or existence, ADR-0023) and its
  category from the critic's fixed list (`critique.CATEGORIES`; the
  three that apply ADR-0003's support test to a citation read where it
  stands are `unsupported_containment`, `evidence_does_not_state` and,
  since #607, `excerpt_does_not_state`), and the findings that failed
  their checks, counted apart as invalid (a finding of a claim key that
  names no claim among them);
- clusters, from the clustering stage's identity map: confident merges
  (whole confident candidate clusters), confident component merges (the
  confident components of ambiguous candidate clusters, #568) and their
  mentions, adjudicated merges (those the adjudicator's `same` verdicts
  made in a development-stage run, #106) and their mentions, the members
  a `different` verdict left alone, ambiguous clusters left split (those
  that end in more than one cluster for want of a settlement: a member
  left apart, a pending pair unsettled between merged components, or a
  contradiction an adjudicated cluster fell back from; counted with the
  members left apart alone, the members of their merged components
  not among them), conflicting clusters (and how many of
  them hard), singletons, the recordless mentions (those of a kind that
  takes a mention record and have none, which never link confidently,
  ADR-0035 §7), the mentions clustered by kind, the candidate
  pairs, the adjudication estimate, the adjudication itself (the pairs
  asked and the calls made, the verdicts `same`, `different` and
  `uncertain`, the pairs left without a verdict, and the answer lines
  refused by the schema, the calls refused whole and the pairs answered
  two ways), and the hub keys not blocked on and
  the mentions holding them; `not run` when the run has no clustering
  stage, and the map's state when it is missing or of another format.
  The map's summary also holds `confident_links_only`, what merging over
  the confident links alone would give; the report leaves it out, since
  it overstates what merges: it takes in the confident links of
  conflicting clusters, and the components a re-check keeps apart for a
  contradiction among their members.

**Calls and the cache.** The calls that reached the engine and those the
cache answered, in all and per role; hits and misses by reason; resumes,
replays (and those that answered otherwise), rejections and adoptions;
and what the engine's call events say: outcomes, errors by exception
class, engine time, waits for the subscription, attempts a limit
refused, why calls were held, the model the engine reported for each
call, and per stage the calls stopped at the output limit: answers cut
off there (stop reason `max_tokens`) and calls the engine gave up on
(error `OutputLimitReached`, told by the error alone as the truncations
check tells it), as counts only (#390), in `report.json`'s
`aggregate.calls.output_limit_stops` (stage to `cut_off` and `gave_up`).
A run whose call events carry no stop reason, no API error and no
give-up has
`{"recorded": false}` there, and reads `not recorded: the run's call
events carry no stop reason`, never `none`, which says the stops were
recorded and there were none. Three kinds of run read so: one made
before the engine recorded why a call stopped (before #441, every run on
the Anthropic API engine); one that made no call, every answer coming
from the cache; and one whose every call gave no stop reason (on the
Anthropic API engine, every call failed or its response gave no stop
reason as a plain token; on the claude-code engine, every call failed
before its stream reported one). On either engine an answered call's
event carries the stop reason the engine gave, verbatim (`end_turn`,
`max_tokens`, `refusal`, `pause_turn`, `stop_sequence`, ...), and the
model the engine reported: the claude-code engine's from the call's
stream, the Anthropic API engine's from its response (#441). Only
`max_tokens` counts as `cut_off`; any other stop reason only makes the
run's stops recorded, so a run whose calls all ended at `end_turn` or
`refusal` reads `none`, not `not recorded`. A give-up an attempt replays from the
cache makes no call, so it is no stop: its `cache-hit` says `gave_up:
true`, and the cache's lines count those hits per stage of their role,
as `give-ups replayed from the cache` (`aggregate.calls.give_ups_replayed`,
counts only). The calls that reached the engine are the cache's misses
and replays; a run made without the cache (`ingest run --no-cache`) has no
cache events, so its engine calls are its call events, as the monitor
counts them.

**Usage.** The tokens each call's stream reported it used (input,
output, cache creation and cache read), per stage and role as the call
events name them, and in all, with how many calls reported any. The sums
are of what was reported: a call cut off before its result reports
nothing, so where some did, the sums fall short of what was used. A run
whose call events carry no usage says so: `not recorded`. The report
totals them from the call events as it reads the log.

**Control requests.** How many, by action and by who asked, and the
slices paused and aborted.

**Not available.** What the run directory cannot give is said to be, and
never counted as none:

- depicts claims left out, and the write's counts: until the write stage
  has run (above);
- the place stage's questions and links: until it has run (above);
- different-source contradictions: until the site stage has run, or when
  its record does not count them (a record made before it did, #575).
- flags by tier and class: no stage makes flags yet. `_flags` in
  `yizkr.pipeline.report` is where the report reads them once the flags
  stage keeps them among its outputs (#112); until then it says they are
  not available.

The page's drill-down lists, with links, the documents that failed (as
`report.md` counts them), those that need attention, those with alerts,
and those with a view that could not be read.

## report.md: aggregates alone

`report.md` is rendered from the report's `aggregate` section and nothing
else, and the aggregates hold counts, rates, times and names of the
pipeline's own vocabulary, never a document's name, a file name, a path,
a label, an excerpt, a quote, or a number a document gives
(docs/AGENT_PROTOCOL.md §7):

- every name in them is checked against a fixed vocabulary (stages,
  kinds, roles, statuses, reasons, outcomes, holds, requesters, the
  anomaly checks' names, the critic's axes and categories, the reasons a
  mention record is set aside by, the mention records' statuses, the
  judgement notes' categories) or
  against the model
  manifest (claim properties and entity kinds), and one it does not know
  is counted as `other`;
- a reason is shown by its leading label only, and only when the label is
  one the pipeline writes (an attention reason's, or a failure outside a
  stage's), or by the class of the exception a failure names, without its
  module path; anything else is `other`;
- no language is named: the aggregates count the documents per language,
  by two- or three-letter code with at most a region, but `report.md`
  leaves that table out and says the counts are in the local report
  alone, since the codes would list the languages the corpus is written
  in, which the operator keeps private in their anomaly thresholds
  (#458). An alert is counted by its check and stage alone, never by its
  value or limit (a `language-unexpected` alert's value is a language,
  and its limit the private list itself). The run diff's postable
  summary follows the same rules: never counts or deltas per language,
  nor an alert's value or limit (ADR-0025 §4);
- a model is shown only as a full model id, exactly as the run's
  configuration names it: it is the operator's configuration, not corpus
  content, so an operator who posts `report.md` should not name a model
  after a corpus language; the run id, the code's commit
  and the configuration hashes only in their own forms; and of the code's
  difference from its commit only that there is one, never its hash,
  which would confirm a guess at a small difference;
- no document appears, not even by its SHA-1, and no path: not the
  corpus folder, the cache or the never-ingest list, nor that list's hash,
  which a short list could be recovered from by guessing (#255).

Unit tests (`tests/unit/test_run_report.py`) guard it four ways. A
fictional run's file names, paths, labels, excerpts, reasons, alert
fields and identity map carry names from the example universe
(docs/EXAMPLES.md), and none of them, nor any document's SHA-1, may
appear in `report.md` or in any string of the aggregates. The same run
carries a name in every field the aggregates name something by (a stage,
a kind, a role, a reason, an outcome, a hold, an error, a requester, a
route, a fallback tier, a critic's axis and category, a set-aside reason,
a record status, a note category, the mode, the state, the engine,
a model, the code's version), and each must fold into `other`. The same
run's transcriptions in four languages, and an alert whose value is a
language, must leave no language code in `report.md`, and `report.html`
must still list them. And the run rewritten, with every document's name
and SHA-1 replaced in every file of its directory, must give a
`report.md` the same byte for byte. Still,
re-read `report.md` before posting it (the self-review of
docs/AGENT_PROTOCOL.md §7).

`report.html` and `report.json` hold file names, and reasons that can
quote a document, so they are local-only data like the rest of the run
directory (docs/AGENT_PROTOCOL.md §6): never posted, committed or sent to
a cloud session.

## Not yet

- The scratch driver does not run the report as a run closes (#145); run
  `yizkr scratch report` after the run, or at any time during it.
- Flag counts by tier and class wait for the flags stage (#112), as above.
