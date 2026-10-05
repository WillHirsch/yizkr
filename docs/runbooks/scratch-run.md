# Runbook: a whole-corpus scratch run

A scratch run takes every non-excluded source of the corpus through every
stage, unattended, from fresh state directories (ADR-0025, ADR-0032).
`yizkr scratch run` is the one command that runs it; this page is how to
count the calls one is to make, run it, follow it, pause, abort and
resume it, read what it did, and reset the instance for it. The parts
have their own references: the live monitor in
[docs/MONITOR.md](../MONITOR.md), the views, the LLM cache and the run
directory in [docs/LLM_CACHE.md](../LLM_CACHE.md), and `run.json` in
[docs/RUN_MANIFEST.md](../RUN_MANIFEST.md). Code:
`yizkr.pipeline.scratch`, `yizkr.pipeline.estimate` for the estimate,
and `yizkr.reset` for the reset.

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
  started on. The pinned version is 2.1.288 (`claude-opus-5-5` needs
  2.1.280 or newer); install exactly that one, with the updater off:

  ```sh
  export DISABLE_AUTOUPDATER=1
  npm install -g @anthropic-ai/claude-code@2.1.288
  claude --version   # 2.1.288 (Claude Code)
  ```

  Keep `DISABLE_AUTOUPDATER=1` set in every shell and Claude Code
  session on the machine for the length of the run. The `claude` on the
  run's `PATH` must be that install: the binary an editor bundles with
  its Claude Code extension is no pin, since it moves with the editor's
  own updates.
- **Archived transcriptions** (optional). `uv run yizkr scratch
  import-transcriptions <archive> [--leave-out SHA1]...` adopts the
  vision transcriptions of an earlier `ingest run`'s state directory in
  the LLM cache, each checked by the transcription check, so the run's
  calls for those scans are answered from the cache, as hits whose
  reason is `adopted`, rather than by the engine. Run it just before the
  run, with the run's `--cache`, `--extract-effort`, `$YIZKR_LLM_MODEL`
  and claude CLI, which its keys name: after a CLI update, run it again.
  A transcription the check fails is rejected and the run reads its scan
  afresh. The vision engine now answers the transcription record
  (ADR-0037), with a prompt no archived transcription was made with, so
  an archive an `ingest run` left before the record gives nothing to
  adopt, and the run transcribes the corpus afresh; `--leave-out` gives the start of the SHA-1 of each scan to read
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
- **The kept-state marker** is the page `Project:Kept state` on the
  instance and the file `kept.json` in the record of the instance:
  keeper mode (#118) writes both, and `yizkr scratch reset` refuses while
  either is there (Reset the instance, below). The driver takes the same
  reader (`yizkr.core.instance.instance_is_kept`) when the write side
  (#200) registers its stages; until then it cannot tell a kept instance
  from another, so it refuses every corpus stage that touches the wiki.
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
  calls they would take, and the count of hub keys.
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
- `yizkr scratch report <run>`: the run report (docs/RUN_REPORT.md).
- `yizkr scratch diff [<baseline>] [<run>]`: what changed between a run
  and a baseline, by default the latest completed run on main, and why,
  with the reviewer checklist a scratch-diff is read by
  (docs/RUN_DIFF.md). Its `diff.md` may be posted; its `diff.json` stays
  local.

In the monitor's counts, **done** is a view made in this run, **reused**
a view that stood or one whose every call the cache answered (by the
answers' reason: `key match`, or an adoption's), **needs attention** a
view made or reused with reasons to look at it (a fallback tier, no
claims), and **failed** a document's stage that failed, with the reason
in the run's events and the document's `state.json`. `yizkr stages
state` counts done and reused views together, as done.

## Reset the instance

`yizkr scratch reset` takes the one instance back to a fresh start and
repopulates it for a scratch run (ADR-0025 §1). In the development stage
it is the only sanctioned way to reset the instance. Code:
`yizkr.reset`, and `yizkr.core.instance` for the record of the instance.

```sh
uv run yizkr scratch reset          # asks you to type "reset" first
uv run yizkr scratch reset --yes    # confirmed; needed without a terminal
make scratch-reset                  # the same; ARGS="--yes" confirms it
```

Either way it first says which instance, compose project and volumes it
is about to reset.

- **The steps.** Each one prints how long it took, and the end lists
  them all:
  1. check the kept-state marker, and refuse on a kept instance
     (Guards, below);
  2. clear the write-phase state: the recorded instance-contents key is
     cleared, and the write stage's state set aside (The record of the
     instance, below);
  3. stop the stack: `docker compose down`, which keeps the volumes,
     after recording where the stack runs from (`stack.json`, below);
  4. remove the graph volumes: the database (`mysql-data`), the search
     index (`elasticsearch-data`), the query service (`wdqs-data`), and
     the wiki's uploaded media (`wikibase-image-data`), which only the
     database refers to. Kept, the media would leave each run's uploads
     beside the last run's files, and a thumbnail of a file of the same
     name could show the last run's bytes. QuickStatements' volume and
     traefik's certificates stay;
  5. set `LocalSettings.php` aside: the wiki's settings belong to the
     database just removed, and the Wikibase image installs MediaWiki
     afresh only when there are none, as on a first boot;
  6. start the stack, `docker compose up --detach --wait`, and wait for
     its API, up to `--start-timeout` seconds (default 900);
  7. bootstrap the manifest, 8. publish the queries, and 9. run the
     smoke test (`--smoke-timeout`, default 300 s), as `yizkr bootstrap`,
     `yizkr publish-queries` and `yizkr smoke-test` do.
- **No backup.** It takes none (ADR-0024 §7): everything written to the
  instance is lost. It never touches the LLM cache, a run directory or
  its reports, or the corpus.
- **Local settings.** Every reset installs a fresh `LocalSettings.php`,
  so keep the wiki's local settings in `deploy/config/Extensions.php` or
  `deploy/config/Uploads.php`, which the compose file mounts into
  `LocalSettings.d`, never in `LocalSettings.php`.
- **The stack.** One env file gives the reset its connection settings
  and the compose files their variables: `--env-file`, else
  `$YIZKR_ENV_FILE`, else `deploy/.env`, as for every command. The
  compose files are in its directory (`--deploy-dir` names another):
  `docker-compose.yml`, unless `--compose-file` names the ones the stack
  was started with. The compose project and the volume names are read
  from `docker compose config`, and no file in `deploy/` changes but
  `config/LocalSettings.php`, which is moved. From a worktree, point
  `$YIZKR_ENV_FILE` at the main checkout's `deploy/.env`: the stack runs
  from that checkout's deploy directory, and the reset refuses any
  other. While the stack has containers, stopped ones included, their
  labels say where it was started from; with none, the reset checks the
  deploy directory and compose files that the last reset recorded in
  `stack.json`.
- **Guards.** Before it changes anything, the reset refuses:
  - `COMPOSE_PROJECT_NAME`: docker compose would take the stack for
    another project than its compose files name, with other volumes,
    which the reset would remove;
  - `DOCKER_HOST` or `DOCKER_CONTEXT`, or a docker context other than
    `default`: the reset moves a file beside the compose files, so it
    works on the local Docker engine only. It reads these settings in the
    environment, and in the env file in each form docker compose reads
    there (`NAME=value`, `export NAME=value`, `NAME: value`), whatever
    the value;
  - compose files that do not declare the four volumes under the keys
    Wikibase Suite Deploy gives them, or declare one external;
  - a stack whose containers, stopped ones included, were made from
    another directory or other compose files, or, with no container,
    that the last reset ran from elsewhere, as another project or on
    another Docker engine (`stack.json`): starting it from here would
    swap its config directory, and its `LocalSettings.php`, for another
    checkout's, and after a restart another engine can answer behind the
    local socket, with none of the stack's volumes. A record from before
    engines were recorded names none, and only a record that names
    another engine is refused on that count;
  - a volume of one of those names that Docker Compose did not make for
    this project as that volume: it removes the stack's own volumes and
    nothing else;
  - a stack that docker cannot read;
  - a reset that is not confirmed: at a terminal, by typing `reset`;
    otherwise by `--yes`.

  It then takes the live-stack lock (docs/TESTING.md), waiting while a
  live test session holds it, and holds it to the end, so no live
  session uses the instance while it is reset. Under the lock it reads
  the stack again under these guards, since it may have changed while
  the reset waited, and refuses one that names another project, other
  volumes or another config directory than the one it confirmed. Then it
  reads the kept-state marker (below), and refuses a kept instance. It
  asks the wiki first. A wiki that answers must be served by this
  engine's stack, with the project's database volume and a running
  `wikibase` container: otherwise it is served from elsewhere (another
  Docker engine behind the local socket, say), and the reset cannot tell
  which instance it would reset. A wiki that answers as another wiki
  than the env file names is refused too. With the database volume
  there and no answer, the reset starts the stack to read the marker,
  and refuses if it still cannot. Only no answer and no database volume
  mean the instance holds no graph to guard.
- **The kept-state marker** is kept twice, and the reset refuses if
  either says the instance is kept: the page `Project:Kept state` on the
  instance, which goes wherever its database goes, into backups and
  through a restore; and the file `kept.json` in the record of the
  instance, which holds when the instance cannot be read, or another
  engine answers for it. Keeper mode (#118) writes both once the keeper
  run's write completes (ADR-0025 §6). Removing them is a deliberate
  owner step, taken only to give up the kept state: delete the page as
  the wiki's admin, and remove `kept.json` by hand.
- **What it deletes**: the four volumes, and nothing else. The rest of
  what it clears it sets aside, in `reset/<UTC time>/` in the record of
  the instance: the write stage's state, and `LocalSettings.php`, which
  holds the wiki's secret key and database password. The folder is
  private to you; clear old ones by hand when you no longer want them.
- **Again.** Every step is safe to repeat, so a reset that failed or
  was interrupted is finished by running it again: it removes only the
  volumes there are and sets aside only what is there, and the
  bootstrap, the queries and the smoke test are idempotent.
- **When the stack cannot start.** A reset cut short while the stack
  installed MediaWiki can leave a half-installed database, whose wiki
  never answers; the reset then refuses, since it cannot tell whether
  the instance is kept. If you are sure it is not kept, and there is no
  `kept.json` in the record of the instance, finish by hand in the
  deploy directory, with the compose files the stack was started with (a
  `-f` for each, as the reset's refusal names them), then run
  `yizkr scratch reset` again:

  ```sh
  docker compose -f docker-compose.yml down
  docker volume rm yizkr-wikibase_mysql-data \
      yizkr-wikibase_elasticsearch-data yizkr-wikibase_wdqs-data \
      yizkr-wikibase_wikibase-image-data
  mv config/LocalSettings.php \
      ~/.local/state/yizkr/instances/<host>/reset/LocalSettings.php.$(date -u +%Y%m%dT%H%M%SZ)
  ```

  The volume names are the project's (`yizkr-wikibase`, from the
  compose file's `name`) and each volume's key;
  `docker compose config --volumes` lists the keys.
- **Exit status**: 0 when it finished; 1 when it was refused or a step
  failed, saying which; 130 on Ctrl-C.

**The record of the instance.** What a scratch run needs to know about
the instance, outside it, is kept per user, apart from any checkout, so
that every checkout and worktree on the machine shares it as they share
the instance: in `$YIZKR_INSTANCE_STATE` (default
`~/.local/state/yizkr/instances`, under `$XDG_STATE_HOME` when that is
set), in a private folder named after the instance's host.

- `contents.json`, the instance-contents record: the key of what the
  scratch write stage wrote (#200), the run that wrote it, and the
  instance's newest revision and newest log entry as the write finished.
  It stands only while those stay the same, so any other write, by a
  live test, a command or a hand in the wiki, voids it, and the next
  scratch run resets first. The live-test harness (as each session
  starts and ends), `yizkr bootstrap` and the reset also clear it
  outright, and the cleared record names who cleared it, when, and from
  which process.
- `kept.json`: the host-side kept-state marker (above).
- `stack.json`: the deploy directory, compose files, project and Docker
  engine (its id, from `docker info`) that the last reset stopped the
  stack from. When it cannot be read, the reset refuses, and says to
  remove it by hand once you know where the stack runs from.
- `write/`: the write stage's state between its invocations (#200),
  which the reset sets aside.
- `reset/`: what each reset set aside.

It is local-only data (docs/AGENT_PROTOCOL.md §6).

## What stays local

Everything a run writes is under `var/`, and is local-only data
(docs/AGENT_PROTOCOL.md §6): the run directory (every document's views,
`run.json`, `events.jsonl`, `alerts.jsonl`, `control.jsonl` and the
status page) and the LLM cache (model answers and session transcripts).
Nothing from them goes into the repository, GitHub or a cloud session.

- Only a run's aggregate report and diff summary (`report.md`,
  `diff.md`) may leave the machine.
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
  site stages (#200, with #103; the reset stage runs the reset above)
  and place-hierarchy reconciliation (#201), register themselves as they
  land. Until then a run finishes after clustering, and needs no stack.
  The estimate counts the per-document stages' calls, says that
  clustering makes none, and names any corpus stage it does not count.
