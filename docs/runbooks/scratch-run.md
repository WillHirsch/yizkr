# Runbook: a whole-corpus scratch run

A scratch run takes every non-excluded source of the corpus through every
stage, unattended, from fresh state directories (ADR-0025, ADR-0032).
`yizkr scratch run` is the one command that runs it; this page is how to
run one, follow it, pause, abort and resume it, and read what it did.
The parts have their own references: the live monitor in
[docs/MONITOR.md](../MONITOR.md), the views, the LLM cache and the run
directory in [docs/LLM_CACHE.md](../LLM_CACHE.md), and `run.json` in
[docs/RUN_MANIFEST.md](../RUN_MANIFEST.md). Code:
`yizkr.pipeline.scratch`.

## Before a run

- **One at a time.** A scratch run holds the claude-code subscription for
  its model calls, and only one LLM-heavy task runs at a time
  (docs/AGENT_PROTOCOL.md §3). The driver holds its runs directory while
  it runs, and a second `yizkr scratch run` is refused at once, naming
  the holder. The monitor and the control commands never take that hold.
- **Configuration**, in the environment or `deploy/.env`:
  - `$YIZKR_CORPUS_DIR`, the corpus folder (or `--corpus`);
  - `$YIZKR_EXCLUDE_FILE`, the never-ingest list. There is no run without
    one; an empty file is an explicit, allowed list;
  - `$YIZKR_LLM_MODEL`, a full model id (`claude-opus-5-5`, never an alias
    such as `opus`): the model of the transcription and the critique.
    Extraction runs `--extract-model`, `claude-sonnet-5` by default
    (`inherit` for `$YIZKR_LLM_MODEL`). Every role must name a model: the
    LLM cache's keys cannot name the engine's own default;
  - `$YIZKR_CLAUDE_RESERVE` and `$YIZKR_CLAUDE_MAX_WAIT`, the engine's
    pacing (docs/LLM_CACHE.md, Subscription limits).
- **Keep the claude CLI still.** A run keeps the claude CLI version it
  started on. Set `DISABLE_AUTOUPDATER=1` in every shell and Claude Code
  session on the machine for the length of the run.
- **Preflight.** Before anything is written or called, the driver checks
  the claude CLI and its login, a full model id for every role, the
  pacing settings, 2 GiB free on the run directory's and the cache's
  filesystems (`--min-free`), and the PDF renderer when the corpus holds
  a PDF. A stage that touches the wiki is checked for too: the stack must
  answer and the instance must carry no kept-state marker. No such stage
  is registered yet (below), so a run needs no stack. A refusal lists
  every problem it found.

## Run

```sh
uv run yizkr scratch run
```

- **A new run, or the unfinished one.** The command starts a new run in
  `var/scratch/<run id>/` (`--runs` for another runs directory), with
  fresh state directories, unless the latest run there did not go to the
  end: then it invokes that run again, so running the same command again
  resumes it. `--new` always starts a new run; `--resume [RUN]` invokes
  a given run again (the latest when none is named), even one that went
  to the end.
- **The order.** One document of each kind (text file, image or PDF,
  deck, GEDCOM file), the first by name, goes through all its stages
  first: the canaries. A misalignment of any kind shows within the first
  calls, and the monitor has a rate for each kind early. The rest follow
  stage by stage: every transcription, then every extraction and GEDCOM
  adapter, then every critique.
- **The barrier.** When every document's stages have ended, done or
  failed, the run is at the barrier, and the driver runs each corpus
  stage registered with it, once, over the documents whose stages all
  completed; a corpus stage keeps its outputs in `corpus/<stage>/` in the
  run directory. None is registered yet: a run finishes at the barrier.
- **Options.** `--until STAGE` stops after a stage (`transcription`,
  `extraction`, `critique`, or a corpus stage; `gedcom` stops with
  `extraction`). `--refresh ROLE` makes a role's views again in this
  invocation, asking the engine afresh for each call even where the cache
  holds an answer. `--cache DIR` names the LLM cache (default
  `var/llm-cache`), which every model call goes through. The rest are as
  for `ingest run`.
- **The end.** The driver prints how the invocation ended, the states of
  the stages, and the commands that show the run and resume it. It exits
  0 when it finished and no document failed; 1 when a document or a
  corpus stage failed, or it refused to start; 2 when it stopped short
  (an abort, or a pause of the engine it could not wait out); 130 on
  Ctrl-C.

## Watch

```sh
uv run yizkr scratch watch            # the latest run
uv run yizkr scratch watch <run>      # a run id, or a run directory
```

The live monitor ([docs/MONITOR.md](../MONITOR.md)): per stage, what is
pending, running, done, reused (and why), needs attention or failed; the
engine's calls and the cache's answers; what is in flight; the slices
and who stopped them; the latest alerts. It never quotes a document.
`--once` prints one view; `--serve ADDRESS:PORT` serves the status page,
and nothing else, to another device. Quitting the monitor never stops
the run.

## Pause, resume and abort

```sh
uv run yizkr scratch pause  [<run>] [--stage NAME | --kind NAME] [--reason TEXT]
uv run yizkr scratch resume [<run>] [--stage NAME | --kind NAME]
uv run yizkr scratch abort  [<run>] [--stage NAME | --kind NAME] [--reason TEXT]
```

The monitor's command line (`:` or Enter) asks the same. A request names
the whole run, a stage (`transcription`, `extraction`, `critique`,
`gedcom`, or a corpus stage) or a kind (`text`, `media`, `presentation`,
`gedcom`), and goes into the run's control file, which the driver reads
before each stage of each document and before each corpus stage.

- **Pause** holds its slice until a resume. The stage in flight finishes;
  the rest of the run goes on, and the driver waits only when nothing
  else can go on.
- **Abort** holds its slice for the rest of the invocation. Once nothing
  else can go on, the invocation ends `stopped`. A run whose documents'
  stages are held does not go past the barrier. `resume` does not lift an
  abort: invoke the run again.
- **Ctrl-C** stops the invocation at once, with the engine's calls in
  flight; it ends `interrupted`.
- **Anomaly checks** (#196) pause or abort a slice on their own, through
  the same control file, and the driver honours them as it does yours.

Nothing completed is lost: every call answered is in the cache, and
every view made is in its document's directory.

**Subscription limits.** A call the subscription refuses never fails a
document: the engine waits, or pauses the pass with the time it can go
on. Then the invocation ends `paused`, and the driver waits until a
minute past that time, reading the control file as it waits: an abort
of the run ends the wait, and a pause of the run keeps it waiting until
a resume. It then invokes the run again by itself, with the slices that
were stopped still stopped. A pause with no such time ends the driver,
which prints the engine's reason and what to do: a call reported to have
run on usage credits (turn extra usage off on the account, then run the
command again), a claude CLI that changed during the run, or an LLM
cache another process holds.

## Resume

Run the same command again: `yizkr scratch run` invokes the latest run
again when it did not go to the end (`--resume RUN` for another). Every
view that stands is reused, with a `reused` event, so nothing completed
is made again, and a stage cut short is made again with its answered
calls from the cache.

- **A run keeps its settings.** An invocation with other settings (an
  effort, a model, the corpus folder, the never-ingest list, the code) is
  refused before anything is written, naming each difference. A claude
  CLI updated between invocations is refused the same way: install the
  recorded version, with auto-update off, or start a new run.
- **A fix to a stage before the barrier** means a new run
  (`yizkr scratch run --new`): its fresh state directories make every
  view again with the fix, and the LLM cache answers every call the fix
  leaves the same, so only the calls it changes reach the engine.
- **A corpus stage registered after the run reached the barrier**: once
  every document's stages have ended, an invocation on other code goes
  past the barrier alone, dispatching no document's stage:
  `yizkr scratch run --resume <run>`. Its code is recorded beside the
  run's (docs/RUN_MANIFEST.md).

## Reading what a run did

- `yizkr stages state <run dir>`: per stage, the documents per state, and
  what the LLM cache did for each role.
- `yizkr stages manifest <run dir>`: what shaped the run, one setting per
  line.
- `yizkr scratch watch <run> --once`: the monitor's view, at any time,
  finished runs included.
- The run report (`yizkr scratch report`, #124) and the run-to-run diff
  (`yizkr scratch diff`, #198) come with their issues.

In the monitor's counts, **done** is a view made in this run, **reused**
a view that stood or one whose every call the cache answered (by the
answers' reason: `key match`, or an adoption's), **needs attention** a
view made or reused with reasons to look at it (a fallback tier, no
claims), and **failed** a document's stage that failed, with the reason
in the run's events and the document's `state.json`. `yizkr stages
state` counts done and reused views together, as done.

## What stays local

Everything a run writes is under `var/`, and is local-only data
(docs/AGENT_PROTOCOL.md §6): the run directory (every document's views,
`run.json`, `events.jsonl`, `alerts.jsonl`, `control.jsonl` and the
status page) and the LLM cache (model answers and session transcripts).
Nothing from them goes into the repository, GitHub or a cloud session.

- Only a run's aggregate report and diff summary may leave the machine.
  In an issue or a PR, cite a document by its SHA-1 prefix and a generic
  descriptor, and give metrics as aggregates.
- Serve the status page with `watch --serve` alone: never serve the run
  directory, which holds every document's views.

## Not yet

- No corpus stage is registered: clustering (#105), the reset,
  register, write and site stages (#200, with #199 and #103), and
  place-hierarchy reconciliation (#201) register themselves as they land.
  Until then a run finishes at the barrier, and needs no stack.
- Until #102's second half and #127 land, the GEDCOM adapter and the
  extraction prompts cite the artifact by a QID, and a scratch run
  registers none: its GEDCOM files fail, and its extraction prompts name
  no artifact. Scratch run 1 (#145) waits for them.
- `yizkr scratch estimate`, the calls a run is expected to make (#101),
  comes next.
