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
and writes them.

## Using it

Until the scratch driver (#194) adds `yizkr scratch watch [<run>]`, which
calls `monitor.watch`, run it as a module:

```
uv run python -m yizkr.pipeline.monitor watch <run dir> [--interval 2] [--once] [--no-page]
uv run python -m yizkr.pipeline.monitor pause|resume|abort <run dir> [--stage NAME | --kind NAME] [--reason TEXT]
```

`watch` redraws the view in place every `--interval` seconds, until you
press `q` or Ctrl-C; quitting the monitor never stops the run. `--once`
prints one view and exits. `--no-page` leaves the status page unwritten.
Off a terminal (output piped or redirected) the view is printed afresh
each time the run changes, without colour, and no keys are read.

**Commands.** Press `:` or Enter in the view to type one:

```
pause  [run | stage NAME | kind NAME] [reason ...]
resume [run | stage NAME | kind NAME] [reason ...]
abort  [run | stage NAME | kind NAME] [reason ...]
help
quit
```

A slice may also be written `stage:NAME` or `kind:NAME`; with no slice
the command is for the whole run. Each command appends one request to
the run's `control.jsonl` (`rundir.RunDir.request`, `by: "monitor"`), and
the view says what the slice's state is then. The driver honours the
requests: a pause holds dispatch for its slice until a resume; an abort
holds it for the rest of the invocation, and invoking the run again
resumes it (docs/LLM_CACHE.md, `control.jsonl`). Neither loses a
completed output: in-flight calls finish, and every completed call is in
the cache.

- In the view, an abort asks for confirmation (`y`); the module's
  `abort` command line does not.
- A resume of an aborted slice is not written, since it would change
  nothing; the view says to invoke the run again.
- A stage or kind the run does not know (a typo, or a corpus stage no
  document plans) is written, and the view notes that no planned
  document has that slice.
- An empty stage or kind name is refused rather than taken for the whole
  run, and so is a directory without `run.json`.

## What it shows

**The header.** The run id, mode and project stage; whether the latest
invocation is running or how it ended (`finished`, `stopped` with its
reason, `interrupted`, `paused`, `refused`, `failed`); the invocations so
far; the time the run has been running, summed over its invocations; how
long ago the last event came; when the run started; and the planned
documents by kind.

**Stages.** One row per stage, in the order the plan gives them, then
any other stage the events name. The columns partition the stage's
documents:

| Column | Counts |
|---|---|
| `pending` | not dispatched yet, including those the control file holds (`held`) and those an ended invocation left started (interrupted: the next invocation starts them again) |
| `running` | started by the running invocation, not yet ended |
| `done` | made in this run: the engine answered at least one of the stage's calls, or the stage makes none (a text file's transcription, the GEDCOM adapter) |
| `reused` | the view was current and stands (`view current`), or the LLM cache answered every call the stage made (by the answers' reasons: `key match`, `adopted`, or several joined by `+`) |
| `attention` | done or reused, with attention reasons (a fallback tier, no claims); these count here, not under done or reused |
| `failed` | the latest attempt failed |
| `engine`, `cached` | the stage's calls the engine answered (a cache miss, or a replay of a conversation's opening turn) and the cache answered |

The counts agree with `rundir.run_state`: `done` and `reused` together
are its done, and `running` with the interrupted ones is its running.
Under the table, the notes break the counts down: reuse by reason,
attention and failure labels, held and interrupted stages.

The cache's events name the document and the role, not the stage: each
is counted for the stage that document has started and not yet ended,
since a document's stages run one at a time. Events outside a stage
count in the run's totals only.

**Calls.** The engine calls and the cache's answers, with the share the
cache answered and its reasons, resumes, replays, rejections and
adoptions, and the same per role. **To go** estimates the engine calls
the remaining stages will take, at the run's rates so far: each stage of
each kind at the average of its ended attempts (done, reused or failed),
since kinds differ; stages of kinds with no ended attempt yet are listed
apart. The canary ordering (#194) gives every kind a rate early.

**In flight.** Each document the running invocation is in the middle of:
its SHA-1 prefix, kind and stage, and for how long.

**Slices.** Each slice the operator can stop: the run, each stage and
each kind, and any other scope a current request names. A stopped slice
shows since when it is paused or aborted, and who stopped it and why (the
latest request). Requests before the latest invocation's start marker no
longer apply.

**Alerts.** The counts, new and carried, and by check; and the latest
ten, newest first: time, `new` (on an output the engine answered in this
run) or `carried` (on one made from cached answers), stage, check,
document and kind, and the measured value.

## What it reads

The monitor reads the run directory's files incrementally: each refresh
parses only the lines appended since the last one, so it keeps up with
a long run. A line without its newline yet (a writer mid-line) waits for
the next refresh. A complete line that is not a JSON object (a writer
killed mid-line, then another appending after it) is skipped and
counted. A log that shrinks or is replaced, or a `run.json` with another
run id, starts the monitor over. `run.json` is read after the logs, since
an invocation records itself there before its first event.

From `events.jsonl` it reads the stage events (`started`, `done`,
`reused`, `failed`, `held`), the LLM cache's events, `run-start`,
`run-end` and `run-interrupted`, `set-aside`, and call events when the
run has them. `alert` events are ignored: `alerts.jsonl` holds the
alerts.

**Call events** are optional. A run without them is shown from the
cache's events alone; a run without the cache (`--no-cache`) and without
call events shows no calls and no estimate. When the engine writes them
(#101), the monitor reads this shape, every field but `event` optional:

| Field | What it holds |
|---|---|
| `event` | `call` |
| `role`, `subject`, `kind`, `stage` | as the other events have them; a call with a `subject` counts for the stage that document is in |
| `duration_s` | how long the call took, in seconds |
| `outcome` | `ok`, or what else happened (`rate-limited`, `error`, ...); the view counts every outcome but `ok` |
| `wait_s` | seconds spent waiting on a rate limit before or during the call |

The view shows how many there were, their mean duration, the outcomes
other than `ok`, and the rate-limit waits. Engine calls are counted from
the cache's events when the run has them, and from call events
otherwise.

## The status page

`watch` writes `status.html` into the run directory: one self-contained
HTML file, with inline styles and no script, font, image or other
external asset, that reloads itself every five seconds. It is written
whole (an atomic replace), at most every five seconds while events
arrive and at least every thirty, and only into a directory that holds
`run.json`. A page that cannot be written (a read-only directory) is
reported in the view, and the monitor goes on.

**Privacy.** The page and the terminal view name a document by its SHA-1
prefix (ten hex digits) and kind only, never by its file name, title or
content. A failure shows only the exception's name it starts with, and
an attention reason only its label before the first colon, since the
details can quote a document; an alert shows its check and its measured
value, cut short. The page still lives in the run directory under
`var/`, which also holds the documents' views, so it is local-only data
(docs/AGENT_PROTOCOL.md §6): it is never published or sent to a cloud
session. The monitor does not serve it; to follow a run from another
device, serve that one file, never the run directory.

## Limits

- An invocation killed without a trace (no end in `run.json`, no later
  invocation) looks as if it were still running; the header's last-event
  age shows how quiet it has gone.
- The estimate assumes the remaining documents of a kind behave like the
  ended ones.
- Reuse is inferred from the events of the stage's latest attempt; a
  stage with a hit and a miss is `done`, whatever the share.
