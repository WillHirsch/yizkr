# The live run monitor

A whole-corpus scratch run is unattended and long (ADR-0025). The monitor
shows what it is doing while it unfolds, so that a clear misalignment in
the early stages is visible without waiting for the whole run, and a
futile run can be paused or aborted with no completed output lost. It
attaches to any run directory, whenever and wherever the run was
started, and only reads it, apart from the operator's control requests
and its status page.

Code: `yizkr.pipeline.monitor`. The run directory's files are described
in [`docs/LLM_CACHE.md`](LLM_CACHE.md) (The run directory) and
[`docs/RUN_MANIFEST.md`](RUN_MANIFEST.md); `yizkr.pipeline.rundir` reads
and writes them. What a run did, per stage and per document, once it has
ended or at any point before, is the run report's to say (`yizkr scratch
report`, [`docs/RUN_REPORT.md`](RUN_REPORT.md)).

## Using it

A scratch run is followed and stopped with the scratch driver's commands
(docs/runbooks/scratch-run.md), which call `monitor.watch` and
`monitor.request`:

```
uv run yizkr scratch watch [<run>] [--interval 2] [--once] [--no-page] [--serve ADDRESS:PORT]
uv run yizkr scratch pause|resume|abort [<run>] [--stage NAME | --kind NAME] [--reason TEXT]
```

`<run>` is a run id in the runs directory (`--runs`, default
`var/scratch`) or a run directory's path; left out, it is the latest run
there, and the command says which run it wrote to. Their requests are
written by `operator`, and a resume of an aborted slice is not written,
as in the view (below). The monitor also
runs as a module, on any run directory, an `ingest run` state directory
included:

```
uv run python -m yizkr.pipeline.monitor watch <run dir> [--interval 2] [--once] [--no-page] [--serve ADDRESS:PORT]
uv run python -m yizkr.pipeline.monitor pause|resume|abort <run dir> [--stage NAME | --kind NAME] [--reason TEXT]
```

`watch` redraws the view in place every `--interval` seconds, until you
press `q` or Ctrl-C; quitting the monitor never stops the run. `--once`
prints one view and exits. `--no-page` leaves the status page unwritten.
`--serve` serves the page (Serving the page, below).

The view is drawn through `curses` (below) only when the standard input
and output are both a terminal and terminfo knows how to move that
terminal's cursor (`cup`). Otherwise the view is printed afresh each
time the run changes, without colour, and no keys are read: when the
output is piped or redirected, when the input is (`watch < /dev/null`;
Python's `curses` can only read keys from a terminal on the standard
input), or when the terminal is `TERM=dumb` or one terminfo does not
know. Printed to a terminal, its footer then ends with `Ctrl-C: quit
(keys are read only from a terminal)`. A printed line, like each line of
`--once`, stops one column short of a width (`COLUMNS` first, else the
terminal's own width, else 100), counted in characters, not in the
columns a terminal gives them: a line of wide characters may be wider
than that, and wrap. Display widths are left to `curses`, which has
them from the C library; the monitor keeps no width table of its own.

On a terminal the view is drawn through the standard library's `curses`
(ncursesw on Linux), which ends when the monitor quits, however it quits,
and leaves the terminal as it found it. The view fills the window; the
footer (the status page's note, the served address, a command's answer,
the help and the key hint) keeps the window's bottom rows when there is
room for it, so a longer or shorter view above it does not move it. The
title and the section heads are bold, the dim rows dim, and the states
coloured from colour pairs: green, yellow and red on the terminal's own
background (black where it has none). `NO_COLOR`, set and not empty,
turns every style off. Each refresh draws the whole view, and `curses`
sends the terminal only the cells that changed: a refresh in which
nothing changed sends nothing. `curses` takes each character's width
from the C library (the locale's character type is set from the
environment while the view is open, so wide characters draw correctly)
and clips each line at the window's edge, with no `...`; nothing wraps.
The view's size is always the window's: exported `LINES` and `COLUMNS`
are set aside while it is open and put back when it closes. Keys are
read as they are typed, waiting `--interval` seconds for one before the
next refresh; an Escape, and whatever comes with it (the rest of a key
`curses` does not know), is no key. A resize draws the view at the new
size at once (a resize signalled just as the monitor starts waiting for
a key shows at the next key or refresh). Press Ctrl-L to send the whole
screen again, as in other terminal programs, if something else has
written over it. After Ctrl-Z and `fg`, `curses` puts the view back as
it was, a half-typed command included; `curses` ignores a Ctrl-Z that
comes while it is sending the screen, so one pressed then needs
pressing again. The served page writes nothing to the terminal, neither
its requests nor the errors of a client that drops the connection. So
the served address reaches the terminal once at the start and again
only when the whole screen is sent (Ctrl-L, a resize, or its row moving
when the footer changes): editors that forward ports they find in a
terminal's output (VS Code's port auto-forwarding, set to read output)
are not told of a new server every refresh.

**Commands.** Press `:` or Enter in the view to type one on the command
line, which opens on the window's bottom row and shows as much of the
line's end as fits (Backspace edits it, Enter carries it out, Escape or
an empty line types nothing, and a line takes at most 500 characters):

```
pause|resume|abort [run | stage NAME | kind NAME] [reason]
help
quit
```

The help, the key hint and every refusal of a command give this grammar.
A slice may also be written `stage:NAME` or `kind:NAME`; with no slice
the command is for the whole run. Each command appends one request to
the run's `control.jsonl` (`rundir.RunDir.request`, `by: "monitor"`), and
the view says what the slice's state is then, read as the slices are
(What it reads, below), or that it cannot be told while the file itself
cannot be read, by the driver either. The driver honours the
requests: a pause holds dispatch for its slice until a resume; an abort
holds it for the rest of the invocation, and invoking the run again
resumes it (docs/LLM_CACHE.md, `control.jsonl`). Neither loses a
completed output: in-flight calls finish, and every completed call is in
the cache.

- In the view, an abort asks for confirmation (`y`), on the command
  line; the module's `abort` command line does not.
- A resume of an aborted slice is not written, since it would change
  nothing; the view says to invoke the run again.
- A stage or kind the run knows of no document in, from its plan or its
  events (a typo, or a corpus stage no document plans), is written, and
  the view and the module's command line note that no document the run
  knows has that slice.
- A name is written without surrounding spaces. An empty name is refused
  rather than taken for the whole run, and so is a name with a space in
  it, which no slice's name has, and a directory without `run.json`.

## What it shows

**The header.** The run id, mode and project stage; whether the latest
invocation is running or how it ended (`finished`, `stopped`,
`interrupted`, `paused`, `refused`, `failed`, with its reason), as
`run.json` records it or, failing that, as the invocation's closing
event says; the invocations so far; the time the run has been running,
summed over its invocations; how long ago the last event came; when the
run started; the planned documents by kind; and, once an invocation has
closed, its documents per status.

**Stages.** One row per stage, in the order the plan gives them, then
any other stage the events name. The columns partition the stage's
documents:

| Column | Counts |
|---|---|
| `pending` | not dispatched yet, including those the control file holds (`held`), whatever they were before, and those an ended invocation left started (interrupted: the next invocation starts them again) |
| `running` | started by the running invocation, not yet ended |
| `done` | made in this run: the engine answered at least one of the stage's calls, or the stage makes none (a text file's transcription, the GEDCOM adapter) |
| `reused` | the view was current and stands (`view current`), or the LLM cache answered every call the stage made (by the answers' reasons: `key match`, `adopted`, or several joined by `+`) |
| `attention` | done or reused, with attention reasons (a fallback tier, no claims); these count here, not under done or reused |
| `failed` | the latest attempt failed |
| `engine`, `cached` | the stage's calls the engine answered (a cache miss, or a replay of a conversation's opening turn) and the cache answered |

The counts agree with `rundir.run_state`, which tells a stage cut short
by the same rule (`rundir.interrupted`, given the latest invocation and
whether it has ended as `rundir.Invocations` reads them from `run.json`
and the events): `done` and `reused` together are its done, the
interrupted stages among `pending` are its `interrupted`, and the rest of
`pending` is its pending (#300). `yizkr stages state` and the scratch
driver's closing report therefore show a stage an ended invocation left
started as interrupted, as this view does, and not as running. A
SHA-1 planned twice (two files with the same bytes) is one document, with
the stages both entries plan. A planned document or stage counts even
when its SHA-1 or name is empty (a stage without a name shows as `?`),
as `run_state` counts it; a stage event with an empty document or stage
is left out, as `run_state` leaves it. Under the table, the notes
break the counts down: reuse by reason, attention and failure labels,
held and interrupted stages, and the documents that failed in the
latest invocation.

The cache's events and the engine's call events name the document, not
always the stage: each is counted for the stage that document has
started and not yet ended, since a document's stages run one at a time.
Events outside a stage count in the run's totals only.

**Corpus stages** (#561). A scratch run's corpus stages, after the
barrier: clustering, then the write side's reset, register, write,
places and site (docs/runbooks/scratch-run.md, The write side). One line
each, in order: those `run.json` records the latest invocation is to run
(`scratch.corpus_stages`, up to `scratch.until`; none when it stops
before the barrier), then any other its events name. A run that has
none, an `ingest run`, shows none. Each is shown in its state in the
latest invocation, since every invocation past the barrier runs every
corpus stage afresh:

| State | When |
|---|---|
| `pending` | the latest invocation has not started it, whatever an earlier one did with it; or the control file held it in an invocation that has ended |
| `held` | the control file holds it (a pause or an abort of the run or the stage), with the request that holds it |
| `running` | started by the running invocation, for how long |
| `interrupted` | started by an invocation that has ended, by the rule `rundir.run_state` reads stages by: the next invocation runs it again |
| `done` | done, and how long it took, with the reasons its output needs attention |
| `skipped` | done, having found that the instance holds what the run's key makes (reset, register, write, places) |
| `failed` | failed, after how long, with the exception's name only |

What a stage is doing comes from its latest `progress` event (What it
reads, below), shown with counts only:

- **reset:** its step, as it starts ("resetting: step 6 of 9, start the
  stack", the step's name shown only when it is lowercase words), or
  that it resumed a reset this run made; a reset that is done has gone
  through every step.
- **register and write:** the documents done, out of those the stage
  plans (registered or failed), and the documents failed, the batches
  refused whole, and the statements written, referenced and skipped and
  the claims refused so far.
- **places:** "waiting for the query service", since when; then
  "reading" (the graph's places and the documents' records); then the
  question batches answered out of those it asks; then the links written
  out of those it plans; and the engine calls and the cache's answers its
  role (`places`) made in this attempt, from the cache's events.
- **site:** "waiting for the query service", since when, or "building".

A count at zero is left out. While a stage runs, its rate is the units
it did between its phase's first progress event and its latest, by
their times, shown as a whole number from 10 a minute up; the time to
go is what is left of its phase at that rate. Since both come from the
events alone, a running stage whose latest progress event is more than
a minute old (other than while it waits for the query service) also
says how long ago that event came: a stage that has stalled keeps its
last rate.
Neither is shown for a reset's steps, which differ too much in length,
and the rate is "unknown yet" until a phase has two events at least a
second apart that differ in count. A stage written before the progress
events existed, or one that emits none (clustering), shows its state
from its `started`, `done`, `failed` and `held` events alone.

**Documents that failed.** Every document that failed in the latest
invocation, in a stage or outside its stages (a source or a `state.json`
that could not be read, say), by kind and file suffix and by reason, and
how many failed before their source was read. Never by file name. A
document that fails twice in one invocation (the same file, or the same
SHA-1 when no file is named) counts once, by its later failure.

**Calls.** The engine calls and the cache's answers, with the share the
cache answered and its reasons, resumes, replays, rejections and
adoptions, and the same per role, where a scratch run's corpus stages
name their calls apart: `adjudication` for the clustering stage's
adjudication of ambiguous pairs (#106), `places` for the place stage.
When the engine reports its calls (below): how many, their mean
duration, the outcomes other than `ok` and
the errors by name, the waits for the subscription, the attempts a limit
refused and why calls were held, and, when the latest invocation paused
for the subscription, why and from when it can go on.

**To go.** The stages still to run (pending, running, and failed, which
the next invocation runs again) and the engine calls they will take at
the run's rates so far. Each stage of each kind is rated at the average
of its attempts that ended, done or failed, since kinds differ (a text
file's transcription makes no call, a scan's does). A view that stood
made no call and is left out of the rate, which it would only lower; a
stage the cache answered counts, since the cache will answer others like
it. Stages of kinds with no ended attempt yet are listed apart, and the
calls are `unknown`, not `~0`, while nothing to go has a rate. The canary
ordering (#194) gives every kind a rate early. A scratch run's corpus
stages still to run (pending, held, running, interrupted or failed)
follow, a running stage with its count and time to go where it has
them, or that it waits for the query service: `...; then corpus stages:
clustering, reset, ...` before the barrier. Once the latest invocation
is past the barrier (its `barrier` event, which the driver emits before
its first corpus stage), the corpus stages come first, and the documents' stages still
to go, those that failed and those after them, are the next
invocation's, which runs them again: `corpus stages: write (412 of
1,200 documents, ~25m 10s), places, site; besides 3 documents' stages
for the next invocation`, with no estimate of their calls. Only once
neither is left does `to go` say `nothing`.

**In flight.** Each document the running invocation is in the middle of:
its SHA-1 prefix, kind and stage, and for how long.

**Slices.** Each slice the operator can stop: the run, each stage and
each kind, and any other scope a current request names. The kinds are
the run plan's (orchestrate.DOCUMENT_STAGES): `text`, `media` (a
photograph or other image, read whole), `pdf`, `presentation` (a deck),
`gedcom` and `excerpt` (an image a PDF or a deck places, read as a
document of its own), so `kind pdf` stops the PDFs and leaves the
photographs running (ADR-0008, as amended by #502). A stopped slice
shows since when it has been stopped (a second pause gives a new reason,
not a new start), and who stopped it and why. The states are the driver's
own: `control.jsonl` is read with rundir's reader and folded with its
fold (`read_jsonl`, `fold_control`), so a slice the monitor shows as
stopped is one the driver stops. Requests before the latest invocation's
start marker no longer apply. While the file itself cannot be read (its
mode forbids it, say), the driver cannot read it either, and every
slice's state is `unknown`, with no since, who or why. The terminal view
says so in one line, "state unknown: the control file cannot be read",
and the page lists every slice as unknown, under a line that says why;
the header of each adds "run state unknown".

**A log that cannot be read.** While `events.jsonl` cannot be read (its
mode forbids it, say), the driver's reader cannot read it either, and
`rundir.run_state` raises. The views then show nothing from it, rather
than every planned stage pending: the stages, the calls and the
documents in flight each say, in their place, that they are unknown and
why, in one line of the terminal view and one of the page ("counts
unknown: events.jsonl cannot be read", for the stages). The header's
last event is `unknown`, and so is the time the run has been running
when an invocation was killed, since only its events say how far it
ran; a run whose `run.json` records no invocation, or is missing, is in
an `unknown` state. The plan, the slices and the alerts are shown as
the other files give them. While `alerts.jsonl` cannot be read, the
alerts say "unknown: alerts.jsonl cannot be read" in the same way, and
the stage counts, which do not come from it, still agree with
`run_state`.

**Alerts.** The counts, new and carried, and by check; and the latest
ten, newest first: time, `new` or `carried`, stage, check, document and
kind, and the measured value. Which checks raise them, how, and when an
alert is new or carried is in [docs/ANOMALIES.md](ANOMALIES.md) (New and
carried): new where this view counts the output's stage done, carried
where it counts it reused.

## What the views never show

The terminal view and the page never quote a document:

- **A document** appears by its SHA-1 prefix (ten hex digits) and kind
  only, never by its file name, title or content. An alert's subject
  names a document only when it is a whole SHA-1, or a prefix of exactly
  one SHA-1 the run knows, and then by that document's own prefix; any
  other subject (a word made of the letters a to f, say) shows as `-`.
- **Names from the pipeline's vocabulary** (stages, kinds, roles,
  checks, statuses, outcomes, holds, windows, who asked for a request)
  are shown only when they are lowercase ASCII words; anything else shows
  as `?`.
- **A failure** shows the exception its reason starts with (a builtin
  exception, or a CamelCase class name ending in `Error`, `Failed`,
  `Paused` and the like; not a name such as `McLaudler`), and otherwise,
  like **an attention reason**, only its leading label, before a colon, a
  bracket, a quote or an equals sign, and only when that label is a
  lowercase phrase (`its source cannot be read`, `no claims extracted`);
  any other reason shows as `other`.
- **A corpus stage's progress** is shown by its counts, which are
  whole numbers, and its phase, unit and a reset's step, each a name from
  the pipeline's vocabulary (above). Nothing else of a `progress` event is
  shown, and its writers put nothing else in it.
- **An alert's value** is shown when it is a number or a yes or no, and
  by its type otherwise (`(text)`, `(list)`, `(object)`): an alert's text
  can quote a transcription. Its other fields are not shown.
- **Three fields are shown as they are written**, cut short: a control
  request's `reason`, a cache hit's `reason` (`key match`, or an
  adoption's reason), and an invocation's `reason` (in `run.json`, or its
  closing event: the control request that stopped it, or an exception's
  name). Their writers must never put a document's words in them.
- **Nothing else** free-text is shown: not the reasons of held stages,
  set-asides, cache misses, rejections and adoptions, nor a set-aside's
  kept name, nor a failed document's file name.
- Every character shown is printable: control characters, line and
  paragraph separators and lone surrogates become spaces, so nothing can
  move the cursor, break a line or fail to encode.

## What it reads

The monitor reads `events.jsonl` and `alerts.jsonl` incrementally: each
refresh parses only the lines appended since the last one, so it keeps
up with a long run. A line without its newline yet (a writer mid-line)
waits for the next refresh. A complete line that is not a JSON object (a
writer killed mid-line, then another appending after it) is skipped and
counted. A log that shrinks or is replaced, or whose first line changes
(one deleted and written afresh on a reused inode, or rewritten in
place), starts the monitor over: it is read again from its start.

A log is looked at as the driver's reader looks at it (`read_jsonl`). A
path that holds no regular file (none yet, a directory, a FIFO) holds no
entries, and is never opened. One that cannot be read, by that reader
either (the file is there, but its mode forbids reading it, or an I/O
error cuts a read short, say), leaves nothing read from it standing,
since the driver has no last good read of it: what the monitor read of
it is forgotten, and what would come from it is unknown (A log that
cannot be read, above). A read cut short takes none of its lines. Every
refresh tries it again, and the one that can read it reads it from its
start, so no record appended meanwhile is skipped and none read before
counts twice. The refresh at which a log cannot be read, and the one at
which it can again, count as changes: the view is drawn again.

`run.json` is read after the logs, since an invocation records itself
there before its first event, and parsed again only when its inode, size
or modification time changes. One that cannot be read (half written by a
writer that does not replace it whole, say) leaves the last good one
standing. `control.jsonl` is read as the driver reads it (above), and
so are its requests: a scope that is empty, false or zero is the whole
run, and a start marker clears every slice whatever its scope. Any other
request whose scope is set but is not a name (a list, say) is left out
and counted as skipped: the driver's fold fails on one it cannot hash,
and one it can stops no slice. The file is read whole at every refresh,
as the driver reads it before each stage it dispatches, so a change of
its mode, which leaves its size and modification time as they were,
shows at once. One that cannot be read at all leaves no last good one
standing, since the driver has none: every slice's state is unknown
(Slices, above) until a refresh reads it. A command, typed in the view
or given on the module's command line, reads the file the same way.

From `events.jsonl` it reads the stage events (`started`, `done`,
`reused`, `failed`, `held`), the LLM cache's events, the engine's call
events, `document-failed`, `run-start`, `run-end` and `run-interrupted`,
`set-aside`, and the corpus stages' `progress` events. `alert` events
are ignored: `alerts.jsonl` holds the alerts. A stage event that names
no document (no `subject`) is a corpus stage's; one whose document is
empty is left out, as above.

**Progress events** (#561) are optional, so a run directory written
before them still shows its corpus stages. The write side
(`yizkr.pipeline.scratchwrite`, Progress) writes one while a corpus stage
runs: as register and write start and after each document, as places
starts asking and after each question batch, as it starts writing links
and after each, at the start of each of a reset's steps, when places and
site start waiting for the query service, places starts reading the
graph, site starts building, or a stage skips. A progress event outside its stage's running attempt (of
another invocation, or before the stage's start) is ignored. Every field
but `event`, `stage` and `phase` is there only when it has something to
say:

| Field | What it holds |
|---|---|
| `stage` | the corpus stage |
| `phase` | what it is doing: `resetting`, `resumed`, `registering`, `writing`, `waiting` (for the query service), `reading` (the place stage reads the graph and the records), `asking`, `building` or `skipped` |
| `unit` | what `done` and `of` count: `steps`, `documents`, `batches` or `links` |
| `done`, `of` | how many it has done (a reset's steps: before the one starting), out of how many |
| `step` | a reset's step, by the reset's own name for it |
| `failed`, `refused` | documents that failed in this attempt; batches refused whole (register) |
| `written`, `referenced`, `skipped`, `claims_refused` | the write's statements so far: new, referenced on an equal statement, skipped, and the claims refused |

**Call events** (#101) are optional. A run without them is shown from the
cache's events alone; a run without the cache (`--no-cache`) and without
call events shows no calls and no estimate. The engine writes one for
every call that reaches it, with or without the cache (a hit is none; a
miss or a replay is one). Every field but `event` is there only when it
has something to say:

| Field | What it holds |
|---|---|
| `event` | `call` |
| `role`, `stage`, `subject`, `kind` | as the other events have them |
| `call` | `complete`, `open` or `fork` |
| `outcome` | `ok`, `error`, `paused` or `interrupted` |
| `duration_s` | how long the call took, in seconds |
| `wait_s` | seconds it waited for the subscription |
| `refused` | how many of its attempts a limit refused |
| `held` | why it last waited or paused: `usage limit`, `reserve`, `rate limit`, `overloaded`, `usage credits` or `closed` |
| `error` | the exception's name, never its message |
| `window`, `resumes_at` | for a pause for a subscription limit: the window, and when the pass can go on (UTC) |

A call event also says whether it is a replay of a conversation's
opening turn (`replay`), and carries what the call's stream reported of
it (`model`, `cli_version`, `usage`, `num_turns`, `subtype`,
`stop_reason`, `api_error`; docs/LLM_CACHE.md), which the monitor does
not show: `rundir.usage_counts` totals the usage per stage and role, and
the anomaly checks compare the model with the run's and count the chunk
calls the claude CLI gave up on at the output limit as truncations
(docs/ANOMALIES.md).

Engine calls are counted from the cache's events when the run has them,
and from call events otherwise. A `duration_s` or `wait_s` that is not a
finite, non-negative number of seconds, one too large for a float
included, is left out; a total too large to show (waits summed past a
float's range, refusals summed past the digits Python writes out, 4,300
by default) shows as `?`.

**Documents that failed** (#215): `document-failed` names a document that
failed, in a stage (which has its own `failed` event too) or outside its
stages, with its `file`, its `subject` once its source was read, its
`kind` and the `reason`. **Closing events** (#215): every invocation ends
with `run-end` or `run-interrupted`, with its `outcome`, `reason` and
`statuses` (documents per status); only a killed invocation ends with
neither.

## The status page

`watch` writes the page to `status/index.html` in the run directory: one
self-contained HTML file, with inline styles and no script, font, image
or other external asset, that reloads itself every five seconds. It is
written beside its place and moved there whole, so a reader sees the old
page or the new one; at most every five seconds while events arrive, and
at least every thirty. It is written only into a run directory that has
`run.json` and is still there: the monitor makes the `status` directory
and nothing else, never the run directory, and does not write through a
`status` that is a symbolic link. No document's directory takes the name
`status`: the driver keeps it free beside the run files
(docs/LLM_CACHE.md). A page that cannot be written is reported in the
view, and the monitor goes on.

The page lives in the run directory under `var/`, which also holds the
documents' views, so it is local-only data (docs/AGENT_PROTOCOL.md §6):
it is never published or sent to a cloud session.

## Serving the page

`watch --serve ADDRESS:PORT` serves the page over HTTP, for as long as
the monitor runs, so the run can be followed from another device. It is
off by default, needs the page (it cannot be combined with `--no-page`),
and uses the standard library alone. What it serves:

- `/` and `/index.html`: the page, read from the run directory's `status`
  directory without following a symbolic link, neither the directory's
  nor the page's;
- nothing else: every other path is not found, no directory is listed,
  and the run directory itself is never read.

**Only the `status` directory may ever be served, never the run
directory.** The obvious `python -m http.server` in the run directory
would serve every document's views, transcriptions and claims included.

Serving on `127.0.0.1` keeps the page on the machine; an address on the
LAN shows it to every device there, which is the operator's choice to
make (CLAUDE.md, rule 7).

Every response, the page's and an error's alike (a 404 for another path
or a missing page, a 501 for another method), carries four headers:
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and a content security policy that allows
nothing but the page's inline style (`default-src 'none'; style-src
'unsafe-inline'`). The one exception is the standard library's own: a
request it reads as HTTP/0.9 (no version, a version it cannot read or
does not serve, a request line it cannot parse) is answered with a body
alone, without a status line or any header. No browser sends such a
request.

## Limits

- An invocation killed without a trace (no end in `run.json`, no closing
  event, no later invocation) looks as if it were still running, and so
  do the stages it left started, here and in `rundir.run_state`; the
  header's last-event age shows how quiet it has gone.
- The estimate assumes the documents still to go of a kind behave like
  the ended ones, and a corpus stage's time to go that what it has left
  goes at the rate of what it did in this attempt so far.
- Reuse is inferred from the events of the stage's latest attempt; a
  stage with a hit and a miss is `done`, whatever the share.
