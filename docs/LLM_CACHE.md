# The LLM cache and the run directory

Development runs the whole corpus through the pipeline again and again
(ADR-0025). The LLM cache makes that affordable (ADR-0032): every model
call's response is kept under a key built from what the call sends, so a
call made again is answered from the cache, while everything after the
call (parsing, the merge, validation, the transcription check) runs again
on every run. The run directory beside it records what each run did, for
the driver (#194), the live monitor (#195), the anomaly checks (#196),
pacing (#101) and the run report (#124).

Both are runtime state under `var/` and are **local-only data**
(docs/AGENT_PROTOCOL.md §6): they hold transcriptions, claims and raw
model output of the corpus. Nothing from them is copied into the
repository, GitHub or a cloud session.

Code: `yizkr.pipeline.llmcache` (the cache), `yizkr.pipeline.llm` (the
engines, and the client factory that alone imports the cache:
`open_cache`, `cached_client`) and `yizkr.pipeline.rundir` (the run
directory and run state).

## Keys

A key is the SHA-256 of the canonical JSON of its components:

| Component | What it holds |
|---|---|
| `engine` | `claude-code` or `anthropic` |
| `model` | The model configuration names: the role's own model (`--extract-model` for extraction), else `$YIZKR_LLM_MODEL`; `null` when the engine picks its own default |
| `effort` | The reasoning effort (claude-code only; `null` for the API) |
| `config` | The client's `config_hash`. claude-code: the pinned CLI version, the arguments (the appended system prompt, `--model` and `--effort` among them), the working directory, the environment rule and the pinned settings. anthropic: the model and the output budget |
| `call` | `complete` (one-shot), `open` (a conversation's opening turn) or `fork` |
| `system` | SHA-256 of the system prompt the engine adds (claude-code's `--append-system-prompt` text) |
| `prompt` | SHA-256 of the text the engine sends (claude-code: the prompt after the line that points the Read tool at each image), with every path of a file the call is given replaced by `sha256:<the file's content SHA-256>` |
| `files` | The content SHA-256 of each file the call may read: the files its caller passes (`image_paths`), in order |
| `parent` | For a fork, the keys of the conversation it forks |
| `epoch` | The role's epoch: 0 unless `--epoch ROLE=N` says otherwise |

The role itself is not a component: two roles making the same call share
its entry.

**What a key does not see.** Anything added on the engine's side of the
call. The claude CLI fetches account data from the server at every start,
which can change its system-prompt text, its default model and its
compaction windows (see the engine's docs in `yizkr.pipeline.llm`), and
it fills details of its own into its system prompt when it runs, such as
the date. When no model is configured for a role, the engine's default
model is not keyed: a change to it is not seen, and the cache logs a
warning once per role. Every entry records the model the engine reported,
so a reuse across such a change can be found. Set `$YIZKR_LLM_MODEL` for
any run whose answers are to be reused.

**QIDs.** A prompt that cites an artifact QID is keyed on it, since the
QID is part of what the call sends. Until #127 takes QIDs out of the
extraction prompts, a reset that gives an artifact a new QID asks the
engine again for that artifact's extraction calls. The transcription
prompt cites no QID.

## Layout

```
var/llm-cache/                     (--cache; mode 700)
  <first two hex digits of the key>/
    <key>.json                     the entry the call is served
    <key>.rejected-<stamp>.json    a response a caller rejected
    <key>.replaced-<stamp>.json    a response a refresh replaced
```

An entry holds `format`, `key`, `components` (the key's components, whose
canonical JSON hashes to the key), `response`, `origin` (`engine`, or an
adoption's reason) and `provenance`: the `role`, the `call`, the
`engine`, the `model` the engine reported, the `cli_version`, the
`config_hash`, the client's `model_identifier` and the time (`at`); a
fork answered on a replayed session says so (`session: replayed`). An
entry moved aside gains `rejected` (`at`, `reason`, `role`) or `replaced`
(`at`).

**Writes are atomic.** An entry is written to a staging file beside it,
flushed, and renamed into place: a reader sees an entry whole or not at
all, a staging file is never read as an entry, and calls answered in
parallel under one key leave one whole entry. A damaged entry is a miss.
**Only a response the engine returned is stored**: an error, a timeout or
a pause (`PassPaused`) stores nothing, so the next run asks again. **The
cache deletes nothing**: an entry it stops serving is moved aside and
kept.

## Rejection

A caller that cannot use a response rejects it, through the client
(`llm.reject_response`; an engine's own client has nothing to reject).
The entry is moved aside as `<key>.rejected-<stamp>.json`, with the
reason, and is never served again: the next call under the key, a retry
in the same run or a call in the next, goes to the engine, and its miss
says `rejected`. Rejected are:

- a transcription that fails the transcription check. The check runs on
  every transcription in every run, from the engine or from the cache, so
  a stricter check applies to every cached transcription at once; the
  document fails, and the next run transcribes it again;
- a chunk response cut off before its terminal sentinel;
- an entity manifest or a segmentation the extractor refuses.

So a parser fix recovers every response the old parser accepted, without
a call, but not one it rejected.

## Conversations and forks

Opening a conversation is a call like any other; the handle the cache
returns carries the key chain its forks are keyed on. A fork the cache
answers needs no session. A fork it cannot answer, of a conversation the
cache opened, has no session to fork: the opening turn is replayed once,
for real, and the session it gives serves every later fork of that
conversation. The replay's answer is not stored (the cached one stands);
the `cache-replay` event says whether it matched. A replayed answer that
differs sits in the fork's context while the fork's prompt was built from
the cached one.

## Switches

- `yizkr ingest run --cache DIR`: the cache (default `var/llm-cache`).
- `--no-cache`: the engine directly; nothing is read or written. The run
  is otherwise the same.
- `--refresh ROLE` (repeatable; `transcribe`, `extract`, `critique`): the
  role's calls go to the engine once per key in the run, whatever the
  cache holds; the new answer replaces the entry, which is kept aside.
- `--epoch ROLE=N` (repeatable): the role's calls are keyed under epoch
  N, to sample them afresh while the answers of other epochs stay.
- `yizkr ingest extract --cache DIR`: the single step answers from, and
  keeps its answers in, that cache; without it every call goes to the
  engine.

## Seeding: visible equivalences

`seed(key, response, provenance, reason="adopted")` stores an answer the
engine did not give, for the call whose key the caching client computes
(`complete_key(prompt, image_paths)`); the entry keeps the provenance it
was given. A hit on it reports `reason` instead of a key match. It never
replaces an entry the key already has. The archived-transcription import
(#104) uses it once the transcription check passes; the check then runs
again on every run.

## The run directory

`yizkr ingest run` uses its state directory (`var/runs/<folder>/`) as its
run directory; the scratch driver gives each run its own. Every file but
`run.json` is append-only JSON lines. Readers need no lock and skip a
torn last line, so an interrupted writer never corrupts a log.

| File | Written by | Read by |
|---|---|---|
| `run.json` | the driver at start (merged, replaced atomically) | everything |
| `events.jsonl` | the stage runner, the driver, the LLM cache, the anomaly checks | the monitor, the report, run state |
| `alerts.jsonl` | the anomaly checks | the monitor, the report |
| `control.jsonl` | the monitor, the anomaly checks, pacing, `yizkr scratch abort`; the driver's start marker | the driver |

### `run.json`

The run manifest. `ingest run` writes `run_id`, `mode`, `cache` (the
cache's path, or `null` without one), `config` (the engine, languages,
efforts, extraction model, `refresh` and `epochs`), and `documents`: one
entry per planned document, `{sha1, file, kind, stages}`, where `kind` is
the document's slice (`text`, `media`, `presentation`, `gedcom`) and
`stages` the stages planned for it. The full run manifest (#96) adds the
rest: stage (development or kept), engine and CLI version, per-stage model
ids, commit, exclusion-list hash, start and end.

### `events.jsonl`

Every line has `at` (UTC), `event` and `invocation` (one per run of `ingest
run`). Events about a document add `subject` (its media SHA-1) and `kind`;
stage events add `stage` (`transcription`, `extraction`, `critique` or
`gedcom`), and cache events `role`, `call` and `key`:

| Event | Meaning | Extra fields |
|---|---|---|
| `run-start` | an invocation began | `run_id` |
| `started` | a stage was dispatched | |
| `done` | a stage completed | `attention`: why its output needs a look (a fallback tier, no claims) |
| `failed` | a stage failed | `reason` |
| `held` | the control file stopped a stage's dispatch | `reason` |
| `set-aside` | a document directory was moved aside and started afresh | `reason` (`content changed`, or no `derivations.json`), `kept` |
| `cache-hit` | the cache answered a call | `reason`: `key match`, or an adoption's reason |
| `cache-miss` | the engine was asked | `reason`: `absent`, `rejected` or `refresh` |
| `cache-replay` | a conversation's opening turn was replayed for a fork's session | `matches` |
| `cache-reject` | a caller rejected an answer | `reason` |
| `cache-adopt` | an answer was seeded as an adoption | `reason` |
| `alert` | an anomaly alert was appended | `check`, `status` |
| `run-end` | the invocation finished | `statuses` |
| `run-interrupted` | the invocation was interrupted (Ctrl-C) | |

### `alerts.jsonl`

One alert per line: `at`, `stage`, `check`, `subject` (a SHA-1 prefix),
`value` (the measured value), and `status`: `new` on an output the engine
answered in this run, `carried` on one made from cached answers. Each
alert also appends an `alert` event.

### `control.jsonl`

Requests `{at, action, scope, by, reason}`: `action` is `pause`,
`resume` or `abort`; `scope` is `run`, `stage:<name>` or `kind:<name>`.
The driver folds them in order: a pause holds until a resume, and an
abort holds for the rest of the invocation. A new invocation writes
`{action: "start"}`, after which earlier requests no longer apply: that
is how an aborted run resumes. A stopped scope stops dispatch only:
in-flight calls finish, and every completed call is already in the
cache. `ingest run` honours run and kind requests between documents and
stage requests before every stage; a document the control file stops is
`paused` and resumes on the next run.

## Run state

Run state per document and stage is derived from the run manifest and
the events alone, never from modification times or a per-document status
file (`rundir.run_state`, `yizkr stages state <run dir>`):

- every stage the manifest plans starts **pending**;
- the latest stage event for a (document, stage) sets its state:
  **running** once started, **done**, **needs attention** with its
  reasons, **failed** with its reason, or **pending** with the reason the
  control file held it. A stage that started with no later event was
  running, or was interrupted; the next run starts it again.

`yizkr stages state` also counts what the cache did for each role
(`rundir.cache_counts`): hits and misses by reason, replays, rejections
and adoptions.

## `yizkr ingest run` on the cache

Every run derives each document's stages before the review gates again,
once per run: its transcription (routed per document: vision, text file,
slide text, or a curator's correction), its extraction and its critique,
or the GEDCOM adapter. With the cache, an unchanged document's calls are
all answered from it. The document directory keeps plain-file views for
reading and review (`transcription.json`, `batch.json`, `critique.json`,
and the extractor's `extraction/coverage.json`, `rejects.json` and
`gaps.ndjson`) and the files the later steps derive from them. Their
dependencies are recorded by content hash in `derivations.json`:

- a batch whose canonical content changed invalidates every derived file
  and moves the reviewed CSVs aside; a batch with the same content, such
  as one derived from cached answers or a deleted view rebuilt,
  invalidates nothing: files derived from the batch record its canonical
  content hash, not the view's bytes;
- a re-exported CSV whose content changed moves its reviewed file aside;
  an identical re-export keeps it;
- a reviewed CSV consents for the CSV it was first seen with: saving it
  again re-writes, and editing the CSV it reviews re-arms the gate.

| Derived file | Made from |
|---|---|
| `equivalences.csv` | `batch.json` |
| `batch.resolved.json` | `batch.json`, `equivalences.reviewed.csv` |
| `review.csv` | `batch.resolved.json`, `critique.json` |
| `write.json` | `review.reviewed.csv`, `batch.resolved.json` |

`equivalences.reviewed.csv` reviews `equivalences.csv`, and
`review.reviewed.csv` reviews `review.csv`.

**A changed source, or a directory without its record.** Different bytes
are a different artifact (ADR-0010). When a document's source no longer
has the SHA-1 its `state.json` recorded, or its directory has files but
no `derivations.json` (a directory from before the records, or one that
lost its record, so nothing says what its files were derived from),
`ingest run` moves the directory aside whole, as
`.<slug>.superseded-<n>` in the state directory, and ingests the document
afresh in a new one: registration, stages and gates. No document slug
starts with a dot, so none can take that name. Nothing in the old
directory is deleted, the log and a `set-aside` event name it, and the
bytes it recorded seed no content dedup. Every directory `ingest run`
makes has its `derivations.json` from its first run, written before any
other file, and no state is saved into a directory without it, so a run
stopped at that point leaves nothing to set aside.

**The transcription check** runs on every vision and text-file
transcription in every run, whether the engine made it in this run or the
cache kept it; a vision transcription that fails it is rejected in the
cache. **The PDF renderer** (poppler-utils) is checked once per run,
before any document, whenever the run holds a PDF: the vision engine
reads a PDF through it, and the check counts its pages with it, cached
transcription or not.

To correct a transcription, save the corrected text as
`<doc>/transcription.corrected.txt`. The document is routed to the
correction, and exactly what depends on the text changes. Delete the file
to return to the original: a vision transcription is still in the cache.

The per-document path goes when the scratch driver has run the corpus
end to end (#119). Until #128 and #103 land, it still registers the
artifact in the wiki before the stages run; the stages themselves read
and write no wiki. The stage store that `ingest run` kept under
`var/stages` before ADR-0032 is no longer read.
