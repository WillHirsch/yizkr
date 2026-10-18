# Anomaly checks

An unattended whole-corpus run (ADR-0025) has to say when its outputs
look wrong while it unfolds, and stop a slice that is futile without
stopping the rest. The anomaly checks do that. They are cheap and call no
model. They raise alerts for the operator, the live monitor
([docs/MONITOR.md](MONITOR.md)) and the run report
([docs/RUN_REPORT.md](RUN_REPORT.md)), and a check marked
fail-fast pauses its slice through the run's control file.

Alerts are signals. No check changes, moves or drops an output or a
claim (ADR-0002): a batch with an alert on it goes on exactly as it would
without one.

Code: `yizkr.pipeline.anomalies`. The thresholds are in
`src/yizkr/pipeline/anomalies.yaml`, the one place they live, with a
comment on each. The run directory's files are described in
[docs/LLM_CACHE.md](LLM_CACHE.md) (The run directory).

## When they run

- **As each stage output is stored**: a transcription, a batch and a
  critique, once the orchestrator has written and recorded the view.
- **As a stored output is reused**, when the run dispatches its document
  and finds its view current (the `reused` event). So a defect in an
  output made by an earlier run, or under other thresholds, is never
  silent.
- **On the run's own events**, read each time the orchestrator calls the
  checks, from where the last call left off: every stage's end (`done`,
  `failed`), a transcription view the transcription check sets aside on
  reuse (`set-aside`), the LLM cache's events, and the engine's `call`
  events, with what each call's stream reported of it. The orchestrator
  calls the checks when a stage fails too, so a run in which every
  document fails is watched as closely as one in which none does. (A
  pause's timer, below, reads the control file, not the events.)

An output made from answers the LLM cache adopted (an archived
transcription, say) is an output the run stored whose calls the cache
answered: it is checked as it is stored, and its alerts are carried.

Each invocation of a run has its own checks, made at its first stage
outcome: the windows, the first documents of each kind and the
distributions start afresh with every invocation. `yizkr ingest run` and
the scratch driver (`yizkr scratch run`, docs/runbooks/scratch-run.md)
both run their stages through the orchestrator, so both are checked the
same way.

## New and carried

Every alert is `new` or `carried`:

- an output the run stores is `new` when the engine answered at least one
  call of the stage that made it, or the stage makes no call (a text
  file's transcription, a curator's correction, slide text, the GEDCOM
  adapter), and `carried` when the LLM cache answered every call the stage
  made, by key match or adoption;
- a stored output the run reuses is `carried`;
- an alert on the run's dispatches or calls (a rate check) is `new`,
  unless every failure it counts came from answers the cache gave.

These are the monitor's `done` and `reused`: an alert is new exactly
where the monitor counts its stage made in the run.

## An alert

Each alert is a line of `alerts.jsonl` in the run directory
(`rundir.RunDir.alert`), with an `alert` event in `events.jsonl`:

| Field | What it holds |
|---|---|
| `at`, `invocation` | when, and which invocation raised it |
| `stage` | `transcription`, `extraction`, `critique` or `gedcom`; `run` for `anomaly-config`; `write` for registration's flags |
| `check` | the check's name (below) |
| `subject` | the document: the first 12 hex digits of its SHA-1 (empty for `anomaly-config`) |
| `value` | the measured value: a number, a language code (`language-unexpected`), a model id (`model-mismatch`), or `true` for a check that measures a presence |
| `status` | `new` or `carried` |
| `kind` | the document's kind: `text`, `media`, `presentation`, `gedcom`, or `excerpt` (an image a PDF read page by page places, a document of its own: ADR-0008) |
| `limit` | the threshold the value crossed |
| `config` | the first 12 hex digits of the SHA-256 of the thresholds in force, so alerts raised under other thresholds tell apart |

A check adds fields of its own, listed with it. An alert is raised once
per document, stage, check and status in an invocation (`model-mismatch`
once per role and model). No field holds a document's words: a value is
a number, a count, a share, a language code, a model id or `true`, and
the other fields are names from the pipeline's vocabulary.
The monitor shows a value when it is a number or a yes or no.

## The checks

Every check has the key `enabled`. The other keys and their defaults are
in `anomalies.yaml`.

### Transcription

**`transcription-check`**: the transcription check's verdict
(`yizkr.pipeline.transcription_check`, which is the single definition of
a failed transcription; the checks read its verdicts rather than judge
again). It fires on a transcription the check failed in the run, read
from its stage's `failed` event, and on a reused transcription view the
check fails again and the orchestrator sets aside, read from its
`set-aside` event (carried). Value `true`; `failure` is the kind of
failure (`tool-error`, `cut-off`, `not-a-record`, `pages-missing`,
`pages-extra`, `tool-failure`, `empty`, `too-short`), and `signature`
the name of the signature that matched, when one did. Only the kind and the name are read
from the reason; the rest of it can quote the output. A recheck judges a
stored vision transcription with its transcription record, and fails one
without (ADR-0037).

**`transcription-flag`**: a transcription the check passed with a flag:
its wording matched a signature of the flag tier, a phrase in the
engine's voice that a letter may also write (a refusal, a note that the
media could not be read); for a transcription with a record, any
signature, in a page's text or in its notes (ADR-0037 §3). The
transcription goes on downstream; its stage output carries the flag
(`flag`), which this check reads, for review. A stored transcription
carries the flag of the check made now wherever it is read: a run's
reuse of it, an excerpt made from a kept image reading, and a recheck
(ADR-0037 §2). Value `true`; `signature` is the signature's name.

**`transcription-sparse`**: a vision transcription with fewer letters and
digits per page than `min-chars-per-page`, for a document of at least
`min-pages` pages whose page count is known (a PDF, through `pdfinfo`).
The transcription check fails only near-empty output, far below any
written page; this is the softer judgement of yield the check leaves to
alerts. A single page never fires it, since a photograph may carry no
text. Value: letters and digits per page; `pages`.

### Language

The run records how it resolved each document's language
(`yizkr.pipeline.language`): the code, whether it was `detected`, a
`fallback` or an `override`, and the detector's confidence. A language
the run was told, or fell back to for want of any guess, is the
operator's choice and is never judged.

**`language-unexpected`**: a detected language outside `expected`, a list
of codes compared by their primary subtag (`de-at` is `de`). The corpus's
languages are the operator's to list: with none listed, as by default,
any language is expected and the check raises nothing. Value: the code;
`confidence`.

**`language-low-confidence`**: a language detected with less than
`min-confidence`, or a fallback taken on a best guess the detector could
not stand by. Value: the confidence; `resolved` (`detected` or
`fallback`).

**`language-short-text`**: a language detected with at least
`min-confidence` in a transcription of fewer than `max-chars` letters and
digits, where a detector's confidence means little. Value: letters and
digits; `confidence`.

**`language-mismatch`** (on the batch): more than `max-share` of the
batch's excerpts tagged with another language than its transcription's,
over at least `min-count` excerpts. The extraction tags each excerpt in
its own language, so a few are expected (a German phrase quoted in an
English letter); most are not. Value: the share; `count`, `of`,
`language` (the transcription's).

### Extraction

A transcription is non-trivial when it holds at least `min-chars` letters
and digits.

**`zero-entities`**, **`zero-claims`**: a batch with no entities, or no
claims, from a non-trivial transcription. The extractor keeps a batch
without claims only when the answers it accepted say the document holds
none (chunked extraction's claim-free outcome); an extraction whose
answers could not be used fails instead. Value: the transcription's
letters and digits.

**`fallback-manifest`**, **`fallback-segmentation`**: an extraction that
took the line-range tier for a refused entity manifest, or line windows
for a refused segmentation, as its coverage side output records it
(`extraction/coverage.json`, `fallback`). Value `true`.

**`chunk-failures`**: more than `max-share` of an extraction's chunk
calls failing, over at least `min-count` of them: the engine's `call`
events of the extraction with the outcome `error`, among its calls other
than a conversation's opening turn (the entity manifest and the
segmentation). A call whose answer the claude CLI gave up on at the
output limit, whose `error` is `OutputLimitReached`, is a truncation,
below, not a failure. It is judged when the extraction ends, done or
failed. Value: the share; `count`, `of`.

**`truncations`**: more than `max-share` of an extraction's chunk answers
cut off, over at least `min-count` answers, among the answers the engine
or the cache gave its chunk calls. A chunk answer is cut off when:
- its model stopped at the output limit (`max_tokens`), or it has no
  terminal sentinel: the LLM cache rejects it, and the `cache-reject`
  event of the extract role gives a reason that starts `chunk:`;
- the claude CLI gave up on it at the output limit and handed back
  none: the chunk call's `call` event has the outcome `error`, and its
  `error` is `OutputLimitReached`, which the engine raises when it reads
  that give-up in the call's stream (docs/LLM_CACHE.md). The event's
  `api_error`, `max_output_tokens`, does not tell it alone: a call that
  crashed or timed out after the CLI printed its notice names that class
  too, under another `error`, and is a failed call.

Value: the share; `count`, `of`.

**`schema-rejects`**: more than `max-share` of the claims an extraction
was given rejected by validation or unread by the parser, over at least
`min-count`: from its coverage side output (`rejected_claims` and
`unread_objects` beside `claims`), and, for an extraction that failed
because it could use none of them, from the reason its failure gives
(every claim rejected, or objects the parser could not read), which is a
share of one. Value: the share; `count`, `of`.

**`implausible-date`**: a batch's claims dated before the birth of the
person they are about, or more than `max-years-after-birth` years (120
by default) after it: a misread century shows here. Only someone the
batch itself gives a date of birth for (`date_of_birth`, about the same
entity) is judged, and only in an extraction's batch: a GEDCOM date is
the source's own reading. A claim is dated by its own time value and by
its `point_in_time`, `start_time` and `end_time` qualifiers; a birth is
read from its time value and its `earliest_date` and `latest_date`
alone, since a point in time, start or end on it dates its source (the
year a register extract was made), not the birth, and is judged as any
claim's date is. Every date, a birth's included, is judged through the
span its source states: the years its precision names, counted as
Wikibase counts them (a decade from a year ending in 0, a century from a
year ending in 01, whichever of its years is stored), its first year
lowered by its earliest bound and its last raised by its latest one, a
start between `earliest_date` and `latest_start_date` and an end between
`earliest_end_date` and `latest_date`. A stated range widens the years
the precision names and never replaces them (the disagreement report
replaces them): a birth in the 1910s stated "between 1915 and 1916" may
still lie in 1912. The wider reading can only miss an alert, never
invent one. A point in time takes `earliest_date` and `latest_date` too,
so on a period the bounds of its start or end also widen its point in
time. A lone bound leaves its other side open ("after 1890" runs on from
1890), and a claim the source gives as circa (`sourcing_circumstances` →
`circa`) is open on both sides, as is a date with a bound that cannot be
read (in a hand-edited or damaged `batch.json`): neither side of it is
judged. A partly open date is still judged by its closed side: a
residence "before 1890" for someone born in 1911 lies wholly before the
birth, and one "after 2040" for someone born in 1900 wholly after the
threshold. So no date is narrowed, and years alone are compared: a date
in a birth year is never before it. Every birth the batch gives the
person counts and none is preferred (ADR-0002): a claim is counted only
when a date of it lies wholly before the earliest year any of them may
lie in, or wholly more than the threshold after the latest, and once,
before the birth if a date of it is, else after it. No property is
exempt: a memorial or a reburial long after a life is a true positive, a
prompt to look. Alert only: the claims go on as they are. Value: the
claims counted; `before_birth`, `after_birth`, and
`max_years_after_birth`, the threshold in years (the alert has no
`limit`, since its value is a count of claims).

### Distributions

A document's measure is compared with the median of the same measure over
the documents of its class: an outlier is more than `factor` times the
median, or less than the median divided by it, with `smoothing` added to
both, so that a median near zero does not make every value an outlier. A
class is judged once `min-samples` of its documents have the measure.
While the run unfolds a document is compared with those before it; a
recheck compares each with every other.

A document's class is the one its transcription record gives
(`document_class`, one of the manifest's artifact classes or
`unclassified`; ADR-0037), so captions, certificates and registers are
never measured against one another. A document without a record (a text
file, a curator's correction, a deck, a GEDCOM export, a PDF read page by
page) has no class yet: it is `unclassified`, and is compared within its
kind among the unclassified documents. Each distribution alert says so:
`document_class`, and `class_source`, `record` or `none`.

**`entity-yield`**, **`claim-yield`**: entities, or claims, per page of a
batch that has some (none at all is the zero checks' finding). A page is
the medium's own (a PDF's pages, an image's one), a deck's slide, or, for
a text file, `chars-per-page` characters of its transcription. A GEDCOM
file's batch has no pages and is not measured. Value: the yield;
`median`, `samples`, `document_class`, `class_source`.

**`critic-rate`** (on the critique): the critic's findings per item it
reviewed (the batch's entities and claims). Value: the rate; `median`,
`samples`, `document_class`, `class_source`.

### Rates

These read the run's dispatches and calls, and can be fail-fast. When a
rate check has paused its slice, a resume starts it afresh: its window is
emptied, or its count of a kind's first documents begun again, so that
only what comes after the resume counts.

**`stage-failures`**: a stage failing more than `max-share` of its last
`window` dispatches in the invocation (its `done` and `failed` events),
once it has at least `min-count` of them. It fires as the share rises
above the threshold, and again only once it has fallen back to it. Its
slice is the stage; not fail-fast by default. Value: the share; `count`,
`of`, and `pause` when it paused the stage.

**`kind-failures`**: a stage failing more than `max-share` of the first
`first` documents of a kind it takes in the invocation: of all the
kind's documents the run plans, for a kind with fewer. It fires as soon
as the failures pass that share, even before the first documents are all
in, and once per stage and kind. Its slice is the kind; fail-fast by
default. A kind the run plans fewer than `min-documents` documents of (2
by default: a single deck, say, or a single GEDCOM file) is never judged,
so this check never pauses it: no other document of the kind is left for
a pause to protect. Its failure still shows as a failed document, and in
`stage-failures`. A run whose `run.json` records no plan has every kind
judged. Value: the failures; `of` (the documents counted), `first`,
`pause`.

**`call-failures`**: more than `max-share` of a stage's last `window`
engine calls failing (`call` events with the outcome `error`), once it has
made at least `min-count`. Calls paused or interrupted are not counted.
Nor is a call whose answer the claude CLI gave up on at the output limit
(`error` `OutputLimitReached`): the engine answered it, so it says
nothing of an engine down, and it is left out of the window. A call that
crashed or timed out after the CLI printed its notice is counted, as a
failed call. It fires as `stage-failures` does. Its slice is the stage;
fail-fast by default. Value: the share; `count`, `of`, `pause`.

### The engine's model

**`model-mismatch`**: an engine call whose stream reports running on
another model than its role's. Each `call` event carries the model the
call's stream reported (its init event's, as the engine resolved the
configured name: docs/LLM_CACHE.md, `events.jsonl`), and the check
compares it with the model the run manifest names for the role: what the
role's engine client asks for (`roles.<role>.client.model`), else what the
run configures (`roles.<role>.model`; docs/RUN_MANIFEST.md). A role the
manifest names no model for, whose engine runs a default of its own, or
names one by an alias (rundir's alias rule), has no model id to compare
with: there, the model the role's first call in the invocation reported
is the resolution the rest are compared with, so a default or an alias
that moves to another model while the run unfolds is seen. Models are
compared without a context-window selector (`[1m]`). The alert is raised
once per role and model in the invocation (a model with a selector and
one without it being one), on the document whose call first ran on it,
and is always new: the call was made in the run. Its
stage is the call's. Value: the model the call ran on; `limit`, the
role's model; `role`, and `against` (`manifest`, or `first call`). Alert
only: it pauses nothing.

### The configuration itself

**`anomaly-config`**: the thresholds file `$YIZKR_ANOMALIES` names cannot
be used (below). The run goes on under the defaults, and this alert, with
a logged warning, says why (`problem`); its `config` is the defaults'
digest. `yizkr scratch run` refuses such a file at its preflight instead,
in one line, before run.json, a document's directory or an event is
written (#327), and `yizkr scratch estimate` refuses it as the run would;
so a scratch run meets this alert only for a file changed after its
preflight. `yizkr ingest run` has no such preflight, and goes on under
the defaults with this alert.

### Registration

The write raises these as it registers a document
(`Orchestrator._step_register`; ADR-0010 as amended by #371), and
`unhashed-title` also as it writes a GEDCOM's batch after an artifact
registered under one of its media objects' labels, or with a page
fragment under one, or as it refuses a media object whose label and
tagged label registered items both hold (`_step_write`; as amended by
#444), outside the thresholds: they have no keys, cannot be turned off
and pause nothing. Each flags an item registration made or found, an
artifact or a derived medium, that may be an artifact registered before
content hashing, a GEDCOM media object's item, or a media object the
writer refused (below); the item is made all the same
when it is new, and nothing is adopted or changed. An item is flagged on
every write that registers it while the condition holds, so a flag
repeats, run after run, until a curator acts on the older item (hashes,
merges or relabels it, or stops it naming the file), and one a run
killed before it raised it is raised by the next. Stage `write`;
subject, the document being written (an excerpt's write registers each
PDF that places it and every image those PDFs place, so their flags
carry the excerpt as subject); value `true`; always new; no `limit` or
`config`. Fields: `artifact`, the item's QID; `others`, the QIDs of the
items it is flagged against, in the order of their numbers; `medium`,
the first 12 hex digits of the item's media SHA-1. It is the subject's
own unless the item is one registered with the document: a deck's or
PDF's image or, for an excerpt, a PDF that places it or another image
that PDF places; or unless the flag is a GEDCOM write's, whose item is
the artifact registered before it. One alert per item, check and write,
so a deck may raise one for itself and one for each image.

**`shared-file`**: `others` name as their `media file` a wiki file that
holds the item's bytes: the file its upload answered with, or any other
MediaWiki lists for its SHA-1 (`list=allimages`, one listing, every
page of it, per item registered), whatever hash the others carry. A
listing that fails (an HTTP or API error, or a continuation it gave
before) fails the document's registration for that run, as any other
wiki call that fails does: the item, and an artifact's page fragments,
are complete by then, its `unhashed-title` flag, which needs no listing,
is raised all the same, and the next write that registers it raises
`shared-file`.

**`unhashed-title`**: the item's title (for a derived medium, its first
label before any content tag), or its own label before its content tag,
is the label of `others`, items with no hash; or, for a media object
the writer refuses (below), `others` is the item under the media
object's tagged label, which holds the place its item would take: a
registered item, with a hash, when registered items hold both labels.
An item with no hash has
no `media SHA-1` and either names a `media file`, as an artifact
registered before content hashing does, or is an `instance of` an
artifact kind, as the GEDCOM import's item for a media object is (a
label, no file). An artifact fragment, a run item, a person or any item
of another kind is never one unless it names a `media file`. A GEDCOM
written after an artifact registered under exactly a media object's
label, or under it with a content tag, or with a page fragment under it
(`"<title>, page <n>"`), never joins the media object to the artifact or
fragment: the writer makes the media object an item of its own,
labelled `"<label> [media object]"`, and its write raises the flag on
each such artifact against that item, in the order of their items;
registration compares the artifact's title, and its fragments' labels,
with that tagged label too. When registered items hold both the label
and the tagged label, the writer refuses the media object, with the
claims that name it, before its first write, writes the rest of the
batch, and raises the flag on each artifact registered under the label
against the item under the tagged label, as no item is made for the
media object; the write's `refused` names the claims and a warning
names the items by QID. A media object under the label of a person, a
place or any item that is no artifact is an item of its own under the
tagged label too, and its write raises the flag on each artifact
registered under the label, content-tagged as the label is the
person's, as registration does in the other order; with none, it raises
nothing (ADR-0010). Any other refusal raises no alert, only the warning
and the step's `refused` count: a media object refused with no artifact
registered under its label, or with no item yet under its tagged label
(another entity of the batch takes it), and a person, place or other
entity refused because artifacts hold both its name and its tagged
label (`"<name> [not an artifact]"`). None of them is an artifact registered
under the title of another item that may be the same object, the one
question `unhashed-title` asks a curator; each is a clash of labels
that no artifact registration on a reset instance makes, and the
warning names its items by QID.

On a reset instance, a scratch run's, registration alone writes `media
file`, always with `media SHA-1`, and an upload never overwrites a file,
so `shared-file` is never raised there. A scratch run imports the GEDCOM,
though (ADR-0025 §1), so `unhashed-title` is raised there when an
artifact or derived medium is registered under exactly the label of a
media object's item (its title, or without one its file's name or a
fallback), or with a page fragment under it, against that item, and on
every write that registers it while the two share it, whichever of the
two is written first (#444). Which of the two writes raises the flag
again does depend on the order (accepted by the owner, #444): every
later registration of the artifact raises it, and, with the artifact
first, every later write of the GEDCOM does too; with the GEDCOM first,
a later write of the GEDCOM raises nothing. A scratch run writes each
document once on a reset instance, so where one write registers the
artifact its counts are the same in either order. Where several do, as
when excerpts of two PDFs that place the same image are written, they
are not: with the GEDCOM first, each of the N writes that register the
artifact raises the flag; with k of them before the GEDCOM, those k
raise nothing, the GEDCOM's write raises one and each later one raises
one, N - k + 1 in all. That dependence is accepted too (#444): a
count above zero still says the pair exists. With no such homonym a scratch run raises no registration
flag. The monitor shows them by name; the run report's
aggregates count them by check (`shared-file`, `unhashed-title`) and
under the stage `write`, as counts only. The findings count flagged
writes, not distinct items: a document's write and a check, once each,
so an image two decks embed, flagged in each deck's write, counts twice.
The alert count beside them counts each flag raised, so one write that
flags two items under a check counts two. A document's own alerts in
report.json and report.html name them with their items by QID.

## Thresholds

`anomalies.yaml` holds every threshold and its default, with a comment
on each; `pause-timeout-s` and `chars-per-page` sit beside the checks,
and `version`, the file's layout, which is 1. `pause-timeout-s` is a
number of seconds up to the longest wait a thread can make
(`threading.TIMEOUT_MAX`, about 292 years), or `null`; infinity is
refused. To change some, write a YAML file with just those keys, nested
as in `anomalies.yaml`, and name it in `$YIZKR_ANOMALIES`, which every
run's checks load (`anomalies.load_config`, through
`anomalies.checks_for`), a scratch run's and an `ingest run`'s alike;
`yizkr scratch recheck --config FILE` names a file in its place. Its
values replace the defaults, a list replacing the whole list. A key the
defaults do not have is refused, and so is a value out of range (a
`min-count` larger than its `window`, a share above one), so a misspelt
key never passes silently. A scratch run's preflight loads the
thresholds, and refuses a file that cannot be read, a key the defaults
do not have or a value out of range in one line, before anything is
written; `yizkr ingest run` falls back to the defaults with an
`anomaly-config` alert (The configuration itself, above).

The thresholds decide only which alerts are raised and which slices are
paused, never what a stage makes: no view and no LLM cache key depends on
them. So a change reaches every stored output without recomputing a
stage:

- **at the next dispatch**: every invocation checks the views it reuses
  with the thresholds as they are then, and a document whose views all
  stand is re-checked without a call;
- **at once, with a recheck** (below), over a run directory's stored
  outputs, without dispatching anything.

## Fail-fast pauses

A rate check marked `fail-fast` asks for a pause of its slice when it
fires: the stage (`stage-failures`, `call-failures`) or the document kind
(`kind-failures`). Other slices keep running. The request goes through
the run's control file (`RunDir.request`):

```json
{"at": "...", "action": "pause", "scope": "kind:media", "by": "anomaly",
 "reason": "...", "check": "kind-failures"}
```

Its `reason` names the check and what it counted, here
`kind-failures: transcription failed 6 of the first 6 media documents`.

No pause is asked for a slice the control file stops already, whoever
stopped it.

**The timeout.** When the pause has stood for `pause-timeout-s` seconds
and nobody has resumed the slice, the checks append an abort of it:

```json
{"at": "...", "action": "abort", "scope": "kind:media", "by": "anomaly",
 "reason": "kind-failures: not resumed within 1800 s",
 "check": "kind-failures"}
```

The timeout runs from the checks' request for the pause, and a driver's
carry of the pause into a later invocation (below) does not restart it.
Only a pause the checks alone asked for is aborted: once someone else
asks for a pause of the same slice, it is theirs. `pause-timeout-s:
null` holds a pause until someone resumes it.

**The slice stays watched.** From the checks' first pause of a slice to
the end of the run's process, one watch per run directory looks at the
slice: a timer on a thread of its own, so a look comes even while no
stage runs, and every later check the orchestrator calls. Each look
reads the control file as a driver folds it and, when a pause of the
checks alone stands on the slice and the timeout has passed since they
asked for it, appends the abort. When such a pause stands short of the
timeout, the next look comes when it is due; otherwise the next look
comes when the checks' request would reach the timeout, or, that past, a
timeout later. A slice has one pending timer at a time, cancelled when a
look needs a sooner one (after a look that failed). Every look, a
timer's or a check's, judges the time on one clock, the process's
monotonic clock, which the timers wait on too: the timeout counts from
when the checks asked for the pause, read on that clock as they ask. The
wall clock only stamps the control file's requests. So a wall clock that
steps back or falls behind (a virtual machine's clock resynchronised
under load, say) never delays an abort, one that reads ahead and is then
corrected back never brings one forward, and neither moves a timer. A
timer whose look came early would find the pause short of its timeout
and look again when it is due. After an abort, the next look comes when
the checks' request would have reached the timeout, or, that past, a
timeout and a second later, on the same clock. The monotonic clock
counts the time the host is awake: time it spends suspended (a laptop's
lid closed, say) does not count towards a timeout, as the run does no
work then. The look holds the lock the control file's appenders take
(rundir's `_append_line`) from its read to its abort, so no other
process appends a request in between: an operator's resume lands before
the look, and is seen, or after the abort. The lock is POSIX's, so it
does not hold against this process's own threads, nor after any
descriptor on the file in this process is closed. A look that fails (the
control file cannot be opened, read or appended to, on a full disk say,
or a timer cannot start) is an `anomaly-error` event: under the watch's
own invocation, `watch-<hex>`, when a timer made it, and under the
check's otherwise. Either way it stops the look at no other slice, and
leaves the slice a timer a timeout later when none is pending: a slice
never goes unwatched.

The slice stays watched after its pause is aborted, resumed or cleared,
because a driver that invokes the run again in the same process, as the
scratch driver does after a wait for a subscription limit, asks again,
after its new start marker, for the requests in force before it, with
the `by` and the reason they had and a new time (a carry). The carried
pause stands again, and no invocation's checks asked for it. Each look
times it from the checks' own request, which the watch keeps, never from
the carry's new time. So a pause carried through one re-invocation after
another is still aborted once the timeout has passed since the checks
asked for it, whether or not any check runs in the new invocation, and
even when a look comes between the start marker and the carry. A pause
whose timeout passes during the wait is aborted there, and the driver
carries the abort instead; a pause carried from a reading of the file
older than the checks' abort is aborted again at the next look. The
watch lives in the process whose checks asked for the pause, and no
pause outlives it unwatched: the scratch driver carries requests only
into its own next invocation, in the same process, and a new process's
first invocation carries none, its start marker voiding every request
before it. So a run invoked again in a new process (after a crash, say)
never waits on a pause no timer watches: the slice is dispatched again,
and its checks pause it afresh, with a watch of their own, if it fails
again. A driver that carried pauses into a new process would have to
watch them itself, or report them and end the run rather than wait.

**How a driver sees it.** A driver folds `control.jsonl` with
`rundir.fold_control`, as it folds every request, the monitor's
included: the slice is `paused` from the pause on, and `aborted` from
the abort for the rest of the invocation; a resume ends the pause.
`yizkr ingest run` honours a kind's pause or abort between documents,
and a stage's or a kind's before every stage: a document it does not
dispatch keeps its status, and one whose stage is held is `paused`. A
driver that waits on a paused slice, as the scratch driver does, finds
the abort in the same fold and stops waiting: the checks end the wait,
carried pauses included, without any code of the driver's beyond folding
the control file and running in the same process. A new invocation's
start marker clears every pause and abort, apart from those a driver
asks for again (above).

The checks run wherever the orchestrator stores, reuses or fails a
stage: in `Orchestrator._transcription`, `_derive`, `_critique`,
`_reused_view` and `_run_stage`. `yizkr ingest run` reaches them through
`_advance`, and a driver that takes each stage of each document through
those methods reaches them the same way. A pause or an abort only stops
dispatch: in-flight calls finish, every completed output stays stored,
and the next invocation takes the slice up where it stopped.

## Recheck

```
uv run yizkr scratch recheck [<run>] [--runs DIR] [--config FILE]
```

takes the run as the other `scratch` commands do: a run directory (an
`ingest run`'s state directory too), or a run's id in the runs directory
(`--runs`, `var/scratch` by default), and the latest run there when none
is named (#327). It checks the outputs stored in the run directory's
document directories again, the excerpts' directories
(`excerpts/<sha1>/`, ADR-0008) included, under the thresholds as they
are now (`--config` names a file of thresholds in place of the one
`$YIZKR_ANOMALIES` names), without dispatching anything or recomputing
any stage (`anomalies.recheck`).
Each stored vision or text-file transcription meets the transcription
check as it is now, as a run's reuse of it would, and each stored
transcription, a PDF read page by page's included, carries the flag the
check raises now, not the one stored with it; and every output meets the
per-output checks and the distributions over the whole run. The rate
checks, which read a run's dispatches and calls rather than its outputs,
are not checked again. Its alerts, all carried, are appended to the
run's alerts under an invocation of their own, `recheck-<hex>`, which an
`anomaly-recheck` event opens with the thresholds' digest and the number
of documents it read, excerpts included. An excerpt's transcription,
batch and critique meet the same checks as a top-level document's, and
its alerts carry the kind `excerpt`, as the run's own do; the
distributions measure an excerpt within its class, or among the
unclassified excerpts. A document whose stored outputs the checks cannot
read (a `batch.json` that does not hold a batch's shape, say) is an
`anomaly-error` event, and the others are checked. Nothing is written
into a document's directory. It prints how many alerts it appended,
where, and the thresholds' digest, then the count of each check. A file
of thresholds that cannot be used (`--config`, or the one
`$YIZKR_ANOMALIES` names) is refused in one line, with nothing appended,
and so is a run whose events or alerts this user cannot append to.

## What the checks never do

- change, move or drop an output, a view or a claim, or write anything
  into a document's directory: they write only `alerts.jsonl`, their
  events and their control requests;
- fail a document or a run: a check that raises is an `anomaly-error`
  event, which names the exception but not its message, and a logged
  warning, and the stage goes on as if nothing had been checked;
- call a model, read the wiki, or read or write the LLM cache.

## Limits

- **Truncations are seen partly without the LLM cache**: a cut-off chunk
  answer is rejected through the cache, whose event says so. Without it
  (`--no-cache`) the retry is only another call, and only the calls the
  claude CLI gave up on at the output limit, which their own events
  show, are counted.
- **Calls are measured where the run made them.** Chunk failures and
  truncations are judged on extractions made in the invocation; a reused
  batch keeps no record of its calls, so it is judged on its stored
  outputs alone. So is `model-mismatch`: an answer the LLM cache gives
  makes no call and has no call event, and the model that made it is
  recorded in its cache entry alone (docs/LLM_CACHE.md, Layout). A
  recheck does not look at calls, and so checks no model again.
- **The first documents of a kind are compared with few**: while the run
  unfolds, a document is compared with those before it, and the first
  `min-samples` of a kind with none. A recheck compares every document
  with the whole run.
- **Every invocation starts afresh**: the rolling windows, the first
  documents of each kind and the distributions do not carry over.
- **The monitor counts every invocation's alerts**, a recheck's included;
  each alert names its invocation for a reader that wants the latest.
