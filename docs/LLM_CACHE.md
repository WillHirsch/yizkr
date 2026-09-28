# Views, the LLM cache and the run directory

Development runs the whole corpus through the pipeline again and again
(ADR-0025). Two things make that affordable and stable (ADR-0032):

- **A document's views persist.** Its transcription, batch and critique,
  and the files the later steps derive from them, stay in its state
  directory and are reused while the inputs they were made from are
  unchanged. A run makes a view again only when its inputs change, when
  `--refresh` names its role, or when it starts from a fresh state
  directory, as every scratch run does (#194).
- **The LLM cache saves the engine calls of a view made again.** Every
  model call's response is kept under a key built from what the call
  sends, so a call made again is answered from the cache, while what is
  done with the answer (parsing, the merge, validation, the transcription
  check) runs whenever the view is made. The cache is only an
  accelerator: without it the pipeline behaves the same, apart from
  making the calls. An answer the cache kept that a caller now rejects
  (below) is asked for once more in the same run, as the engine would
  have been asked without the cache.

The run directory beside them records what each run did, for the scratch
driver (docs/runbooks/scratch-run.md), the live monitor
([docs/MONITOR.md](MONITOR.md)), the anomaly checks (#196), the call
estimate (#101) and the run report (#124). How the claude-code engine
meets the subscription's limits, and paces its calls to leave headroom
for development sessions, is under Subscription limits, below.

All three are runtime state under `var/` and are **local-only data**
(docs/AGENT_PROTOCOL.md §6): they hold transcriptions, claims, session
transcripts and raw model output of the corpus. Nothing from them is
copied into the repository, GitHub or a cloud session.

Code: `yizkr.pipeline.orchestrate` (the views), `yizkr.pipeline.llmcache`
(the cache), `yizkr.pipeline.llm` (the engines and their limits, the
reports of their calls, the call estimate, and the client factory that
alone imports the cache: `open_cache`, `read_cache`, `cached_client`,
`cache_holder`) and `yizkr.pipeline.rundir` (the run directory and run
state).

## Views

`yizkr ingest run` brings each document's views before the review gates
up to date once per run, each after the view it is made from:

| View | Made by (stage, role) | Made from |
|---|---|---|
| `transcription.json` | `transcription`, `transcribe` | the correction `transcription.corrected.txt`, when there is one; otherwise the source, routed (vision, text file, slide text) |
| `batch.json` | `extraction`, `extract` (a GEDCOM file: `gedcom`) | `transcription.json`, by its canonical content (text, language, and the artifact's key, the SHA-1 of its media); a GEDCOM file: the source alone |
| `critique.json` | `critique`, `critique` | `batch.json`, by its canonical content (without its run stamps) |

The source is not recorded: the directory is kept for one source's bytes,
and a changed source sets it aside (below). Each view is recorded in
`derivations.json` with its inputs' content hashes, and a view whose
inputs still hash the same stands: the run reuses it, with a `reused`
event. A change to a prompt, a model or the code never makes a view stale
by itself, with one exception: a vision or text-file transcription is
checked again, with the transcription check as it is now, each time it is
reused. One the check now fails is moved aside as
`transcription.superseded-<n>.json`, never deleted, with a `set-aside`
event that gives the reason; its record is forgotten, and it is made
again in the same run: with the cache, the failing answer it kept is
rejected and the engine is asked (Rejection, below). When the batch made
from the new transcription differs, the reviews of the old one are moved
aside, as below. A curator's correction and slide text are never checked.
`--refresh ROLE` makes the role's views again, in place, current or not
(their calls then go to the engine whatever the cache holds); a view made
again with the same content invalidates nothing after it. The extractor's
side outputs (`extraction/coverage.json`, `rejects.json`, `gaps.ndjson`)
are written with the batch they describe.

The later files are recorded the same way:

| Derived file | Made from |
|---|---|
| `equivalences.csv` | `batch.json` |
| `batch.resolved.json` | `batch.json`, `equivalences.reviewed.csv` |
| `review.csv` | `batch.resolved.json`, `critique.json` |
| `write.json` | `review.reviewed.csv`, `batch.resolved.json` |

`equivalences.reviewed.csv` reviews `equivalences.csv`, and
`review.reviewed.csv` reviews `review.csv`.

- A batch whose canonical content changed invalidates every file derived
  from it and moves the reviewed CSVs aside; a batch made again with the
  same content invalidates nothing. So a deleted `batch.json` rebuilt
  with the same content, as the cache or the GEDCOM adapter gives it,
  invalidates nothing; rebuilt without the cache, the engine words it
  afresh, and the reviews are moved aside.
- A re-exported CSV whose content changed moves its reviewed file aside;
  an identical re-export keeps it.
- A reviewed CSV consents for the CSV it was first seen with: saving it
  again re-writes, and editing the CSV it reviews re-arms the gate.

To correct a transcription, save the corrected text as
`<doc>/transcription.corrected.txt`. The transcription is made again from
it, and exactly what depends on the text changes: a correction that says
what the transcription said changes nothing after it. Delete the file to
return to the original: the transcription is made again by its route, and
a vision transcription is still in the cache.

**A changed source, or a directory without its records.** Different
bytes are a different artifact (ADR-0010). When a document's source no
longer has the SHA-1 its `state.json` recorded, when its directory has
files but no `derivations.json` (a directory from before the records, or
one that lost its record, so nothing says what its files were derived
from), or when files sit beside the record with no `state.json` to name
the artifact they were made for, `ingest run` moves the directory aside
whole, as `.<name>.superseded-<n>` in the state directory (`<name>` is
the directory's), and ingests the document afresh in a new one:
registration, views and gates. No document directory's name starts with
a dot, so none can take that name. Nothing in the old directory is
deleted, the log and a `set-aside` event name it and the document, and
the bytes it recorded seed no content dedup. Every directory `ingest
run` makes has its `derivations.json` from its first run, written before
any other file, and its `state.json` from its registration, before any
view; no state is saved into a directory without its record.

**A directory per source file.** Each source file has a directory of its
own in the state directory, named after its stem (`letter`) when no
other source in the folder has a stem of the same name, ignoring case.
Sources that share a stem (`letter.pdf` and `letter.jpg`) are named after
their whole file names (`letter.pdf`, `letter.jpg`), and where that name
is taken too, after its first 200 characters with a hash of the file
name. A source keeps the directory whose `state.json` names its file,
and a document's state names its file as the file is named now, so a
source that joins or leaves the folder moves no other source's
directory: `letter.pdf` keeps `letter` when `letter.jpg` joins it,
`letter.jpg` keeps `letter.jpg` when `letter.pdf` leaves, and a source
renamed with its bytes unchanged keeps its directory. A source that
keeps no directory takes one whose `state.json` cannot be read among
the names it may have, so that damage is reported (below) whether or not
another source shares the stem. Where several such sources could take
it, the one whose file the damaged bytes still name takes it (a state
is saved with its `file` first, so a cut-short one almost always keeps
it), and failing that the first by name. The naming moves nothing aside: a
directory whose source has left stays until a source is given its name,
and when that source's bytes differ, it sets the directory aside as a
changed source's (above). No directory takes the name of a run file
(`run.json`, `events.jsonl`, `alerts.jsonl`, `control.jsonl`), of the
directory the live monitor writes its page into (`status`,
docs/MONITOR.md), or of the one a scratch run's corpus stages keep their
outputs in (`corpus`, docs/runbooks/scratch-run.md).

A state directory an earlier `ingest run` made may hold one directory
that two same-stem sources shared: it stays with the source its
`state.json` names, but its views may have been made from the other
source. Resume such a state directory with `--refresh` for each role
(`transcribe`, `extract`, `critique`), or start a fresh one, as every
scratch run does.

**A document that cannot be read** fails alone, and the run goes on with
the others. That is a document whose source cannot be read (its
permissions, say), whose bytes the never-ingest list's hashes cannot be
checked against, or whose `state.json` cannot be read (cut short,
foreign, or its permissions). It is not planned or dispatched: a
`document-failed` event names it, with the reason. Where it has a
directory with its record, and its `state.json` names its file, its
`state.json` records the failure and the reason; a state that names
another file, a departed source's, is left as it is. A `state.json` the
run cannot read is left as it is too, never saved over, since it may hold
the registration: repair it, or remove it and the next run starts the
document afresh. A source removed after discovery fails its document
when a later step reads it; a document whose views all stand may not
read it again in the run.

`state.json` is replaced in one step, as the views are (written beside
it, then renamed), so a kill never leaves it cut short; a state that has
not changed is not written. So a document whose views all stand is
advanced in a directory the run cannot write to. One whose state must
change there, and cannot be saved, fails alone, and the reason says so.

**One-off invalidation.** A state directory made before ADR-0032 has no
record for its views, and its files derived from the batch were recorded
by the batch view's bytes: its first run makes every view again (the old
stage store is not read, so the calls go to the engine), and everything
after the batch with them. The pre-ADR-0032 stage store under
`var/stages` may be deleted. A transcription view made before #127 names
its artifact by QID, and does not load: its first run makes it again
(the transcription prompt is unchanged, so the cache answers its call),
and the batch after it, whose prompts name the artifact by its key now,
so its calls go to the engine.

## Keys

A key is the SHA-256 of the canonical JSON of its components:

| Component | What it holds |
|---|---|
| `engine` | `claude-code` or `anthropic` |
| `model` | The model configuration names: the role's own model (`--extract-model` for extraction), else `$YIZKR_LLM_MODEL`; `null` when the engine picks its own default (an empty name included) |
| `effort` | The reasoning effort (claude-code only; `null` for the API) |
| `config` | The client's `config_hash`. claude-code: the pinned CLI version, the arguments (the appended system prompt, `--model` and `--effort` among them), the working directory, the environment rule and the pinned settings. anthropic: the model and the output budget |
| `call` | `complete` (one-shot), `open` (a conversation's opening turn) or `fork` |
| `system` | SHA-256 of the system prompt the engine adds (claude-code's `--append-system-prompt` text) |
| `prompt` | SHA-256 of the text the engine sends (claude-code: the prompt after the line that points the Read tool at each image), with every path of a file the call is given replaced by `sha256:<the file's content SHA-256>` |
| `files` | The content SHA-256 of each file the call may read: the files its caller passes (`image_paths`), in order |
| `parent` | For a fork, the keys of the conversation it forks |
| `opening` | For a fork, the SHA-256 of the conversation's opening answer, which the fork's context holds: a kept session is resumed only for the answer it was made on |
| `attempt` | Which attempt at the call this is, for a caller that asks again and then falls back (the entity manifest, the segmentation); 0 otherwise |
| `epoch` | The role's epoch: 0 unless `--epoch ROLE=N` says otherwise |

The role itself is not a component: two roles making the same call share
its entry. A claude CLI update changes `config`, and so every key.

**What a key does not see.** Anything added on the engine's side of the
call. The claude CLI fetches account data from the server at every start,
which can change its system-prompt text, its default model and its
compaction windows (see the engine's docs in `yizkr.pipeline.llm`), and
it fills details of its own into its system prompt when it runs, such as
the date. When no model is configured for a role, the engine's default
model is not keyed: a change to it is not seen, and the cache logs a
warning once per role. Every entry records the model the engine reported,
so a reuse across such a change can be found. Set `$YIZKR_LLM_MODEL` for
any run whose answers are to be reused. The engine's system prompt tells
the model to read only the files a prompt names, but its Read tool does
not enforce that.

**No QIDs.** No prompt names what an instance gives an artifact: the
extraction prompts (the entity manifest, the segmentation, each chunk and
the single call) name it by the SHA-1 of its media, which its
transcription records and its claims cite (#127), and the transcription
prompt names no artifact. So a reset, after which each artifact is
registered again under another QID, changes no prompt and no key: the
run after it is answered from the cache wherever nothing else changed.
The transcription view holds the same key, so its content hash, which
the batch is recorded as made from, does not change either.

## Layout

```
var/llm-cache/                     (--cache; private, below)
  .lock                            the lock of the process using the cache,
                                   and its record (mode 600)
  <first two hex digits of the key>/
    <key>.json                     the entry the call is served
    <key>.rejected-<stamp>.json    a response a caller rejected
    <key>.replaced-<stamp>.json    an entry a newer one displaced
    <key>.session/<opening>/       an opening turn's session files, under
                                   the SHA-256 of the answer they hold
```

An entry holds `format`, `key`, `components` (the key's components, whose
canonical JSON hashes to the key), `response`, `origin` (`engine`, or an
adoption's reason) and `provenance`: the `role`, the `call`, the
`engine`, the `model` the engine reported, the `cli_version`, the
`config_hash`, the client's `model_identifier` and the time (`at`). A
fork's entry adds how its session was obtained (`session`: `live`,
`resumed`, `replayed` or `recomputed`) and the `opening_sha256` it was
made on. An entry moved aside gains `rejected` (`at`, `reason`, `role`)
or `replaced` (`at`). An entry is served only under the key it records.

**Writes are atomic, and nothing is overwritten.** An entry is written to
a staging file beside it, flushed, and published by a link that fails
where the key has an entry already: a reader sees an entry whole or not
at all, and a staging file is never read as one. A newer entry for the
same key (a refresh's, a racing call's, one displacing a damaged entry)
moves the one there aside as replaced. **Only a response the engine
returned with some text in it is stored**: an error, a timeout, a pause
(`PassPaused`) or an empty answer stores nothing, so the next run asks
again. **The cache deletes nothing**: an entry it stops serving is moved
aside and kept.

## The cache folder

The entries hold corpus content: model answers, and the transcriptions
their prompts carry. They are local-only data, never committed or shared
(docs/AGENT_PROTOCOL.md §6), so opening a cache refuses two folders
(#305), however the command names them:

- **One a git work tree would track**: a folder inside a work tree that
  git does not ignore, where `git add` would pick the entries up. The
  refusal names the tree. This repository ignores `var/`, so the default
  `var/llm-cache` is fine, as is a folder outside any work tree. A folder
  not made yet is judged by its name, as git matches its ignore rules,
  and nothing is made in the tree. A symbolic link is followed: what
  counts is where the entries would land. Where no git can be run, none
  can track the folder, and nothing is refused on this count. The scratch
  driver refuses a runs directory by the same check.
- **One there already that is open to other users**: any permission for
  its group or for others, a home directory's 755 say, or a folder of
  your own that its group can write. Its mode is left as it is: make it
  private (`chmod 700`), or name another folder.

A folder that cannot be made, is not a folder, or is private but not
writable by this user (another user's, or one closed to its owner) is
refused too, and so is a lock file that cannot be opened. The checks run
as the cache opens, and every command opens its cache before its first
call: `ingest extract --cache` before it asks the engine, `ingest run`
and `scratch run` when a stage first needs the engine. The refusal's
message is one line, and it is raised as `CacheRefused`, a pause
(`PassPaused`) like `CacheInUse`: it ends the pass rather than failing
each document in turn, the invocation ends `paused` (`scratch run`
prints the line and exits 2), and the pass goes on once the folder can
hold the cache, or with another.

**Folders the cache makes are private** (mode 700, whatever the umask):
the root and any missing folder above it, a key's folder for an answer
or an adoption, and a kept session's folders, however deep. A folder
there already keeps its mode, one another process makes first included,
so the cache changes the mode of no folder it did not make; a private
root keeps the folders under it from other users, whatever their modes.

## One process per cache

One process at a time uses a cache (#236): a scratch run and `ingest
extract --cache` on the same cache at once, say, is not supported.
Opening a cache locks its `.lock` exclusively (flock) for the rest of the
process, and records the process there: its id, host, command and start.
`ingest run` opens its cache when a stage first needs the engine,
`ingest extract --cache` before its first call. A second process that
opens the cache while another holds it is refused at once with
`CacheInUse`, whose message names the holder. The refusal is a pause
(`PassPaused`): it ends the pass before its next call, as a claude CLI
update does, rather than failing each document in turn. Run the pass
again once the holder has ended, or give it another cache folder, or
none.

The kernel releases the lock when its holder ends, however it ends, so a
holder that crashed or was killed blocks nothing. A clean exit empties the
record, so a record the next process finds was left by a holder that did
not exit cleanly: the process says so in a warning, naming the holder,
and records itself in its place. A process opens its cache as often as
it likes and holds it once. Nothing deletes the lock file: one removed
while a process holds the cache no longer excludes anyone, and the
holder, when it next opens the cache, warns and locks the file there
again, or is refused if another process has taken it meanwhile.

**Reading a cache as it is.** `llm.read_cache` gives the entries a run
would be served, without opening the cache: it takes no lock, makes no
folder and writes nothing, and what calls made through it change (an
answer rejected, one stored) stays in memory (ADR-0032 §3). A kept
session comes back as the names of its files only, with no bytes, read
through its folder opened once, so one replaced meanwhile by renames is
never read torn, and looked for again a moment later while it is
missing. The call estimate reads the cache so, beside a run that holds
it (docs/runbooks/scratch-run.md, Before a run). `llm.cache_holder` names
the process that holds a cache, from its lock file's record, without
taking the lock.

Why one process: several session paths assume it. A resumed claude-code
session's transcript sits under its original id in the CLI's projects
folder, which every process shares, so one process's release could pull
it from under another's forks; and a session held in memory after a
replay (Conversations and forks, below) is known only to the process
that holds it.

## Rejection

A caller that cannot use a response rejects it, through the client
(`llm.reject_response`; an engine's own client has nothing to reject).
The entry is moved aside as `<key>.rejected-<stamp>.json`, with the
reason, and is never served again: the next call under the key, a retry
in the same run or a call in the next, goes to the engine, and its miss
says `rejected`. A rejection moves only the answer it names: the entry is
renamed to a private name first and only then compared, and an answer
another caller stored under the key meanwhile stays live.

**A kept answer rejected is asked for once more.** A rejection tells the
caller whether the answer was a hit, one the cache kept from an earlier
call, rather than the engine's answer to this call. When it was, the
engine has not answered the call in this run, so the caller asks once
more, once: the call misses as `rejected`, and the engine's answer takes
the rejected one's place, as it would without the cache. A chunk spends
no retry on it; the transcriber, the critic and the single-call extractor
use the new answer. So a check or a parser made stricter ends a run with
the cache as it ends one without it. Rejected are:

- a transcription that fails the transcription check. The check runs on
  every transcription a run makes, from the engine or from the cache, and
  on every vision or text-file transcription view a run reuses (above). A
  failing answer the cache kept is asked for once more; a failing answer
  of the engine's fails the document, and the next run transcribes it
  again. A stricter check applies to every transcription, the stored
  views included;
- a chunk response cut off before its terminal sentinel;
- a critique without its terminal sentinel (a refusal, one cut off, a
  usage-limit notice): its findings are kept, and the critic never
  raises;
- a single-call extraction (`ingest extract --strategy single`) with no
  claim JSON or no valid claim, which fails, or without its terminal
  sentinel, whose claims are kept.

So a parser fix recovers every response the old parser accepted, without
a call, but not one it rejected.

## Attempts

The entity manifest and the segmentation behind it are asked in two
attempts each, and each attempt is keyed apart (`llm.for_attempt`). A
refused answer is kept, not rejected: a later run is answered attempt by
attempt from the cache, takes the same fallback, and gives the same
batch, whatever the engine would answer now. `--refresh extract` or a new
epoch asks again.

**A stricter parser replays a kept answer, rather than rejecting it.** An
entity manifest or segmentation the cache kept, which the parser now
refuses because it was made stricter since, is not rejected: it spends
its attempt, as any refused answer does. A later attempt that no earlier
run asked goes to the engine: when the old parser accepted the first
attempt's answer, the second attempt is asked afresh. Only when every
attempt is refused does the document take the next tier. Why not reject
it and ask once more, as the rejectors above do:

- the caller cannot tell a refusal by a stricter parser from the refusal
  an answer met when it was stored; both are refused now. Rejecting
  every refused answer the cache kept would ask the engine again for
  every refused manifest in every run, and the attempt-by-attempt replay
  that makes a document's fallback repeat, and a scratch run's fallback
  counts comparable, would be lost;
- a refused manifest or segmentation falls back to the next tier, which
  is what the parser does with an unusable answer with or without the
  cache, rather than failing the document, as a transcription the check
  fails does.

The price: after a parser is made stricter, a run with the cache can fall
back where one without it would have had fresh answers for the attempts
the cache answered. `--refresh extract`, or a new epoch, asks every
attempt afresh, and the scratch diff of the change shows its fallbacks.

## Conversations and forks

Opening a conversation is a call like any other; the handle the cache
returns carries the key chain and the opening answer its forks are keyed
on. A fork the cache answers needs no session. A fork it cannot answer,
of a conversation the cache opened, needs the engine to hold the
conversation exactly as the fork's context assumes:

1. **Resumed exactly.** When a conversation is opened, the cache keeps
   what the engine needs to resume it, beside the opening turn's entry and
   under the SHA-256 of the opening answer it holds
   (`<key>.session/<opening>/`, published whole): for claude-code, the
   session's transcript files. A fork resumes only the session kept for
   the opening answer the cache serves, and replays when none is kept for
   it: a session made on another answer (by two callers that opened the
   conversation at once, or a replay whose session could not be kept) is
   never resumed for it. The files are put back into the CLI's projects
   folder for `--resume … --fork-session`, and released after the forks,
   as every transcript is. The API engine rebuilds the conversation from
   the opening turn's prompt and cached answer. No call is made; the event
   is `cache-resume`.
2. **Replayed**, only when nothing usable is kept. The opening turn is
   asked again and its answer compared with the cached one
   (`cache-replay`, with `matches`). When they match, the replay's session
   serves the forks and is kept for the next run. When they differ, the
   whole conversation is a miss: the replay's answer becomes the opening
   turn's entry (the old one is kept aside as replaced), its session is
   kept under the new answer, and the extractor starts over from the new
   answer, whose forks are keyed apart and are made on the replay's
   session. That session is also held in memory for the rest of the run,
   with the SHA-256 of the answer the replay gave, and the conversation
   opened again takes it only when the cache serves that answer. When a
   racing caller in the run (another document with the same opening
   prompt, say) has stored another answer meanwhile, the held session is
   released, and the fork resumes the session kept for the served answer,
   or replays again. So is a held session that a later replay's session
   displaces under the same key, since nothing will take it.

   A replay that fails (the engine raises, or answers with no text) fails
   the fork, and is remembered for the conversation: its later forks in
   the pass fail at once, without asking the engine again, so a failing
   engine is asked once per pass rather than once per chunk. Each chunk
   then drops to inline context. The conversation opened again, for a new
   pass, replays again.

A session, however obtained, serves every later fork of the conversation
in the run, and never a conversation opened on another answer than the
one it holds. A fork's `cache-miss` is emitted once the engine holds its
session, just before the fork is asked, after any `cache-resume` or
`cache-replay`: a fork that ends for want of a session (the conversation
changed, or its replay failed) is no miss.

## Switches

- `yizkr ingest run --cache DIR`: the cache (default `var/llm-cache`),
  which serves one process at a time (One process per cache, above), in
  a folder no git work tree would track and no other user can open (The
  cache folder, above).
- `--no-cache`: the engine directly; nothing is read or written. Views
  persist and are reused as with the cache, and a rejected answer the
  cache kept is asked for again in its run, so the run is otherwise the
  same. Its calls are `call` events, as with the cache (The run
  directory, below).
- `--refresh ROLE` (repeatable; `transcribe`, `extract`, `critique`): the
  role's views are made again in the run, current or not, and their calls
  go to the engine once per key whatever the cache holds; the new answer
  replaces the entry, which is kept aside. `extract` covers the GEDCOM
  adapter's batches too. A refresh belongs to its run.
- `--epoch ROLE=N` (repeatable): the role's calls are keyed under epoch
  N, to sample them afresh while the answers of other epochs stay.
  Current views stand, so combine it with `--refresh` to re-sample them.
- `--refresh` and `--epoch` act through the cache: `ingest run` refuses
  them with `--no-cache`.
- `yizkr ingest extract --cache DIR`: the single step answers from, and
  keeps its answers in, that cache; without it every call goes to the
  engine.
- `yizkr scratch run --cache DIR` and `--refresh ROLE`, as for `ingest
  run`. Every call of a scratch run goes through the cache: it has no
  `--no-cache`, and its fresh state directories make every view again,
  the cache answering the calls a change leaves the same.

## Seeding: visible equivalences

`seed(key, response, provenance, reason="adopted")` stores an answer the
engine did not give, for the call whose key the caching client computes
(`complete_key(prompt, image_paths)`); the entry keeps the provenance it
was given. A hit on it reports `reason` instead of a key match. It never
replaces an entry the key already has, never adopts an answer a caller
rejected under that key, and refuses the reason `engine`. The
archived-transcription import (#104) uses it once the transcription check
passes, with the client `Orchestrator.role_client("transcribe")` builds:
the role's client exactly as `ingest run` makes it (engine, effort, model,
pinned CLI, cache and epoch), so that the keys match the run's calls. A
CLI update changes every key: adoptions made before it are not found, and
those calls miss as `absent` until they are adopted again.

## The run directory

`yizkr ingest run` uses its state directory (`var/runs/<folder>/`) as its
run directory. The scratch driver (`yizkr scratch run`,
docs/runbooks/scratch-run.md) gives each run its own,
`var/scratch/<run id>/`, which is also the run's state directory, fresh
for each new run; a run's id starts with the UTC time it started, so the
runs sort as they started. Every file but `run.json` is append-only JSON
lines. Readers need no lock and skip a torn last line, so an interrupted
writer never corrupts a log.

| File | Written by | Read by |
|---|---|---|
| `run.json` | the driver at start (merged, replaced atomically) | everything |
| `events.jsonl` | the stage runner, the driver, the LLM cache, the engine's call reports, the anomaly checks | the monitor, the report, run state, the call estimate |
| `alerts.jsonl` | the anomaly checks | the monitor, the report |
| `control.jsonl` | the monitor, the anomaly checks, `yizkr scratch pause\|resume\|abort`; the driver's start marker, and the requests it carries into an invocation | the driver |

### `run.json`

The run manifest. `ingest run` writes `run_id`, `mode`, `cache` (the
cache's absolute path, or `null` without one), `config` (the engine,
languages, efforts, extraction model, `refresh` and `epochs`), and
`documents`: one entry per planned document, `{sha1, file, kind,
stages}`, where `kind` is the document's slice (`text`, `media`,
`presentation`, `gedcom`) and `stages` the stages planned for it; a
document that cannot be read is not planned (Views, above). The
full run manifest (#96) adds the rest: stage (development or kept),
engine and CLI version, per-stage model ids, commit, exclusion-list hash,
start and end.

### `events.jsonl`

Every line has `at` (UTC), `event` and `invocation` (one per run of
`ingest run`; one per recheck of the anomaly checks, `recheck-<hex>`;
and one for the anomaly checks' pause watch in a run's process,
`watch-<hex>`, whose only events are its failed looks' `anomaly-error`;
docs/ANOMALIES.md). Events about a document add `subject` (its media
SHA-1) and `kind`; `document-failed` and a directory's `set-aside` also
add `file`, the source's file name, which names a document whose source
could not be read and so has no `subject`; stage events add `stage`
(`transcription`, `extraction`, `critique` or `gedcom`; in a scratch
run, a corpus stage's `started`, `done`, `failed` and `held` name the
corpus stage, and no document); cache events add `role` and `key`, and
all of them but `cache-reject` and `cache-adopt` the kind of call
(`call`); call events add `role`, the `stage` the role's calls are made
in, and the kind of call:

| Event | Meaning | Extra fields |
|---|---|---|
| `run-start` | an invocation began | `run_id` |
| `started` | a stage was dispatched | |
| `done` | a stage completed | `attention`: why its output needs a look (a fallback tier, no claims) |
| `reused` | a stage's view was current and stands | `attention`: the reasons noted when it was made |
| `failed` | a stage failed | `reason` |
| `held` | the control file stopped a stage's dispatch | `reason` |
| `barrier` | a scratch run went past the barrier: every document's stages had ended, and its corpus stages come next | `documents` (whose stages all completed), `failed` (with a failed stage) |
| `refreshed` | in a scratch run, a stage's view made under a refresh of its role (`--refresh`) was saved: the run's later invocations that refresh the role reuse it | `role` |
| `set-aside` | a document directory was moved aside and started afresh; or, with `stage`, a transcription view that fails the check again was moved aside, to be made again | `file` (a directory's), `reason` (`content changed`, no `derivations.json`, or no `state.json`; or the check's reason), `kept` |
| `document-failed` | a document failed, and only it: a stage failed (with its own `failed` event too), or something outside its stages, such as its source or `state.json` that could not be read, its first record, or a step between the stages | `file`, `reason` |
| `cache-hit` | the cache answered a call | `reason`: `key match`, or an adoption's reason |
| `cache-miss` | the engine was asked (a fork: once its session is held) | `reason`: `absent`, `rejected` or `refresh` |
| `cache-resume` | a conversation was resumed exactly for a fork | |
| `cache-replay` | a conversation's opening turn was replayed for a fork's session | `matches` |
| `cache-reject` | a caller's rejection moved an answer aside | `reason` |
| `cache-adopt` | an answer was seeded as an adoption | `reason` |
| `call` | a call reached the engine, with or without the cache (a hit is none; a miss or a replay is one) | `outcome` (`ok`, `error`, `paused` or `interrupted`), `duration_s` (end to end, so it includes any time paced); when there is something to say, `wait_s` (the part of `duration_s` spent waiting for the subscription), `refused` (how many of its attempts a limit refused), `held` (why it last waited or paused: `usage limit`, `reserve`, `throttle`, `rate limit`, `overloaded`, `usage credits` or `closed`), `error` (the exception's name, never its message), and for a pause for a subscription limit its `window` and `resumes_at` (absent for `usage credits`: the subscription does not say when that clears) |
| `alert` | an anomaly alert was appended | `check`, `status` |
| `anomaly-recheck` | a recheck of the stored outputs began (docs/ANOMALIES.md) | `config` (the thresholds' digest), `documents` |
| `anomaly-error` | an anomaly check raised; nothing failed with it | `error` (the exception's name, never its message), `hook` |
| `run-end` | the invocation ended, other than by Ctrl-C | `outcome` and `reason` as `run.json` records them for the invocation (docs/RUN_MANIFEST.md: `finished`, `stopped`, `paused`, `refused` or `failed`), `statuses` (documents per status) |
| `run-interrupted` | the invocation was interrupted (Ctrl-C) | `outcome` (`interrupted`), `statuses` |

Every invocation's events end with `run-end` or `run-interrupted`,
however it ended: a paused pass, a refusal and an escaped error
included. So a reader of the events alone tells an invocation that ended
from one still running. Only a killed one ends with neither, and its
`ended_at` in `run.json` stays `null`.

A document has one `document-failed` event each time it fails. Serial
mode advances a document twice in one invocation (the extraction of
every document first, then its turn in the sequence), so a document
that fails both times has two; the later one stands. Count documents,
not events.

### `alerts.jsonl`

One alert per line: `at`, `stage`, `check`, `subject` (a SHA-1 prefix),
`value` (the measured value), and `status`: `new` on an output the engine
answered in this run, or one its stage makes without a call, `carried` on
one made from cached answers or reused whole. Each alert also appends an
`alert` event. The checks, and the fields each adds, are in
[docs/ANOMALIES.md](ANOMALIES.md).

### `control.jsonl`

Requests `{at, action, scope, by, reason}`: `action` is `pause`,
`resume` or `abort`; `scope` is `run`, `stage:<name>` or `kind:<name>`.
A fail-fast anomaly check's requests say `by: "anomaly"` and name their
`check` (docs/ANOMALIES.md). The driver folds them in order: a pause
holds until a resume, and an abort holds for the rest of the invocation.
A new invocation writes `{action: "start"}`, after which earlier
requests no longer apply: that is how an aborted run resumes. A stopped
scope stops dispatch only: in-flight calls finish, every completed call
is already in the cache, and a view that stands is reused, which
dispatches nothing. `ingest run` honours run and kind requests between
documents: a document it does not dispatch keeps its status. It honours
stage requests before every stage: a document whose stage is held is
`paused`. Either resumes on the next run.

The scratch driver (docs/runbooks/scratch-run.md) honours every request
before each stage of each document and before each corpus stage, with a
`held` event for each stage a stage's or a kind's request holds. Of the
requests that cover a stage (the run's, its stage's, its kind's), an
abort outranks a pause, since no resume can let the stage through. Other
slices go on, and when nothing else can, it waits for a paused slice's
resume; an aborted slice waits for the next invocation, and so does the
barrier while a document's stage is held; once an abort holds every
stage left, the invocation ends. When it invokes a run again by itself
after a subscription limit, it writes the requests in force just before
the new start marker (the whole run's included) again after it, with
who asked and why and `carried: true`, so what was stopped stays
stopped, a request made while the driver got ready again included.

## Run state

Run state per document and stage is derived from the run manifest and
the events alone, never from modification times or a per-document status
file (`rundir.run_state`, `yizkr stages state <run dir>`):

- every stage the manifest plans starts **pending**;
- the latest stage event for a (document, stage) sets its state:
  **running** once started; **done**, or **needs attention** with its
  reasons, once completed or reused; **failed** with its reason; or
  **pending** with the reason the control file held it. A stage that
  started with no later event is **running**. Run state does not look
  at how the invocation ended, so this includes a stage whose
  invocation's closing event (`run-end`, `run-interrupted`) follows it,
  or whose invocation was killed: that stage was cut off. A reader that
  needs to tell them apart looks for the closing event. The next run
  starts it again.

`yizkr stages state` also counts what the cache did for each role
(`rundir.cache_counts`): hits and misses by reason, resumes, replays,
rejections and adoptions. The counts are totals over every invocation the
run directory has seen.

## Subscription limits

The claude-code engine draws on the subscription's usage windows: a
session window of five hours and weekly ones, each reopening at a set
time. A call a limit refuses never fails a document, and the engine
paces its calls to leave a reserve of each window to development
sessions (#101; `yizkr.pipeline.llm`, "Subscription limits").

**What the CLI reports**, as the pinned 2.1.237 prints it in its
stream-json output:

- a `rate_limit_event` whenever its view of the limits changes, which in
  a new process, as each call is, means with the call's first response.
  Its `rate_limit_info` gives the `status` (`allowed`, `allowed_warning`
  or `rejected`), the window that binds (`rateLimitType`: `five_hour`,
  `seven_day`, `seven_day_opus`, ...), when it reopens (`resetsAt`, Unix
  seconds), whether the call ran on usage credits (`isUsingOverage`),
  and the share of it used (`utilization`), but only once that share
  passes one of the CLI's own warning thresholds: for the five-hour
  window, 90%, and only while at most 72% of the window has elapsed;
  for the weekly window, 75%, 50% or 25%, early in the week (as CLI
  2.1.237's `-p` mode reports them). Short of that threshold, or past
  its elapsed-time gate, no `utilization` is reported at all;
- a call the API refuses ends on an API error: an assistant event whose
  `error` is `rate_limit` (`overloaded` for an overload), with the CLI's
  notice as its text ("You've hit your session limit · resets 3pm"), and
  a result with `is_error` and `api_error_status` 429 (529); the process
  exits with 1. A used-up window is also reported `rejected`, with its
  `rateLimitType` and `resetsAt`. A refusal of another kind (the server
  limiting requests, which the CLI has already retried with a backoff of
  its own) gives no reset time.

**What it does not report**: the use of a window below the CLI's warning
thresholds (the CLI reads it from every response's headers, but shows it
only in its interactive status line), the size of a window in calls or
tokens, and how much of it other sessions use: the reported utilization
includes their use.

**A refused call** is never answered with its notice, so the cache never
keeps one. The engine asks again after the reported reset of a used-up
window (with a margin of 30 s), or after a backoff that doubles from one
minute up to eight when a refusal gives no reset time, or one the local
clock has passed already (a skewed clock). A hold that one call meets
holds every thread's next call, since the windows are the account's. A
hold that would take a call's wait past `$YIZKR_CLAUDE_MAX_WAIT` (in
seconds, all told; 16 minutes by default, a margin above the backoff
sequence's own sum) pauses the pass instead:
`UsageLimitReached`, a `PassPaused`, with the time the pass can go on
(`resumes_at`: the reset the CLI reported, else when the engine would
have asked again). The pause unwinds the invocation as any pause does: no
document fails, the invocation's outcome is `paused`
(docs/RUN_MANIFEST.md), every call answered before it is in the cache, a
stage it cut short counts as running and is made in the next run, and the
paused call's `call` event gives `resumes_at`. A wait ends early, as a
pause, when the engine's client is closed.

Waits happen inside the call, on the thread that makes it. Ctrl-C
interrupts a wait on the calling thread as it does any other work there;
it is only a wait on a chunk pool's worker threads that neither Ctrl-C
nor the control file reaches until it ends, since a pool's threads are
not the one Python delivers a signal to. So a window that reopens later
than the longest wait pauses the pass rather than hold a pool's threads
for hours: the scratch driver waits until a minute past `resumes_at`,
and a minute at least when that time has passed already (a pause can
hold past the time it reports), reading the control file as it waits,
and then invokes the run again by itself
(docs/runbooks/scratch-run.md). Set `$YIZKR_CLAUDE_MAX_WAIT` to 21600 to
wait through a five-hour window inside the call instead.

**The reserve** (`$YIZKR_CLAUDE_RESERVE`, a share of each window, 0.25 by
default; 0 keeps none). The engine estimates a window's use from its last
reported `utilization`, plus the use of every call completed or still in
flight since, at the use per call measured between the window's first
report and its latest. Dispatch stops at the reserve line until the
window reopens (a hold like any other, so one beyond the longest wait
pauses the pass), and short of the line it is spaced so that what is left
above the line lasts until the reset: with a share *left* above the line,
a use *per call* and *T* seconds to the reset, one call every
*T* × *per call* / *left* seconds.

**What the reserve achieves and what it cannot.** It only ever paces
from a report the CLI has actually sent, so it inherits exactly the gaps
above: below the first warning threshold there is no `utilization` to
pace against, so the reserve gives no protection there at all, whatever
`$YIZKR_CLAUDE_RESERVE` is set to; and a first warning that arrives late
— close to 100% already used, or past its elapsed-time gate — can give
little or none either, since there may be no room left to pace before
the CLI refuses outright at 100%. What it does achieve is pacing dispatch
from whichever warning it does get, so that ordinary use across a
window's many calls keeps what is left above the line spread out to the
reset rather than spent all at once. The only way to keep headroom below
the first warning is a local budget that needs no CLI report at all,
counting a window's own use independently; the owner decided
(2026-09-28) not to add one, so this reserve is best-effort pacing from
the CLI's own reports, not a guarantee of headroom. Two more limits:

- the use per call includes what other sessions used meanwhile, which
  makes the pacing stricter, never looser;
- a report that a call ran on usage credits (`isUsingOverage`) pauses
  the pass at once, process-wide, whatever the reserve, whatever window
  the report names or fails to, and whatever else it says or leaves
  out. This stops dispatch only once a call is *reported* to have run
  on credits: that call, and any others already in flight when the
  report arrives, may already have used credits; only turning extra
  usage off on the account guarantees none does. Nothing local says
  when the pause clears, so only a new process resumes; the operator
  confirms extra usage is off on the account first
  (docs/RUN_MANIFEST.md).

The reserve and the longest wait decide only when calls are made, never
what a call sends: they are no part of the engine's `effective_config`,
so no cache key depends on them, and `run.json` does not record them.

**The call estimate.** `llm.estimate_calls` gives, per stage, the calls a
planned run is expected to make for its stale work: its planned documents
(the run manifest's `documents`), less the (document, stage) pairs whose
views stand, of which a scratch run, starting from fresh state
directories, has none. A document's calls in a stage are those a baseline
run made for it (`llm.run_calls` over the baseline's `events.jsonl`: the
cache's hits and misses, or without the cache its `call` events; a stage
made without a call counts 0), else the baseline's mean for documents of
its kind, else typical: a vision call per scan and none for a text file
or slide text, the entity manifest and a chunk per 2000 characters for
extraction, and a critique each. These are the pipeline's calls,
counted without their keys. `yizkr scratch estimate`
(docs/runbooks/scratch-run.md, Before a run) counts a planned scratch
run's calls by their keys: it takes each stale stage through the
pipeline's own code with the cache as it is (`llm.read_cache`), so each
call the cache answers, and each it cannot, is known. It estimates only
the calls whose keys wait on an answer the engine has yet to give: by
`llm.estimate_calls`, or, for the forks after a conversation it replays,
by the chunks the cache kept.

## Checks once per run

**The PDF renderer** (poppler-utils) is checked once per run, before any
document, whenever the run holds a PDF: the vision engine reads a PDF
through it, and the transcription check counts its pages with it
(`pdfinfo`), whether the transcription comes from the engine or the
cache, or is a view the run reuses and checks again.

The per-document path goes when the scratch driver has run the corpus
end to end (#119). Until #103 lands, it still registers the
artifact in the wiki before the stages run; the stages themselves read
and write no wiki. A scratch run registers no artifact.
