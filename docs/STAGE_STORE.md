# The stage store and the run directory

Development runs the whole corpus through the pipeline again and again
(ADR-0025). The stage store makes that affordable: every stage output
before the write is stored under a key built from everything that shaped
it, so a run recomputes only what a change reaches and reuses the rest.
The run directory beside it records what each run did, for the driver
(#194), the live monitor (#195), the anomaly checks (#196), pacing (#101)
and the run report (#124).

Both are runtime state under `var/` and are **local-only data**
(docs/AGENT_PROTOCOL.md §6): they hold transcriptions, claims and raw
model output of the corpus. Nothing from them is copied into the
repository, GitHub or a cloud session.

Code: `yizkr.pipeline.stages` (store, keys, stage runner),
`yizkr.pipeline.stageversions` (version registry) and
`yizkr.pipeline.rundir` (run directory and run state).

## Keys

A stage key is the SHA-256 of the canonical JSON of its components:

| Component | What it holds |
|---|---|
| `inputs` | The input content: the media SHA-1 for a first stage; after that, the canonical content hash of each upstream output |
| `stage`, `version` | The stage name and its declared version |
| `method`, `method_version` | For a routed stage, the method this document was routed to and that method's version |
| `prompts` | SHA-256 of each prompt template the stage renders (and of the claim schema a chunk prompt embeds) |
| `manifest` | SHA-256 of the model manifest (`yizkr/model/manifest.yaml`) behind the prompts' vocabulary and the validation |
| `model` | Engine, model id and effort, from configuration (`$YIZKR_LLM_MODEL` when no model is set per phase). With neither, the model is keyed as `engine default` (see *Unpinned models* below) |
| `params` | Anything else that changes the output: the language settings, a chunk's assigned entities |
| `depends` | Versions of registered helper stages a stage relies on without an input from them |

A content hash is taken over the output with its **volatile fields**
left out (run timestamps, the software commit, engine build strings).
So an identical recompute, say under a new method version, yields the
same hash, and nothing downstream is recomputed (early cutoff).

**Nothing is keyed on QIDs, file modification times or timestamps.**
While prompts still cite the artifact QID (until #127), the QID is a
*binding*, not an input: a record notes the QID it was computed under,
content hashes replace it with a placeholder, and a reused output is
rebound to the document's current QID. A reset that re-registers every
artifact therefore reuses every output.

Context mode (whether a chunk call forked the manifest conversation or
carried the document inline) is how the document reaches the model, not
what is asked, so it is recorded on the record but not keyed.

### Unpinned models

The `model` component comes from configuration alone, so a run that
reuses every output never starts the engine. When neither the phase's
own setting (`--extract-model` for extraction, which `ingest run` sets
by default) nor `$YIZKR_LLM_MODEL` names a model, the engine runs a
default of its own choosing: the anthropic engine's built-in default,
or whatever model the claude-code CLI picks. The key cannot see that
default, so it holds the literal `engine default`. **A change to an
engine's default model is not tracked**: the keys stay the same, and
outputs that the previous default made are reused. Until #133 pins the
model and the CLI, a key holds the full model id only when one is
configured. `yizkr ingest run` warns, naming each unpinned role
(transcribe, extract, critique), whenever a document's stages call the
engine. Set `$YIZKR_LLM_MODEL` for any run whose outputs are to be
reused.

### Routes

The transcription stage is routed per document by a deterministic step,
`route_transcription`: `vision` for scans and images, `text-file` for
`.txt`/`.md`, `slide-text` for decks, and `correction` when a curator's
corrected transcription exists. Each method has its own version in the
registry, and a key holds only the version of its own method, so a new
or changed method recomputes only the documents routed to it. The route
is per document for now; per-page routing (a text layer for some pages,
vision for others) fits the same scheme with the page as the item.

## Stages

| Stage | Scope | Inputs | Output | Raw |
|---|---|---|---|---|
| `transcription` | document | media SHA-1 (+ correction hash) | text, language, language resolution | vision responses |
| `entity_manifest` | document | transcription | entity manifest, or the recorded line-range fallback | every manifest and segmentation response |
| `chunk` (one per chunk) | document | transcription, entity manifest; params: assigned members | claims and gaps | every attempt's response |
| `batch` | document | transcription, entity manifest, each chunk's content hash | the merged, validated claim batch; extras: `coverage.json`, `rejects.json`, `gaps.ndjson` | none (no call) |
| `critique` | document | batch | findings | the critic's response |
| `gedcom` | document | media SHA-1 | the adapter's claim batch | none |

`chunk_parse` is registered but not stored: it versions the chunk
parser. A stored chunk whose parse version is out of date is re-parsed
from its raw response; the call is never repeated. A chunk that failed
to parse keeps its response in the failure log, and the next run
re-parses that before calling the model, so a parser fix costs no model
call (ADR-0006).

Corpus stages (the identity map, #105; flags, #112) use the `corpus`
scope, which the store supports already. The transcription check (#175)
is to be registered as a stage of its own, with its own version, so that
landing it does not bump the transcription version.

## Layout

```
var/stages/
  doc/<media sha1>/<stage>/<key>/record.json    key, components, output hash,
                                                attention, provenance
                                /output.json    the parsed output
                                /raw/0001.txt   every model response, in order
                                /extras/<name>  advisory side outputs
  doc/<media sha1>/<stage>/failures.jsonl       append-only failure log
  doc/<media sha1>/<stage>/adopted/<key>.json   recorded equivalences
  corpus/<stage>/...                            corpus-level stages
```

`record.json` carries `format`, `scope`, `stage`, `key`, `components`,
`output_sha256` (the canonical content hash downstream keys use), `raw`,
`extras`, `attention`, `created_at` and `meta` (provenance: the engine's
model identifier, session id, context mode, the artifact QID binding, the
chunk item and parse version).

**Atomic writes.** A record is written into a staging directory and
appears only when that directory is renamed to its key, with
`record.json` written last. An interrupted run leaves at most a
`.staging-*` directory, which is never read as a record and may be
deleted. A forced recompute replaces a record in one rename.

**Failures.** A failed computation stores nothing under its key and
appends `{key, at, reason, item?, attempts?, raw?}` to the stage's
`failures.jsonl`. The next run retries the key; the log is never
rewritten, so the reason outlives the success that follows.

**Attention.** A stored output can carry attention reasons: a manifest
that fell back to line-range segmentation or line windows, a batch with
no claims. It is reused like any other, and its run state is *needs
attention*. A fallback is stored only when the engine answered; when
every call of a tier raised, the stage fails instead, so an engine
outage is retried rather than reused.

## Reuse

An output is reused only when:

- its recorded key matches the current key exactly (**key match**); or
- a recorded equivalence in `adopted/<current key>.json` maps the current
  key to a stored record (**adopted**). Imported transcriptions (#104)
  are stored under their own backfilled key and adopted this way. The
  equivalence names one exact current key, so a version bump drops it.

Every reuse is an event with its reason. `yizkr ingest run --recompute
<stage>` (repeatable) recomputes a stage once in a run even where its key
matches.

## The version registry

`yizkr.pipeline.stageversions.STAGES` declares each stage's version, each
method's version, and the code that defines their behaviour (`sources`).
`src/yizkr/pipeline/stage_versions.json` records a fingerprint of those
sources for the declared version. Fingerprints ignore comments,
docstrings and blank lines; prompt templates, the manifest and the claim
schema are key components of their own and are not sources.

A unit test (`tests/unit/test_stage_store.py`) fails when a listed
source changes without a version bump or a recorded functionally neutral
entry. `yizkr stages check` names the drift. Then:

- the change alters output: bump the version in `STAGES` and run
  `uv run yizkr stages lock <stage>` (or `<stage>:<method>`);
- it does not (a rename, a refactor): run
  `uv run yizkr stages lock <stage> --neutral "<why>"`.

A PR that bumps a version or changes a key component of a stage before
write says so, and carries the matching `stage:*` label (#64).

## The run directory

`yizkr ingest run` uses its state directory (`var/runs/<folder>/`) as its
run directory; the scratch driver gives each run its own. Every file but
`run.json` is append-only JSON lines. Readers need no lock and skip a
torn last line, so an interrupted writer never corrupts a log.

| File | Written by | Read by |
|---|---|---|
| `run.json` | the driver at start (merged, replaced atomically) | everything |
| `events.jsonl` | the stage runner, the driver, the anomaly checks, the engine wrapper | the monitor, the report, run state |
| `alerts.jsonl` | the anomaly checks | the monitor, the report |
| `control.jsonl` | the monitor, the anomaly checks, pacing, `yizkr scratch abort`; the driver's start marker | the driver |

### `run.json`

The run manifest. This module writes `run_id`, `mode`, `store` (the
stage store's path), `config`, and `documents`: one entry per planned
document, `{sha1, file, kind, stages}`, where `kind` is the document's
slice (`text`, `media`, `presentation`, `gedcom`) and `stages` the stages
planned for it. The full run manifest (#96) adds the rest: stage (development or
kept), engine and CLI version, per-stage model ids, prompt and manifest
hashes, stage and method versions, commit, exclusion-list hash, start and
end.

### `events.jsonl`

Every line has `at` (UTC), `event` and `invocation` (one per process
that works on the run). Stage events add `subject` (the document's media
SHA-1, or `corpus`), `kind`, `stage`, `key`, `method` (routed stages) and
`item` (a chunk id):

| Event | Meaning | Extra fields |
|---|---|---|
| `run-start` | an invocation began | `run_id` |
| `started` | a stage computation was dispatched | `changed`: the key components that differ from the stage's latest record |
| `stored` | a computed output was stored | `reason` (`computed`, `recomputed`), `attention` |
| `reused` | an output was reused | `reason` (`key match`, `adopted`), `record` (for adopted), `attention` |
| `failed` | a computation failed; nothing was stored | `reason` |
| `held` | the control file stopped dispatch | `reason` |
| `alert` | an anomaly alert was appended | `check`, `status` |
| `run-end` | the invocation finished | `statuses` |
| `run-interrupted` | the invocation was interrupted (Ctrl-C) | |

Call events from the engine wrapper (#98, #101) join this log with
`event: call`.

### `alerts.jsonl`

One alert per line: `at`, `stage`, `check`, `subject` (a SHA-1 prefix),
`value` (the measured value), and `status`: `new` on an output computed
in this run, `carried` on a reused one. Each alert also appends an
`alert` event.

### `control.jsonl`

Requests `{at, action, scope, by, reason}`: `action` is `pause`,
`resume` or `abort`; `scope` is `run`, `stage:<name>` or `kind:<name>`.
The driver folds them in order: a pause holds until a resume, and an
abort holds for the rest of the invocation. A new invocation writes
`{action: "start"}`, after which earlier requests no longer apply: that
is how an aborted run resumes. A stopped scope stops dispatch only.
Stored outputs are still reused, in-flight work finishes, and nothing
completed is lost. `ingest run` honours run and kind requests between
documents and stage requests before every computation; a held document
is `paused` and resumes on the next run.

## Run state

Run state per document and stage is derived from the run manifest, the
events and the store, never from modification times or a per-document
status file (`rundir.run_state`, `yizkr stages state <run dir>`):

- every stage the manifest plans starts **pending**;
- the latest event for a (document, stage, item) sets its state:
  **reused** with its reason, **done**, **needs attention** with its
  reasons, **failed** with its reason, or running while in flight;
- the store has the last word: an output that is no longer stored is
  pending again, and a failure or an interrupted computation whose key
  has since been stored is done.

## `yizkr ingest run` on the store

`ingest run` reads and writes every stage before the review gates
through the store (`--store`, default `var/stages`). The document
directory keeps plain-file views for reading and review (`transcription.json`,
`batch.json`, `critique.json`) and the files the later steps derive from
them. Their dependencies are recorded by content hash in
`derivations.json`:

- a batch whose canonical content changed invalidates every derived file
  and moves the reviewed CSVs aside; a rebuilt view with the same content
  invalidates nothing;
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

**Directories from before these records.** A document directory with a
`state.json` but no `derivations.json` was made under the old
modification-time rule and holds no content hashes. `ingest run` adopts
it once, before anything in it is rewritten, by applying that rule one
last time to the directory as it was left:

- a derived file is recorded as current only when no input it was made
  from is newer than it (a tie counts as not newer). Any other derived
  file is derived again, and from then on content hashes decide what
  follows it;
- a reviewed CSV is bound only when the CSV it reviews exists and is not
  newer than it. Otherwise (the CSV is missing, or was re-exported after
  the review was saved) the review decided on rows that have since been
  replaced: it is moved aside as `*.reviewed.superseded-<n>.csv` and
  consents to nothing. Its gate waits for a review of the current rows,
  and nothing is written from the old one.

After adoption, modification times are never read again.

To correct a transcription, save the corrected text as
`<doc>/transcription.corrected.txt`. The document is routed to the
`correction` method, and exactly what depends on the text is recomputed.
Delete the file to return to the original, which is still stored.

The per-document path goes when the scratch driver has run the corpus
end to end (#119). Until #128 and #103 land, it still registers the
artifact in the wiki before the stages run; the stages themselves read
and write no wiki.
