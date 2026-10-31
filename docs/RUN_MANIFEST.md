# The run manifest (`run.json`)

Every run directory holds a `run.json`: the run manifest. It says what shaped the run, in enough detail for a later tool to tell two runs apart by their configuration, and it plans the documents the run takes through its stages. `yizkr stages state`, the scratch driver, the live monitor ([docs/MONITOR.md](MONITOR.md)), the call estimate (`yizkr scratch estimate`, docs/runbooks/scratch-run.md) and the run report (`yizkr scratch report`, [docs/RUN_REPORT.md](RUN_REPORT.md)) read it, and so will the scratch diff (#198); `yizkr stages manifest <run dir>` prints it for a person.

The other files of the run directory (`events.jsonl`, `alerts.jsonl`, `control.jsonl`) and the LLM cache are described in [`docs/LLM_CACHE.md`](LLM_CACHE.md). ADR-0025 and ADR-0032 give the reasons for both.

Code: `yizkr.pipeline.rundir` (the file, the settings check, the code version, the never-ingest list's hash, the alias rule), `yizkr.pipeline.orchestrate` (what `yizkr ingest run` records) and `yizkr.pipeline.scratch` (what a scratch run records, docs/runbooks/scratch-run.md).

## What it records, and what it never does

The manifest records settings, hashes and paths:

- the mode (`ingest run`, or `scratch run` for the scratch driver) and the project stage (`development`; keeper mode, #118, records `kept`);
- the engine, and the claude CLI version the pass is pinned to;
- for each role the engine is called in (`transcribe`, `extract`, `critique`): the model the run configures, the effort, the epoch and whether the run refreshes it, and, once the role's engine client has started, what that client applies: its model, its effort, its configuration hash (`config_hash`, `yizkr.pipeline.llm`) and its model identifier;
- the LLM cache's absolute path, or `null` when the run has no cache;
- the corpus folder, and the never-ingest list's file and hash;
- the code version: the package version, the git commit, whether tracked files differ from it, and the SHA-256 of that difference;
- the run's other configuration (language, concurrency, serial mode);
- the planned documents;
- when the run and each of its invocations started and ended, and how each invocation ended.

It never records a prompt, the engine's system prompt, any document content, an entry of the never-ingest list, or the code difference itself; only their hashes, where a hash is useful. The system prompt the engine appends reaches the manifest only through the configuration hash, which covers it among the CLI's other arguments.

`run.json` still lives in the run directory under `var/`, and it names local paths (the corpus folder, the never-ingest list, the cache) and the files of the corpus. It is local-only data, like everything else in `var/` (docs/AGENT_PROTOCOL.md §6). Only aggregates from it may leave the machine.

## When it is written

`run.json` is replaced atomically on every write, so a reader sees it whole or not at all.

1. **When an invocation starts.** Every invocation of the run directory (every `yizkr ingest run` over its state directory, say) merges its settings and plan into the manifest, appends itself to `invocations`, writes the start marker to `control.jsonl`, and emits `run-start`. The run keeps its `run_id` and its first `started_at`.
2. **When a role's engine client starts.** Engine clients start lazily, inside the first stage that calls them, so an engine that cannot start fails that stage (ADR-0032 §11). When one starts, its role's `client` and the engine's `cli_version` are recorded. A role whose views all stand in an invocation starts no client, and its `client` stays `null`.
3. **When the invocation ends.** Its `ended_at` and `outcome` are recorded (below), and so is the run's `ended_at`. Then the event that closes the invocation's events (`run-end`, or `run-interrupted` for Ctrl-C) gives the same `outcome` and `reason`, however the invocation ended, a paused pass included (docs/LLM_CACHE.md).

A model named by an alias is refused before anything is written (below).

## One run, several invocations

The manifest describes the run **as its latest invocation ran it**.

- **A run whose settings may change** (`yizkr ingest run`): each invocation records its own settings over the last one's. ADR-0032 lets a state directory be run again with another model or effort: a view that stands is reused whatever the settings. A role's `client` and the engine's `cli_version` start `null` in each invocation and say what this invocation's engines applied.
- **A run that keeps its settings** (`fixed_settings`: a scratch run, `yizkr scratch run`): an invocation must ask for the settings the run recorded, or it is refused with `RunSettingsConflict` before anything is written or called. The refusal lists each setting that differs, with the recorded value and the one asked for, and says what to do: invoke the run again with its settings, or start a new run in a new run directory (the LLM cache answers every call the change leaves the same). The recorded clients and CLI version are kept across invocations. A client that runs another claude CLI version than the one recorded, or applies another model, effort or configuration hash than the one recorded for its role, is refused the same way when it starts, before its first call. The likeliest such refusal is a CLI that updated itself between invocations, as the claude CLI does while other Claude Code sessions share the machine. The refusal then lists `engine.cli_version` first (a client's configuration hash covers the CLI version, so a role's recorded hash differs too) and says what to do: install the recorded version, keep auto-update off (`DISABLE_AUTOUPDATER=1`) and invoke the run again, or start a new run on the new version, none of whose calls the LLM cache answers from this run's, since its keys name the CLI version. A CLI that changes while an invocation runs is the engine's own pin's to catch: it pauses the pass (`CliVersionMismatch`, a `PassPaused`). `RunSettingsConflict`, like a paused pass, is no document's failure: it stops the invocation.

The settings a run that keeps its settings must keep are `mode`, `project_stage`, `engine`, `roles`, `corpus`, `code` and `config`, with these exceptions, which may change between its invocations:

| May change | Why |
| --- | --- |
| `roles.*.refresh`, `config.refresh` | A refresh belongs to its invocation (ADR-0032 §7) |
| `config.chunk_concurrency`, `config.extract_concurrency` | How many calls run at once shapes pacing, not what is made |
| `engine.cli_version`, `roles.*.client` | What the engine applies is checked when a client starts |
| `cache` | The cache only saves engine calls (ADR-0032 §1) |

A `run.json` written before format 1 lacks these settings, so a run that keeps its settings refuses to resume from one.

**A scratch run past its barrier.** A scratch run keeps its code with one exception, so that a corpus stage registered after the run reached its barrier can still run on it. When every document has ended its per-document stages in the run's events, all of them done or failed, or one failed (the stages after a failed one are never dispatched), and the code is the only setting an invocation changes, the invocation goes past the barrier alone: it dispatches no document's stage, and runs the corpus stages over the views as they are. The run's `code` stays the code the documents' stages ran on, and the invocation records its own under `scratch.code`, with `scratch.past_barrier_only`. Such an invocation refuses `--refresh`, which would make views again. With any other setting changed, or a document's stage still to run, a change of code is refused as any other change is: start a new run, whose fresh state directories make every view again with the new code, the LLM cache answering every call the change leaves the same.

## Model ids, never aliases

A run refuses to start when its configuration names a model by an alias: the extraction model (`--extract-model`) or the run's main model (`$YIZKR_LLM_MODEL`, which serves every role without a model of its own). An alias lets the engine choose the model, and neither the manifest nor the LLM cache's keys could then say which model answered. `yizkr ingest run` exits with the reason, naming the setting, the roles it serves and what to configure instead.

A name is an alias when it asks for the latest model (it ends in `-latest`) or names no version at all (the claude CLI's `sonnet`, `opus`, `haiku`, `default` and the like), once a context-window selector such as `[1m]` is set aside. A full model id names its version: `claude-sonnet-5`, `claude-opus-5-5`, `claude-haiku-4-5-20251001`.

Two cases pass the check and stay visible elsewhere:

- **No model at all.** The engine then runs its own default. The run records the role's `model` as `null`, and the LLM cache warns once per role that its keys name no model (docs/LLM_CACHE.md, Keys). A scratch run, whose answers later runs reuse, refuses this too: its preflight asks every role for a model.
- **An undated name of a model that also has dated snapshots.** Its form cannot tell it from a full id.

In either case, the model each call ran on is in its `call` event and its cache entry, as the call's stream reported it (docs/LLM_CACHE.md), and the `model-mismatch` anomaly check compares it with the one the manifest names for the role (docs/ANOMALIES.md): an undated name that a call's stream reports running on a dated snapshot raises it. A role the manifest names no model for has none to compare with: the model its first call ran on, the one the engine resolved, stands for it.

## Format 1

A reader checks `format`. A later format may add fields; a reader that meets fields it does not know ignores them. Fields a reader needs and does not find are absent because the manifest was written by an earlier version of the code: `run.json` files from before format 1 hold only `run_id`, `mode`, `cache`, `config` and `documents`.

| Field | What it holds |
| --- | --- |
| `format` | `1` |
| `run_id` | Set at the first invocation and kept: `<UTC time>-<6 hex digits>`. A scratch run's is its directory's name, and its hex digits give the microsecond it started; when the clock reads at or before the latest run id already in the runs directory (a clock set back), the id is the first after that one instead, so that runs always sort as they were started. A run refuses to start, naming the entry, when an entry in the runs directory is named like a run id that no id sorts after (the last microsecond of the year 9999, or past it) |
| `mode` | What wrote the manifest: `ingest run`, or `scratch run` |
| `project_stage` | `development`, or `kept` from keeper mode on (ADR-0024 §7, ADR-0025 §6) |
| `engine` | `{name, cli_version}`: `claude-code` or `anthropic`; the claude CLI version the pass is pinned to, once a claude-code client started in the invocation (`null` otherwise, and for the API engine) |
| `roles` | Per role (`transcribe`, `extract`, `critique`), see below |
| `cache` | The LLM cache's absolute path; `null` when the run has none (`--no-cache`) |
| `corpus` | `{folder, exclusions}`: the corpus folder's absolute path; the never-ingest list as `{file, sha256}`, see below |
| `code` | `{version, commit, dirty, diff_sha256}`, see below |
| `config` | The run's configuration: `engine`, `language`, `fallback_language`, `extract_effort`, `review_effort`, `extract_model`, `refresh` (the refreshed roles), `epochs` (role to epoch, where not 0), `artifact_type`, `serial`, `chunk_concurrency`, `extract_concurrency`; a scratch run's has no `artifact_type`, `serial` or `extract_concurrency`, which it does not use |
| `pacing` | A scratch run's: the engine's pacing, as the latest invocation ran it (not a setting it keeps): `reserve`, the share of each usage window left to development sessions (`$YIZKR_CLAUDE_RESERVE`), and `max_wait`, the longest a call waits for the subscription, in seconds (`$YIZKR_CLAUDE_MAX_WAIT`); `null` for the API engine |
| `scratch` | A scratch run's latest invocation (not a setting it keeps): `until` (the stage it was asked to stop after, or `null`), `canaries` (the SHA-1s of the documents that went first, and of the first excerpt of the invocation, added as it is found), `corpus_stages` (the names of those registered, which the live monitor lists with `until` as the corpus stages the invocation is to run), `past_barrier_only` and, when it is true, `code` (the invocation's own code, see above), and `complete` (whether it went through every stage there is, which `yizkr scratch run` reads to resume the latest run or start a new one) |
| `documents` | One entry per planned document, `{sha1, file, kind, stages}`: its media SHA-1, its file name, its slice (`text`, `media`, `pdf`, `presentation`, `gedcom`, `excerpt`) and the stages planned for it. An excerpt, an image that a PDF places on any of its pages (not decoration, nor one covering most of its page) or a deck paints on a slide (not decoration), read as a document of its own (ADR-0008), is added as its container's transcription finds it, its file named for its SHA-1; an invocation plans the excerpts its directories hold from its start. A document whose source or `state.json` cannot be read is not planned; a `document-failed` event names it (docs/LLM_CACHE.md) |
| `started_at` | When the first invocation started (UTC, ISO 8601, to the second) |
| `ended_at` | When the latest invocation ended; `null` while it runs, or when it was killed |
| `invocations` | One entry per invocation, in order, see below |

**`roles.<role>`:**

| Field | What it holds |
| --- | --- |
| `model` | The model the run configures for the role, the key's `model` component in docs/LLM_CACHE.md: the role's own (`--extract-model` for `extract`), else `$YIZKR_LLM_MODEL`; `null` when neither names one and the engine picks its default |
| `effort` | The reasoning effort the run asks for (`--extract-effort` for `transcribe` and `extract`, `--review-effort` for `critique`). The API engine takes none; `client.effort` says what was applied |
| `epoch` | The role's epoch in the LLM cache (`--epoch`), 0 by default |
| `refresh` | Whether the invocation makes the role's views again (`--refresh`) |
| `client` | What the role's engine client applies, once it started in the invocation, else `null`: `model` (the model its calls name; for the API engine without a configured model, its default), `effort`, `config_hash` and `model_identifier` (for claude-code, `claude-code/<CLI version>:<model>`) |

**`corpus.exclusions`:** `file` is the list's absolute path (`null` for a list given in code). `sha256` is the SHA-256 of the canonical JSON of `{"sha1s": [...], "patterns": [...]}`, each sorted, with patterns case-folded as the list matches them. Two lists that exclude the same hash the same whatever their comments, blank lines and order. The entries are not recorded. `corpus.exclusions` is `null` for a run without a list, which `ingest run` refuses.

**`code`:** `version` is the package version. `commit` is `git rev-parse HEAD` of the checkout the package runs from. `dirty` says whether tracked files differ from that commit (`git diff HEAD`); untracked files do not count. `diff_sha256` is the SHA-256 of that difference, so two runs of one commit with different edits tell apart, and it is `null` when the tree is clean. Outside a git checkout, or without git, `commit` and `dirty` are `null`. The code version is measured once per process.

**`invocations[]`:**

| Field | What it holds |
| --- | --- |
| `invocation` | The invocation's id, which every event it emits carries |
| `started_at`, `ended_at` | Its start and end; `ended_at` stays `null` while it runs, or if it was killed |
| `outcome` | `finished` (it went through its documents, whatever their statuses, and no request for the whole run held any of them), `stopped` (explicit programmatic cancellation, or a control request for the whole run stopped it: before a document, or by holding a stage of one, the last document's included; a request for one stage or kind holds documents at it, and the invocation still finishes; in a scratch run, whose barrier waits for every document, a request that aborts a stage or kind stops the invocation too, once nothing else is left to do), `interrupted` (Ctrl-C), `paused` (the pass paused, `PassPaused`: the claude CLI changed while the invocation ran, say, or a subscription limit would hold a call longer than the engine waits, `UsageLimitReached`, whose call event gives the time the run can go on; docs/LLM_CACHE.md, Subscription limits), `refused` (`RunSettingsConflict`: other settings than the run recorded, a claude CLI updated since it recorded its version included) or `failed` (another error escaped it, or a scratch run's corpus stage failed); `null` while it runs |
| `reason` | When it did not finish: the control request that stopped it (`run aborted`, `run paused`; in a scratch run, the slices whose aborts held it, such as `kind:media aborted`), or the name of the exception, never its message, which may quote a document |

## An example

With placeholder paths and hashes:

```json
{
  "format": 1,
  "run_id": "20260927T101500Z-1a2b3c",
  "mode": "ingest run",
  "project_stage": "development",
  "engine": {"name": "claude-code", "cli_version": "2.1.0"},
  "roles": {
    "transcribe": {"model": "claude-opus-5-5", "effort": "low", "epoch": 0,
                   "refresh": false, "client": null},
    "extract": {"model": "claude-sonnet-5", "effort": "low", "epoch": 0,
                "refresh": false,
                "client": {"model": "claude-sonnet-5", "effort": "low",
                           "config_hash": "<sha256>",
                           "model_identifier": "claude-code/2.1.0:claude-sonnet-5"}},
    "critique": {"model": "claude-opus-5-5", "effort": "high", "epoch": 0,
                 "refresh": false,
                 "client": {"model": "claude-opus-5-5", "effort": "high",
                            "config_hash": "<sha256>",
                            "model_identifier": "claude-code/2.1.0:claude-opus-5-5"}}
  },
  "cache": "/home/<user>/yizkr/var/llm-cache",
  "corpus": {"folder": "/home/<user>/corpus",
             "exclusions": {"file": "/home/<user>/never-ingest.txt",
                            "sha256": "<sha256>"}},
  "code": {"version": "0.1.0", "commit": "<40 hex digits>", "dirty": false,
           "diff_sha256": null},
  "config": {"engine": "claude-code", "language": "auto", "...": "..."},
  "documents": [{"sha1": "<40 hex digits>", "file": "letter.txt",
                 "kind": "text",
                 "stages": ["transcription", "extraction", "critique"]}],
  "started_at": "2026-09-27T10:15:00+00:00",
  "ended_at": "2026-09-27T10:42:10+00:00",
  "invocations": [
    {"invocation": "<12 hex digits>", "started_at": "2026-09-27T10:15:00+00:00",
     "ended_at": "2026-09-27T10:42:10+00:00", "outcome": "finished"}
  ]
}
```

`yizkr stages manifest <run dir>` prints the same run one setting per line, showing each role's client where it started and the configuration otherwise.

## Not here yet

- **The write order of kept runs** (ADR-0019 §1). Serial mode computes its order afresh in each invocation. Persisting it, with an explicit re-sequence as the only way to change it, belongs to keeper mode (#118).
- **The engine's pacing in `ingest run`.** The share of each usage window left to development sessions and the longest a call waits for the subscription (`$YIZKR_CLAUDE_RESERVE`, `$YIZKR_CLAUDE_MAX_WAIT`; docs/LLM_CACHE.md) decide only when calls are made, never what they send; a scratch run records them (`pacing`), and `ingest run` does not yet. A pause they cause is the invocation's `paused` outcome, and its call event says why and when the run can go on (a scratch run waits for that time and goes on by itself, docs/runbooks/scratch-run.md) — except a report that a call ran on usage credits, which stops dispatch only once that report arrives: the reported call, and any others already in flight when it lands, may already have used credits, and only turning extra usage off on the account guarantees none does. Its call event gives no `resumes_at` at all: the subscription does not say when the pause clears, so the operator confirms that extra usage is off on the account before invoking the run again, rather than waiting for a time nothing will report.
