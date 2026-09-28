# Anomaly checks

An unattended whole-corpus run (ADR-0025) has to say when its outputs
look wrong while it unfolds, and stop a slice that is futile without
stopping the rest. The anomaly checks do that. They are cheap and call no
model. They raise alerts for the operator, the live monitor
([docs/MONITOR.md](MONITOR.md)) and the run report, and a check marked
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
- **On the run's own events**, read as they are appended: every stage's
  end (`done`, `failed`), a transcription view the transcription check
  sets aside on reuse (`set-aside`), the LLM cache's events, and the
  engine's `call` events. The orchestrator also calls the checks when a
  stage fails, so a run in which every document fails is watched as
  closely as one in which none does.

An output made from answers the LLM cache adopted (an archived
transcription, say) is an output the run stored whose calls the cache
answered: it is checked as it is stored, and its alerts are carried.

Each invocation of a run has its own checks, made at its first stage
outcome: the windows, the first documents of each kind and the
distributions start afresh with every invocation. `yizkr ingest run` runs
its stages through the orchestrator, as the scratch driver (#194) is to,
so both are checked the same way.

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
| `stage` | `transcription`, `extraction`, `critique` or `gedcom`; `run` for `anomaly-config` |
| `check` | the check's name (below) |
| `subject` | the document: the first 12 hex digits of its SHA-1 (empty for `anomaly-config`) |
| `value` | the measured value: a number, a language code (`language-unexpected`), or `true` for a check that measures a presence |
| `status` | `new` or `carried` |
| `kind` | the document's kind: `text`, `media`, `presentation` or `gedcom` |
| `limit` | the threshold the value crossed |
| `config` | the first 12 hex digits of the SHA-256 of the thresholds in force, so alerts raised under other thresholds tell apart |

A check adds fields of its own, listed with it. An alert is raised once
per document, stage, check and status in an invocation. No field holds a
document's words: a value is a number, a count, a share, a language code
or `true`, and the other fields are names from the pipeline's vocabulary.
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
failure (`tool-error`, `tool-failure`, `refusal`, `empty`, `too-short`),
and `signature` the name of the signature that matched, when one did.
Only the kind and the name are read from the reason; the rest of it can
quote the output.

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
segmentation). It is judged when the extraction ends, done or failed.
Value: the share; `count`, `of`.

**`truncations`**: more than `max-share` of an extraction's chunk answers
cut off before their terminal sentinel, over at least `min-count`
answers: the LLM cache's rejections of chunk answers (`cache-reject`
events of the extract role whose reason starts `chunk:`), among the
answers the engine or the cache gave its chunk calls. Value: the share;
`count`, `of`.

**`schema-rejects`**: more than `max-share` of the claims an extraction
was given rejected by validation or unread by the parser, over at least
`min-count`: from its coverage side output (`rejected_claims` and
`unread_objects` beside `claims`), and, for an extraction that failed
because it could use none of them, from the reason its failure gives
(every claim rejected, or objects the parser could not read), which is a
share of one. Value: the share; `count`, `of`.

### Distributions

A document's measure is compared with the median of the same measure over
the documents of its kind: an outlier is more than `factor` times the
median, or less than the median divided by it, with `smoothing` added to
both, so that a median near zero does not make every value an outlier. A
kind is judged once `min-samples` of its documents have the measure.
While the run unfolds a document is compared with those before it; a
recheck compares each with every other.

**`entity-yield`**, **`claim-yield`**: entities, or claims, per page of a
batch that has some (none at all is the zero checks' finding). A page is
the medium's own (a PDF's pages, an image's one), a deck's slide, or, for
a text file, `chars-per-page` characters of its transcription. A GEDCOM
file's batch has no pages and is not measured. Value: the yield;
`median`, `samples`.

**`critic-rate`** (on the critique): the critic's findings per item it
reviewed (the batch's entities and claims). Value: the rate; `median`,
`samples`.

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
default. Value: the failures; `of` (the documents counted), `first`,
`pause`.

**`call-failures`**: more than `max-share` of a stage's last `window`
engine calls failing (`call` events with the outcome `error`; calls
paused or interrupted are not counted), once it has made at least
`min-count`. It fires as `stage-failures` does. Its slice is the stage;
fail-fast by default. Value: the share; `count`, `of`, `pause`.

### The configuration itself

**`anomaly-config`**: the thresholds file `$YIZKR_ANOMALIES` names cannot
be used (below). The run goes on under the defaults, and this alert, with
a logged warning, says why (`problem`).

## Thresholds

`anomalies.yaml` holds every threshold and its default, with a comment
on each; `pause-timeout-s` and `chars-per-page` sit beside the checks. To
change some, write a YAML file with just those keys, nested as in
`anomalies.yaml`, and name it in `$YIZKR_ANOMALIES`; the scratch driver
may pass one of its own (`anomalies.load_config`, `anomalies.attach`).
Its values replace the defaults, a list replacing the whole list. A key
the defaults do not have is refused, and so is a value out of range (a
`min-count` larger than its `window`, a share above one), so a misspelt
key never passes silently.

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
 "reason": "kind-failures: transcription failed 6 of the first 6 media documents",
 "check": "kind-failures"}
```

No pause is asked for a slice the control file stops already, whoever
stopped it.

**The timeout.** When the pause has stood for `pause-timeout-s` seconds
and nobody has resumed the slice, the checks append an abort of it:

```json
{"at": "...", "action": "abort", "scope": "kind:media", "by": "anomaly",
 "reason": "kind-failures: not resumed within 1800 s", "check": "kind-failures"}
```

A timer started with the pause does it on a thread of its own in the
run's process, so the abort comes even while no stage runs, and every
later check looks too. Only a pause the checks alone asked for is
aborted: once someone else asks for a pause of the same slice, it is
theirs. `pause-timeout-s: null` holds a pause until someone resumes it.

**How a driver sees it.** A driver folds `control.jsonl` with
`rundir.fold_control`, as it folds every request, the monitor's
included: the slice is `paused` from the pause on, and `aborted` from the
abort for the rest of the invocation; a resume ends the pause. `yizkr
ingest run` honours a kind's pause or abort between documents, and a
stage's or a kind's before every stage: a document it does not dispatch
keeps its status, and one whose stage is held is `paused`. A driver that
waits on a paused slice, as the scratch driver (#194) is to, finds the
abort in the same fold and stops waiting: the checks end the wait
without any help from it. A new invocation's start marker clears every
pause and abort. A pause or an abort only stops dispatch: in-flight
calls finish, every completed output stays stored, and the next
invocation takes the slice up where it stopped.

## Recheck

```
uv run python -m yizkr.pipeline.anomalies recheck <run dir> [--config FILE]
```

checks the outputs stored in a run directory's document directories
again, under the thresholds as they are now (`--config` names a file of
thresholds in place of the one `$YIZKR_ANOMALIES` names), without
dispatching anything or recomputing any stage (`anomalies.recheck`).
Each stored vision or text-file transcription meets the transcription
check as it is now, as a run's reuse of it would, and every output meets
the per-output checks and the distributions over the whole run. The rate
checks, which read a run's dispatches and calls rather than its outputs,
are not checked again. Its alerts, all carried, are appended to the
run's alerts under an invocation of their own, `recheck-<hex>`, which an
`anomaly-recheck` event opens with the thresholds' digest and the number
of documents. Nothing is written into a document's directory. It prints
how many alerts it appended, by check.

## What the checks never do

- change, move or drop an output, a view or a claim, or write anything
  into a document's directory: they write only `alerts.jsonl`, their
  events and their control requests;
- fail a document or a run: a check that raises is an `anomaly-error`
  event, which names the exception but not its message, and a logged
  warning, and the stage goes on as if nothing had been checked;
- call a model, read the wiki, or read or write the LLM cache.

## Limits

- **Truncations are seen only with the LLM cache**: a cut-off chunk
  answer is rejected through the cache, whose event says so. Without it
  (`--no-cache`) the retry is only another call.
- **Calls are measured where the run made them.** Chunk failures and
  truncations are judged on extractions made in the invocation; a reused
  batch keeps no record of its calls, so it is judged on its stored
  outputs alone.
- **The first documents of a kind are compared with few**: while the run
  unfolds, a document is compared with those before it, and the first
  `min-samples` of a kind with none. A recheck compares every document
  with the whole run.
- **Every invocation starts afresh**: the rolling windows, the first
  documents of each kind and the distributions do not carry over.
- **The monitor counts every invocation's alerts**, a recheck's included;
  each alert names its invocation for a reader that wants the latest.
