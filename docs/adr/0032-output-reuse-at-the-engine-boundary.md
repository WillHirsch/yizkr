# ADR-0032: Output reuse at the engine boundary

Status: **accepted** — records the owner's decision of 2026-09-27 (#225)
and the design the owner agreed to. Like ADR-0024 and ADR-0025, it does
not pass through a `proposed` stage: the owner's merge of the PR that adds
it is its acceptance. It supersedes the stage store and the stage-version
registry of ADR-0025 §2, whose goals stand, and ADR-0025 carries a pointer
to it and is amended in place where it names stage keys (§2's write-side
key, §3's run report, §4's diff). ADR-0006 §5, ADR-0007 and ADR-0010 are
amended in place where they describe the store. The operational detail
is in [`docs/LLM_CACHE.md`](../LLM_CACHE.md).

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

A model answers differently each time it is asked. Whatever replaces the
store must not make the pipeline's stability depend on reuse: a run
without it must still leave a document's batch, and so its reviews,
where they were.

## Decision

### 1. Views persist; the cache only saves engine calls

A document's views, the outputs a gate or a later stage reads
(`transcription.json`, `batch.json`, `critique.json`, and the files the
later steps derive from them), persist in its state directory. Each is
recorded in `derivations.json` with the content hashes of the inputs it
was made from, and is reused while those are unchanged:

- the transcription is made from the curator's correction, when there is
  one (`transcription.corrected.txt`), and otherwise from the source,
  whose bytes the directory is kept for (a changed source sets the
  directory aside, ADR-0010);
- the batch from the transcription, by its canonical content (its text,
  language and artifact; not the route or engine build that made it). A
  GEDCOM file's batch is made from the source alone;
- the critique from the batch, by its canonical content (without its run
  stamps);
- the later files as before (§11).

A change of prompt, model or code never makes a stored view stale by
itself, with one exception: the transcription check, below. There are two
ways to make views again:

- `--refresh ROLE` makes the role's views again in place, current or not,
  bypassing both the view and the cache (§7). A view made again with the
  same content invalidates nothing after it.
- A scratch run starts from fresh state directories (#194), where every
  view is made again and the cache makes that cheap.

A reused view is a `reused` event for its stage (§8). A vision or
text-file transcription, the routes whose transcribers check their
output, is checked again each time it is reused, with the transcription
check as it is now (§4); a curator's correction and slide text are never
checked. A transcription the check now fails is moved aside as
`transcription.superseded-<n>.json`, never deleted, with a `set-aside`
event giving the reason; its record is forgotten, and it is made again in
the same run. The new transcription is checked as any other, and when the
batch made from it differs, the reviews of the old batch are moved aside
as usual. So a check made stricter reaches every stored transcription,
and a failed one never stays under its batch and its reviews on the way
to the write. The check needs only the text and, for a PDF, its page
count.

The LLM cache (§2 to §6) is then only an accelerator: it saves the engine
calls of a view made again. Without it (`--no-cache`) the pipeline
behaves the same, apart from making those calls. Where a caller rejects an
answer the cache kept from an earlier call (a transcription a stricter
check fails, say), it asks once more in the same run, so the engine
answers the call there as it would without the cache (§4). Unit tests
hold this: with no cache and an engine that words its answers afresh on
every call, a document passes both review gates across runs and is
written, with no review moved aside; and in a fresh state directory, a
cached transcription a stricter check fails ends the run with the status
a run without the cache reaches.

### 2. Model responses are reused, keyed on what each call sends

A caching wrapper around the engine client (`LlmClient`,
`ConversationalLlmClient`) answers a call from the LLM cache when the same
call was answered before, and otherwise makes it and keeps the response.
What is done with a response runs whenever a view is made, on a cached
answer and a new one alike: parsing, the merge, validation, the
transcription check, the critique's parsing.

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
  files its caller passes (`image_paths`), the only files a pipeline
  prompt names; the engine's system prompt tells the model to read no
  others, though its Read tool does not enforce that;
- for a fork, the key chain of the conversation it forks and the SHA-256
  of the conversation's opening answer, which the fork's context holds: a
  kept session is resumed only for the opening answer it was made on
  (§6);
- the attempt, for a caller that asks again after an answer it cannot use
  and then falls back (§5); 0 otherwise;
- the role's epoch (transcribe, extract, critique), 0 unless someone
  re-samples the role on purpose.

There is no registry and no version bump. A change to what a call sends,
whether it comes from a prompt, the manifest rendered into one, a model,
an effort, the engine's configuration, a file or the code that builds a
prompt, changes the key by itself. A change to what is done with a
response changes no key, and runs on the cached responses without a call
whenever a view is made again. A claude CLI update changes `config_hash`,
and with it every key.

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

### 3. What is stored, and where

- The cache lives under `var/llm-cache` by default, is local-only data
  like the rest of `var/` (ADR-0024 §4), and is private to its owner (mode
  700, applied to folders that exist already). Each key has one live
  entry, published whole by a link that fails where the key has one
  already: a reader sees an entry whole or not at all.
- Only a response the engine returned with some text in it is stored. An
  error, a timeout, a pause (`PassPaused`) or an empty answer stores
  nothing, so the next run asks again. The API engine's client refuses an
  answer without text, as the claude CLI's does.
- Each entry records its key, the key's components, and its provenance:
  the role, the engine, the model the engine reported, the CLI version,
  the client's config hash, the time, and its origin (the engine, or an
  adoption's reason). An entry is served only under the key it records.
- The cache deletes nothing and overwrites nothing. An entry a caller
  rejects is moved aside with the reason; one a newer entry displaces (a
  refresh's, a racing call's, one displacing a damaged entry) is moved
  aside as replaced. Both are kept for inspection under names no lookup
  reads.
- A conversation's opening turn keeps, beside its entry, the session files
  the engine needs to resume it exactly, under the SHA-256 of the opening
  answer they hold (§6).
- The entries are model responses, the raw material of proposals, not a
  second store of assertions: the graph stays the system of record, as it
  was with the stage store.

### 4. Rejection

A caller that cannot use a response rejects it, through an optional client
capability (`RejectingLlmClient`) that an engine's own client does not
have. The entry is moved aside with the reason and never served again, so
the next identical call, a retry in the same run or a call in the next
one, goes to the engine. A rejection moves only the answer it names: the
entry is first renamed to a private name and only then compared, and an
answer another caller stored meanwhile stays live.

A rejection also tells the caller whether the answer was one the cache
kept from an earlier call (a hit) rather than the engine's answer to this
call. When it was, the engine has not answered the call in this run, so
the caller asks once more, once, and the engine's answer takes the
rejected one's place, just as it would without the cache: a chunk spends
no retry on it, and the transcriber, the critic and the single-call
extractor use the new answer. The rejectors are:

- the transcription check, which runs on every transcription a run makes,
  from the engine or from the cache, and on every transcription view a run
  reuses (§1). A cached transcription it fails is rejected and asked for
  once more; a failure of the engine's own answer fails the document, and
  the next run transcribes it again. No check version is keyed: a
  stricter check applies to every transcription, the stored views
  included;
- the chunked extractor, for a chunk cut off before its terminal
  sentinel;
- the critic, for an answer without its terminal sentinel (a refusal, one
  cut off, a usage-limit notice). The critique stays advisory: the
  findings the answer holds are kept, and the critic never raises;
- the single-call extractor (`ingest extract --strategy single`), for an
  answer with no claim JSON or no valid claim, which fails the
  extraction, and for one without its terminal sentinel, whose claims
  are kept.

The price: a parser fix recovers every response the old parser accepted,
without a call, but not one it rejected, which is asked again. ADR-0006 §5
is amended accordingly.

### 5. Attempts

A caller that asks again after an answer it cannot use, and then falls
back to something else, keys each attempt apart (`llm.for_attempt`) rather
than rejecting the refused answer. The chunked extractor does so for the
entity manifest and for the segmentation behind it: two attempts each,
then the next tier (ADR-0006 §2, ADR-0007). A refused answer is kept, so a
later run is answered attempt by attempt from the cache, takes the same
fallback and gives the same batch, whatever the engine would answer now.
A refused manifest falls back rather than failing the document, so the
reason not to key chunk retries this way (a chunk that failed every
attempt would fail again from the cache in every run) does not apply to
it. `--refresh extract` or a new epoch asks again.

### 6. Conversations and forks

Opening a conversation is a call like any other. The handle the cache
returns carries the conversation's key chain and its opening answer; a
fork is keyed on both. A fork the cache answers needs no session. A fork
it cannot answer, of a conversation whose opening turn the cache
answered, needs the engine to hold that conversation exactly as the
fork's context assumes:

- **Resumed exactly, without a call.** When the conversation is opened,
  the cache keeps what the engine needs to resume it
  (`ResumableLlmClient`): for claude-code, the session's transcript files,
  beside the opening turn's entry under the cache folder (local-only, in
  `var/`). They are kept under the SHA-256 of the opening answer the
  session holds (`<key>.session/<opening>/`), published whole with it, and
  a fork resumes only the session kept for the opening answer the cache
  serves. Two callers that open the same conversation at once, or a replay
  that stored a new answer but could not keep its session, can leave a
  session beside an entry that holds another answer; that session is never
  resumed for it: a fork resumes the session kept for the served answer,
  when there is one, and otherwise replays the opening turn. The files are
  put back into the CLI's projects folder for `--resume … --fork-session`,
  and released after the forks, as llm.py releases every transcript. The
  API engine rebuilds the conversation from the opening turn's prompt and
  cached answer, with no call.
- **Replayed, only when nothing usable is kept.** The opening turn is
  asked again, and its answer compared with the cached one. When they
  match, the replay's session serves the forks, and is kept for the next
  run to resume. When they differ, the whole conversation is a miss: the
  replay's answer becomes the opening turn's entry (the old one is kept
  aside), its session is kept under the new answer, the fork raises
  `ConversationChanged`, and the extractor starts over from the new
  answer, whose forks are keyed apart from the old ones and are made on
  the replay's session.

A session, however obtained, serves every later fork of the conversation
in the run. Every fork entry records how its session was obtained
(`live`, `resumed`, `replayed` or `recomputed`) and the opening answer's
SHA-256. Because forks are served through the cache whether or not a
session exists, chunk calls always fork (ADR-0006 §3, context mode 1), and
their prompts, and so their keys, stay the same from run to run.

### 7. Switches

- `yizkr ingest run --cache DIR` (default `var/llm-cache`).
- `--refresh ROLE` (repeatable) makes the role's views again in the run
  (§1), and sends their calls to the engine once per key whatever the
  cache holds; the new answer replaces the entry, and the old one is kept
  aside. `extract` covers the GEDCOM adapter's batches too. It replaces
  `--recompute`. A refresh belongs to its run: the next run is served
  again.
- `--epoch ROLE=N` keys the role's calls under epoch N, to sample them
  afresh while the answers of other epochs stay. A current view stands,
  so an epoch applies to the views a run makes again: combine it with
  `--refresh` to re-sample them.
- `--no-cache` calls the engine directly: no wrapper, nothing read or
  written. The run is otherwise the same (§1): a rejected answer the cache
  kept is asked for once more in its run (§4), and a unit test also shows
  that a replay from the cache and a run without it give the same views.
  `--refresh` and `--epoch` act through the cache, and are refused with
  `--no-cache`.
- `yizkr ingest extract --cache DIR` takes the single step's calls through
  that cache.

### 8. Events

Every answer the cache gives or cannot give is an event in the run's
`events.jsonl`, with the document's SHA-1, its kind, the role and the key:
`cache-hit` (reason `key match`, or the adoption's reason), `cache-miss`
(reason `absent`, `rejected` or `refresh`), `cache-resume` (a
conversation resumed exactly), `cache-replay` (whether the answers
matched), `cache-reject` (the reason; only when an answer was moved
aside) and `cache-adopt`. All but rejections and adoptions name the kind
of call. A view a run reuses is a `reused` stage event, with the attention
reasons noted when it was made; a transcription view that fails the check
again is a `set-aside` event, with its stage, the reason and the name it
is kept under (§1). The monitor (#195) and the run report
(#124) count reuse from them, and `yizkr stages state` prints the cache
counts per role, as totals over every invocation of the run directory.

### 9. Seeding: visible equivalences

`seed(key, response, provenance, reason="adopted")` stores an answer the
engine did not give, for the key of the call it stands for, which the
caching client computes (`complete_key`). A hit on it reports its reason,
not a key match, and it keeps the provenance it was given. It never
replaces an entry the key already has, never adopts an answer a caller
rejected under that key, and refuses the reason `engine`, which marks the
engine's own answers. The archived-transcription import (#104) uses it
after the transcription check passes, and takes its client from
`Orchestrator.role_client(role)`, which builds the role's client exactly
as `ingest run` does (engine, effort, model, pinned CLI, cache and epoch),
so that its keys are the ones the run's calls are served under. A CLI
update changes every key (§2): adoptions made before it are no longer
found, and those calls miss as `absent` until they are adopted again. The
import is #104's.

### 10. The pipeline does not depend on the cache

Only the client factory, `yizkr.pipeline.llm`, imports the cache module
(`yizkr.pipeline.llmcache`): `open_cache` opens a cache, and
`cached_client` wraps a client. A unit test parses every module and fails
on any other way of reaching it: an import in any form, a call of an
importing function (`importlib.import_module`, `__import__`, an alias of
one) whose name resolves to it, and any string constant other than a
docstring, or attribute, that names it. The orchestrator asks the factory
for its clients and names only the cache folder, the refreshed roles and
the epochs; the extractor, the critic and the transcriber call whatever
client they are given, and ask for rejections and attempts through
optional capabilities an engine's own client lacks. So their unit tests
run with scripted clients and no cache, and removing the cache means no
longer wrapping the client.

### 11. What stays of ADR-0025 §2 and #209

- Content-hash freshness (`derivations.json`), extended to the views
  (§1): a derived file is current while its inputs hash the same, the
  transcription and the batch by their canonical content, so an identical
  recompute invalidates nothing.
- The run directory: `run.json` (the cache named by its absolute path),
  `events.jsonl`, `alerts.jsonl` and `control.jsonl`. Stage events are
  `started`, `done` (with attention reasons), `reused`, `failed` and
  `held`, for the stages `transcription`, `extraction`, `critique` and
  `gedcom`; a control request can stop each of them. The engine for a
  stage starts inside it, so an engine that cannot start fails the stage,
  with its events.
- The changed-source set-aside, and atomic writes.
- Stages before write still read nothing from the wiki: their inputs are
  the corpus, configuration and the answers of the engine or the cache
  (ADR-0025 §2).
- The write side's key (#200) takes the batches' canonical content hashes
  where ADR-0025 §2 named the batch keys.
- The attribution the run report (#124) and the diff (#198) give a
  recompute: every entry keeps its key's components, so a call made in
  one run can be compared with the baseline's, component by component
  (the prompt, a file, the model, the config hash, the attempt, the
  epoch), where ADR-0025 named the key component of a stage.

### 12. What goes

- The stage store and its records, keys, failure logs and adoptions.
- The stage-version registry (`stageversions.py`, `stage_versions.json`),
  its drift test, and `yizkr stages check|lock`.
- `--store` and `--recompute`.
- The hold of a document directory without `derivations.json`, its
  adoption by modification times, and `--rederive-unrecorded`. Such a
  directory, and one whose files have lost their `state.json`, is set
  aside whole and started afresh, as a changed source is (ADR-0010), with
  nothing in it deleted. Modification times are read nowhere.
- The per-call check for the PDF renderer: a run checks the host once,
  before any document (#218).

## Consequences

- **Views persist, and the cache is removable** (§1). A run changes a
  document's views only when their inputs change or `--refresh` asks; a
  change to a prompt, a model or the code reaches existing views only
  through a refresh or a fresh state directory. So the scratch driver
  (#194) must start every scratch run from fresh state directories, and
  it is there that a code change shows in a scratch diff. The one
  exception is the transcription check: a change to it reaches every
  stored vision and text-file transcription on the next run, and makes
  again the ones it fails, with their batches.
- **One-off invalidation.** The first run on a state directory made
  before this change finds no record for its views, and files derived
  from the batch recorded by the view's bytes where they are now recorded
  by its canonical content. So every view is made again (the old store is
  not read, so its calls go to the engine), every file derived after the
  batch is stale and made again, and a reviewed CSV is moved aside
  wherever what it reviews comes out different. This happens once. Only
  development-stage directories exist, and a reset is planned, so there
  is no compatibility shim.
- **Refused manifests replay** (§5). A document whose manifest fell back
  keeps falling back from the cache until `--refresh extract` or an epoch
  asks again, so a scratch run's fallback counts stay comparable.
- **A claude CLI update changes every key** (§2): the first view made
  again after it asks the engine for every call, and adoptions made
  before it are orphaned (§9).
- A code change to a stage costs no model call unless it changes what a
  call sends, and needs no version bump or neutral entry. The scratch diff
  (ADR-0025 §4) stays the evidence for a change to a stage before write,
  and reads "its source with a version bump" as a change to its code:
  without a registry, whether a code change alters outputs is what the
  diff shows, and replaying cached responses makes that run cheap.
- Reuse is exact only while each call's inputs reach its key: its text, its
  files and its engine configuration do by construction; what the server
  and the CLI add at run time does not (§2).
- A directory from before `derivations.json`, or whose views lost their
  `state.json`, loses its registration and reviews to a fresh start, kept
  aside for inspection.
- The pre-#225 stage store under `var/stages` is no longer read; the owner
  may delete it.

## Alternatives considered

- **Make every view again in every run, from the cache** (this ADR's first
  draft). Rejected: the pipeline's stability then depends on the cache.
  Without it, a sampled model changes the batch on every run, reviews are
  moved aside, and no document passes a gate.
- **Leave a reused transcription unchecked**, so that a change to the
  check reaches only the transcriptions made after it. Rejected: a failed
  transcription that a stricter check catches would then stay under its
  batch and pass the gates to the write, in every run on its state
  directory. The check is cheap, and only a transcription it fails is made
  again (§1).
- **Fail the document when a cached answer is rejected, and ask the
  engine in the next run.** Rejected: a run with the cache would then end
  differently from one without it, and the first scratch run after a
  stricter check would fail every document it affects instead of
  transcribing it again (§4).
- **Keep one session per conversation, and resume whatever is kept.**
  Rejected: two callers opening the same conversation at once, or a
  replay whose session could not be kept, leave a session made on another
  opening answer than the one the cache serves, and a fork resumed on it
  is stored under a key that names the served answer, in every later run
  (§6).
- **Keep the stage store and key its stages on `config_hash` and the check
  version (#218).** Rejected: it keeps the registry and its judgement, and
  a parser fix still recomputes the stage's calls.
- **Stage keys hashed from the stage's source automatically.** Rejected:
  what code shapes an output is transitive and hard to enumerate, and any
  change to it still recomputes the model calls.
- **Replay a conversation's opening turn for a fork's session, and fork on
  whatever it answers** (the first draft of §6). Rejected: the fork is then
  shaped by an unchecked answer that may differ from the one its prompt
  was built from, and it is reused in later runs. Every chunk-prompt or
  schema change leaves the manifest cached and misses every fork behind
  it, so this is the common case, not a rare one.
- **Key chunk retries by attempt number, and replay them.** Rejected: a
  chunk that failed every attempt would fail again from the cache in every
  run; rejection asks again instead. Keyed attempts suit the manifest and
  the segmentation, whose refusal falls back (§5).
- **Delete rejected entries.** Rejected: they are the evidence of what a
  check or a parser refused.
