# Runbook: a whole-corpus scratch run

A scratch run takes every non-excluded source of the corpus through every
stage, unattended, from fresh state directories (ADR-0025, ADR-0032).
`yizkr scratch run` is the one command that runs it; this page is how to
count the calls one is to make, run it, follow it, pause, abort and
resume it, and read what it did. The parts have their own references:
the live monitor in [docs/MONITOR.md](../MONITOR.md), the views, the LLM
cache and the run directory in [docs/LLM_CACHE.md](../LLM_CACHE.md), and
`run.json` in [docs/RUN_MANIFEST.md](../RUN_MANIFEST.md). Code:
`yizkr.pipeline.scratch`, and `yizkr.pipeline.estimate` for the estimate.

## Before a run

- **One at a time.** A scratch run holds the claude-code subscription for
  its model calls, and only one LLM-heavy task runs at a time
  (docs/AGENT_PROTOCOL.md §3). The driver holds its runs directory while
  it runs, and a second `yizkr scratch run` is refused at once, naming
  the holder. The monitor and the control commands never take that hold.
- **Configuration in the environment or `deploy/.env`** (or the file
  `--env-file` names):
  - `$YIZKR_CORPUS_DIR`, the corpus folder (or `--corpus`);
  - `$YIZKR_EXCLUDE_FILE`, the never-ingest list. There is no run without
    one; an empty file is an explicit, allowed list.
- **Configuration in the environment only.** These are read from the
  process's environment and never from `deploy/.env`: a model set only
  there makes the preflight refuse, and a pacing setting there is
  ignored.
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
- **Archived transcriptions** (optional). `uv run yizkr scratch
  import-transcriptions <archive> [--leave-out SHA1]...` adopts the
  vision transcriptions of an earlier `ingest run`'s state directory in
  the LLM cache, each checked by the transcription check, so the run's
  calls for those scans are answered from the cache, as hits whose
  reason is `adopted`, rather than by the engine. Run it just before the
  run, with the run's `--cache`, `--extract-effort`, `$YIZKR_LLM_MODEL`
  and claude CLI, which its keys name: after a CLI update, run it again.
  A transcription the check fails is rejected and the run reads its scan
  afresh; `--leave-out` gives the start of the SHA-1 of each scan to read
  afresh anyway, and withdraws, for good, any adoption the cache serves
  for it. It prints what it did with each scan's transcriptions and the
  counts (imported, rejected, excluded, left out, skipped): local output,
  which names the corpus's files and quotes what the check matched. What
  it adopts, what it reads of the archive, and when an adoption stops
  applying: docs/LLM_CACHE.md, Seeding.
- **Preflight.** Before anything is written or called, the driver checks
  the claude CLI and its login, a full model id for every role, the
  pacing settings, 2 GiB free on the run directory's and the cache's
  filesystems (`--min-free`), and the PDF renderer when the corpus holds
  a PDF. A stage that touches the wiki is checked for too: the stack must
  answer and the instance must carry no kept-state marker. No such stage
  is registered yet (below), so a run needs no stack. A refusal lists
  every problem it found.
- **The kept-state marker.** Nothing reads the marker yet: keeper mode
  (#118) writes it, and its reader comes with the write side (#199,
  #200). Until then the driver cannot tell a kept instance from another,
  so it refuses every corpus stage that touches the wiki.
- **The estimate.** `uv run yizkr scratch estimate`, with the options
  the run is to take, counts the engine calls that `yizkr scratch run`
  would make now, per stage and in all: the calls of the stages whose
  views do not stand that the LLM cache cannot answer. It plans the same
  run, a new one or the one the command would invoke again, and says
  which. The table gives, per stage, the documents whose views are made
  and reused, the calls the cache answers, and the engine calls, of
  which some are estimated:
  - **known**: the estimate takes each stale stage through the
    pipeline's own code with a client that keys every call as the run
    does (the same prompts, files, models, efforts and claude CLI) and
    answers it from the cache alone. So it knows each call the cache
    answers and each it cannot, a kept answer the run would reject and
    ask again (a reading the transcription check fails, a critique cut
    off) included, and a call another document asks earlier in the run
    counts once. The replay of a conversation whose session was not
    kept, for its forks, is a known call too;
  - **estimated**: a call whose key waits on an answer the engine is yet
    to give: the extraction after a reading the engine makes, the chunks
    after an entity manifest it answers, the forks after a replay, and
    the critique of a batch with chunks it answers. An extraction is
    counted as the latest run to make the same document measured it,
    else by the mean for its kind over the runs measured, else by its
    text's length; the forks after a replay as the chunks the cache kept
    leave them unanswered; a critique as one call;
  - **up to more**: an entity manifest the engine answers, a replay's
    among them, may be refused, and then its second attempt and the
    segmentation's two are asked (3 more for an extraction at most); a
    replayed conversation that answers differently starts its extraction
    over and asks its forks again, the ones the cache answered too, and
    their number can change. These are estimates, not ceilings: the
    chunks an entity manifest makes follow its entities, which a text's
    length only approaches, so a dense document can take more;
  - **not counted**: calls asked again after an engine error or a chunk
    answer cut off (up to 2 for a chunk), and waits for the
    subscription, which make no call. A slice the anomaly checks pause
    as the run unfolds (docs/ANOMALIES.md) makes fewer calls than
    counted.

  It writes nothing, and takes no lock: it runs beside a running driver,
  counting what the run and the cache hold as it reads them, and names
  who holds the runs directory and the cache. It makes no model call and
  asks the wiki nothing. It refuses what the run would refuse before its
  first call: no never-ingest list, a runs directory the driver refuses
  (open to others, tracked by git, a file, one it cannot write), a model
  that is no full id, a pacing setting the engine refuses, a PDF without
  the renderer, other settings or another claude CLI for a run invoked
  again, and, once a stale stage needs the engine, a cache folder the
  cache refuses (What stays local, below). It lists what the run's
  preflight would still refuse (the login, disk space); a stage that
  touches the wiki is left to the run's own preflight. A corpus with no
  document is no call, whatever the settings, as it is to the run. Like
  the driver, it starts a role's engine client when the role's first
  stale stage needs it, which for the claude CLI runs `claude --version`,
  for the version every key names: a client that cannot start, or pauses
  as it starts, stops the estimate as it would stop the run, and nothing
  stale starts none. How the LLM cache keys a call, and what a run's
  events count: docs/LLM_CACHE.md.

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
  to the end. A run is invoked only from its own runs directory, which
  one driver at a time holds: for a run elsewhere, name its directory,
  `--runs <its runs directory> --resume <run id>`, as the driver's own
  resume line does.
- **Failed documents.** A run that went to the end with failed documents
  is complete: the bare command then starts a new run, and only `--resume`
  tries the failed documents again in the same run.
- **The order.** One document of each kind (text file, image or PDF,
  deck, GEDCOM file), the first by name, goes through all its stages
  first: the canaries. A misalignment of any kind shows within the first
  calls, and the monitor has a rate for each kind early. The rest follow
  stage by stage: every transcription, then every extraction and GEDCOM
  adapter, then every critique.
- **The barrier.** When every document's stages have ended, done or
  failed, the run is at the barrier, and the driver runs each corpus
  stage registered with it over the documents whose stages all
  completed. It runs every one in each invocation that goes past the
  barrier, so a corpus stage keeps its outputs in `corpus/<stage>/` in
  the run directory and reuses them while their inputs stand, as the
  documents' views are reused. The one registered is clustering (#105):
  it writes the corpus's identity map to
  `corpus/clustering/identity-map.json`, from the documents' batches
  alone, with no wiki and no model (src/yizkr/pipeline/README.md,
  "Corpus clustering"). Its `done` event carries its counts: the
  ambiguous and conflicting clusters it left apart, the adjudication
  calls they would take, and any hub key it blocked no pair on.
- **Options.** `--until STAGE` stops after a stage (`transcription`,
  `extraction`, `critique`, or a corpus stage; `gedcom` stops with
  `extraction`). `--cache DIR` names the LLM cache (default
  `var/llm-cache`), which every model call goes through. The rest are as
  for `ingest run`.
- **`--refresh ROLE`** makes the role's views again, asking the engine
  afresh for each call even where the cache holds an answer, once in the
  run: a later invocation that refreshes the role again, the driver's
  own after a subscription limit or yours with the same `--refresh`,
  reuses the views the refresh has made and saved already, and makes the
  rest. One without `--refresh` makes the views not made yet from the
  cache's answers, unrefreshed. So resume a refresh with its `--refresh`.
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
uv run yizkr scratch resume [<run>] [--stage NAME | --kind NAME] [--reason TEXT]
uv run yizkr scratch abort  [<run>] [--stage NAME | --kind NAME] [--reason TEXT]
```

The monitor's command line (`:` or Enter) asks the same. A request names
the whole run, a stage (`transcription`, `extraction`, `critique`,
`gedcom`, or a corpus stage) or a kind (`text`, `media`, `presentation`,
`gedcom`), and goes into the run's control file, which the driver reads
before each stage of each document and before each corpus stage. With no
run named, the command takes the latest, and says which run it wrote to.

- **Pause** holds its slice until a resume. The stage in flight finishes;
  the rest of the run goes on, and the driver waits only when nothing
  else can go on.
- **Abort** holds its slice for the rest of the invocation. Once nothing
  else can go on, the invocation ends `stopped`. A run whose documents'
  stages are held does not go past the barrier. `resume` does not lift an
  abort: invoke the run again.
- **An abort outranks a pause.** A stage an abort covers stays stopped
  whatever else is resumed, so a pause of its stage, its kind or the run
  that also covers it makes the driver wait for nothing: when an abort
  holds every stage left, the invocation ends `stopped` at once.
- **Ctrl-C** stops the invocation at once, with the engine's calls in
  flight; it ends `interrupted`.
- **Anomaly checks** (#196) pause or abort a slice on their own, through
  the same control file, and the driver honours them as it does yours.

Nothing completed is lost: every call answered is in the cache, and
every view made is in its document's directory.

**Subscription limits.** A call the subscription refuses never fails a
document: the engine waits, or pauses the pass with the time it can go
on. Then the invocation ends `paused`, and the driver waits until a
minute past that time, and never less than a minute when that time has
passed already (the engine can hold its pause past the time it
reported). It reads the control file as it waits: an abort of the run
ends the wait, and a pause of the run keeps it waiting until a resume.
It then invokes the run again by itself, and every request in force as
the new invocation starts stays in force, the whole run's included, one
made while the driver got ready again too. A wait can last a window's
length, days for a weekly window, and all the while the process holds
the runs directory and the LLM cache, so no other scratch run or
cache user can start; Ctrl-C or `yizkr scratch abort` ends the wait. A
pause with no such time ends the driver, which prints the engine's
reason and what to do: a call reported to have run on usage credits
(turn extra usage off on the account, then run the command again), a
claude CLI that changed during the run, an LLM cache another process
holds, or a cache folder the LLM cache refuses (What stays local,
below).

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
  every document has ended its stages, done or failed (a failed stage
  leaves the ones after it undispatched), an invocation on other code
  goes past the barrier alone, dispatching no document's stage:
  `yizkr scratch run --resume <run>`. Its code is recorded beside the
  run's (docs/RUN_MANIFEST.md).
- **A refresh** is resumed with its `--refresh` (Run, above).

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
- **The runs directory is private, and never tracked.** The driver makes
  the runs directory mode 700 when it makes it, and refuses one that
  exists and is open to other users, leaving its mode alone. It refuses
  a runs directory inside a git work tree unless git ignores it, as this
  repository ignores `var/`: `--runs .`, or any other path git would
  track, is refused before anything is written.
- **So is the LLM cache's folder.** The cache makes its folders mode
  700. As it opens, before any call, it refuses a `--cache` folder a git
  work tree would track, one there already that is open to other users,
  whose mode it leaves alone, and one this user cannot write in
  (docs/LLM_CACHE.md, The cache folder).

## Not yet

- Of the corpus stages only clustering (#105) is registered, and it
  touches no wiki. The stages that do, the reset, register, write and
  site stages (#200, with #199 and #103) and place-hierarchy
  reconciliation (#201), register themselves as they land. Until then a
  run finishes after clustering, and needs no stack. The estimate counts
  the per-document stages' calls, says that clustering makes none, and
  names any corpus stage it does not count.
