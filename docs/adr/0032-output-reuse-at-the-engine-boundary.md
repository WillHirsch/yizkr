# ADR-0032: Output reuse at the engine boundary

Status: **accepted** — records the owner's decision of 2026-09-27 (#225)
and the design the owner agreed to. Like ADR-0024 and ADR-0025, it does
not pass through a `proposed` stage: the owner's merge of the PR that adds
it is its acceptance. It supersedes the stage store and the stage-version
registry of ADR-0025 §2, whose goals stand, and ADR-0025 carries a pointer
to it. ADR-0006 §5, ADR-0007 and ADR-0010 are amended in place where they
describe the store. The operational detail is in
[`docs/LLM_CACHE.md`](../LLM_CACHE.md).

## Context

ADR-0025 made whole-corpus scratch runs the way the pipeline is developed,
and §2 made them affordable: a stage store (#87, #209) kept every stage
output before the write under a key built from its input content, the
stage's declared version, its route and method version, its prompt and
manifest hashes, and its model. Its goals were reuse of unchanged work,
visible equivalences, and nothing completed lost on abort. The way it met
them had three costs.

- **The registry needed judgement at every edit.** A stage's version was a
  key component, so each edit to a listed source needed a version bump or
  a *functionally neutral* entry, and a drift test failed until one was
  made. The same judgement decided whether an edit changed outputs, which
  is what a scratch run is for. A source missing from the list escaped the
  keys silently: the transcription check and the engine's CLI configuration
  were two, found within hours of the merge (#218).
- **Stage keys reach too far.** The model call is the costly,
  non-deterministic step; parsing, merging and validation after it run in
  moments. With stage keys a fix to parsing recomputed its stage, model
  call included.
- **The store was threaded through the pipeline.** Keys, records, QID
  rebinding, failure logs and the hold for directories without records
  lived in the orchestrator and the extractor, so removing reuse later
  would have meant rewriting pipeline logic.

## Decision

### 1. Model responses are reused, keyed on what each call sends

A caching wrapper around the engine client (`LlmClient`,
`ConversationalLlmClient`) answers a call from the LLM cache when the same
call was answered before, and otherwise makes it and keeps the response.
Everything after the call runs on every run, on the cached answer or the
new one alike: parsing, the merge, validation, the transcription check,
the critique's parsing.

A key is the SHA-256 of the canonical JSON of:

- the engine, the model configuration names, and the effort;
- the client's `config_hash` (yizkr.pipeline.llm). For claude-code it
  covers the pinned CLI version, the arguments (the appended system prompt
  among them), the working directory, the environment rule and the pinned
  settings. For the API engine it covers the model and the output budget;
- the kind of call: one-shot, the opening turn of a conversation, or a
  fork of one;
- the SHA-256 of the system prompt the engine adds, and of the prompt text
  as the engine sends it. Every path of a file the call is given is
  replaced by that file's content SHA-256, so no key depends on where the
  corpus, a state directory or a temporary file lives;
- the content SHA-256 of every file the call may read. These are the
  files its caller passes (`image_paths`): the engine lets the model read
  only files a prompt names;
- for a fork, the key chain of the conversation it forks;
- the role's epoch (transcribe, extract, critique), 0 unless someone
  re-samples the role on purpose.

There is no registry and no version bump. A change to what a call sends,
whether it comes from a prompt, the manifest rendered into one, a model,
an effort, the engine's configuration, a file or the code that builds a
prompt, changes the key by itself. A change to what is done with a
response changes no key, and runs for free on the cached responses.

**Not keyed.** Nothing on the engine's side of the call can be keyed. The
claude CLI fetches account data from the server at every start, which can
change its system-prompt text, its default model and its compaction
windows (llm.py says what the engine pins against it), and the CLI fills
details of its own into its system prompt when it runs, such as the date.
A key names a model only when configuration names one; when the engine
picks its own default, a change to that default is not seen, and the
cache warns once per role. Every entry records the model the engine
reported, so a reuse across such a change can be found. Artifact QIDs are
keyed where a prompt cites one: until #127 takes them out of the
extraction prompts, a reset that gives an artifact a new QID asks the
engine again for its extraction. Transcription prompts cite none.

### 2. What is stored, and where

- The cache lives under `var/llm-cache` by default, is local-only data
  like the rest of `var/` (ADR-0024 §4), and is private to its owner (mode
  700). Each key has one entry, written atomically: a reader sees an entry
  whole or not at all, and calls answered in parallel under one key leave
  one whole entry.
- Only a response the engine returned is stored. An error, a timeout or a
  pause (`PassPaused`) stores nothing, so the next run asks again.
- Each entry records its provenance: the role, the engine, the model the
  engine reported, the CLI version, the client's config hash, the time,
  and its origin (the engine, or an adoption's reason).
- The cache deletes nothing. An entry a caller rejects, or one a refresh
  replaces, is moved aside under a name no lookup reads, and kept for
  inspection.
- The entries are model responses, the raw material of proposals, not a
  second store of assertions: the graph stays the system of record, as it
  was with the stage store.

### 3. Rejection

A caller that cannot use a response rejects it, through an optional client
capability (`RejectingLlmClient`) that an engine's own client does not
have. The entry is moved aside with the reason and never served again, so
the next identical call, a retry in the same run or a call in the next
one, goes to the engine. The rejectors are:

- the transcription check, which runs on every transcription in every run,
  whether it came from the engine or the cache. A stricter check therefore
  applies to every cached transcription at once, and no check version is
  keyed; a cached transcription it fails is rejected, its document fails,
  and the next run transcribes it again;
- the chunked extractor, for a chunk cut off before its terminal sentinel,
  and for an entity manifest or a segmentation it refuses. Without the
  rejection its retry would be answered with the same text.

The price: a parser fix recovers every response the old parser accepted,
without a call, but not one it rejected, which is asked again. ADR-0006 §5
is amended accordingly.

### 4. Conversations and forks

Opening a conversation is a call like any other, and the handle the cache
returns carries its key chain; a fork is keyed on that chain. A fork the
cache answers needs no session. A fork it cannot answer, of a conversation
whose opening turn the cache answered, has no session to fork: the opening
turn is replayed once, for real, to obtain one, and that session serves
every later fork of the conversation in the run. The replay's own answer
is not stored, since the cached one stands. It can differ from the cached
answer, and the fork then sees the replayed answer in its context while
its prompt was built from the cached one. The fork is keyed on the chain
all the same: a key that took the replayed answer in would never be
answered twice. The replay event says whether the answers matched.

Because forks are served through the cache whether or not a session
exists, chunk calls always fork (ADR-0006 §3, context mode 1), and their
prompts, and so their keys, stay the same from run to run.

### 5. Switches

- `yizkr ingest run --cache DIR` (default `var/llm-cache`).
- `--refresh ROLE` (repeatable) sends the role's calls to the engine once
  per key in the run whatever the cache holds; the new answer replaces the
  entry, and the old one is kept aside. It replaces `--recompute`.
- `--epoch ROLE=N` keys the role's calls under epoch N, to sample them
  afresh while the answers of other epochs stay.
- `--no-cache` calls the engine directly: no wrapper, nothing read or
  written. The run is otherwise the same; a unit test shows that a replay
  from the cache and a run without it give the same outputs.

### 6. Events

Every answer the cache gives or cannot give is an event in the run's
`events.jsonl`, with the document's SHA-1, its kind, the role, the kind of
call and the key: `cache-hit` (reason `key match`, or the adoption's
reason), `cache-miss` (reason `absent`, `rejected` or `refresh`),
`cache-replay` (whether the answers matched), `cache-reject` (the reason)
and `cache-adopt`. The monitor (#195) and the run report (#124) count
reuse from them, and `yizkr stages state` prints the counts per role.

### 7. Seeding: visible equivalences

`seed(key, response, provenance, reason="adopted")` stores an answer the
engine did not give, for the key of the call it stands for, which the
caching client computes (`complete_key`). A hit on it reports its reason,
not a key match, and it keeps the provenance it was given. It never
replaces an entry the key already has. The archived-transcription import
(#104) uses it after the transcription check passes; the check then runs
again on every run, like any transcription's. The import is #104's.

### 8. The pipeline does not depend on the cache

Only the client factory, `yizkr.pipeline.llm`, imports the cache module
(`yizkr.pipeline.llmcache`): `open_cache` opens a cache, and
`cached_client` wraps a client. A unit test fails on any other import. The
orchestrator asks the factory for its clients and the extractor and
transcriber call whatever client they are given, so their unit tests run
with scripted clients and no cache, and removing the cache means no longer
wrapping the client.

### 9. What stays of ADR-0025 §2 and #209

- Content-hash freshness of the files after the batch (`derivations.json`):
  a derived file is current while its inputs hash the same, the batch view
  by its canonical content, so an identical recompute, which a run answered
  from the cache is, invalidates nothing.
- The run directory: `run.json`, `events.jsonl`, `alerts.jsonl` and
  `control.jsonl`. Stage events become `started`, `done` (with attention
  reasons), `failed` and `held`, for the stages `transcription`,
  `extraction`, `critique` and `gedcom`; a control request can stop each of
  them.
- The changed-source set-aside, and atomic writes.
- Stages before write still read nothing from the wiki: their inputs are
  the corpus, configuration and the answers of the engine or the cache
  (ADR-0025 §2).

### 10. What goes

- The stage store and its records, keys, failure logs and adoptions.
- The stage-version registry (`stageversions.py`, `stage_versions.json`),
  its drift test, and `yizkr stages check|lock`.
- `--store` and `--recompute`.
- The hold of a document directory without `derivations.json`, its
  adoption by modification times, and `--rederive-unrecorded`. Such a
  directory is set aside whole and started afresh, as a changed source is
  (ADR-0010), with nothing in it deleted. Modification times are read
  nowhere.
- The per-call check for the PDF renderer: a run checks the host once,
  before any document (#218).

## Consequences

- A code change to a stage costs no model call unless it changes what a
  call sends, and needs no version bump or neutral entry. The scratch diff
  (ADR-0025 §4) stays the evidence for a change to a stage before write,
  and reads "its source with a version bump" as a change to its code:
  without a registry, whether a code change alters outputs is what the
  diff shows, and replaying cached responses makes that run cheap. The
  stage-label rule in docs/AGENT_PROTOCOL.md §2 and the PR template still
  name the registry, and follow this ADR.
- Every run derives every document's stages again from the cache: parsing
  and merging cost moments per document, and the engine clients start
  once per role per run. A run with `--no-cache` asks the engine for
  every call of every document.
- Reuse is exact only while each call's inputs reach its key: its text, its
  files and its engine configuration do by construction; what the server
  and the CLI add at run time does not (§1).
- A directory from before `derivations.json` loses its registration and
  reviews to a fresh start, kept aside for inspection.
- The pre-#225 stage store under `var/stages` is no longer read; the owner
  may delete it.

## Alternatives considered

- **Keep the stage store and key its stages on `config_hash` and the check
  version (#218).** Rejected: it keeps the registry and its judgement, and
  a parser fix still recomputes the stage's calls.
- **Stage keys hashed from the stage's source automatically.** Rejected:
  what code shapes an output is transitive and hard to enumerate, and any
  change to it still recomputes the model calls.
- **Key a fork on its parent's answer too.** Rejected: a replay that
  answers differently would give a key no later run reproduces, so the
  fork would never be served from the cache.
- **Key retries by attempt number, and replay them.** Rejected: a chunk
  that failed every attempt would fail again from the cache in every run;
  rejection asks again instead.
- **Delete rejected entries.** Rejected: they are the evidence of what a
  check or a parser refused.
