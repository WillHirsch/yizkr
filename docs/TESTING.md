# Testing

Choose evidence for the behavior and boundary being changed. The [current contract](CONTRACT.md) defines the outcomes; [AGENT_PROTOCOL.md](AGENT_PROTOCOL.md) covers review, permissions and who can run checks involving private data. Each tool's `--help` describes its command options.

## Verification by risk

Use focused checks while implementing and CI's broad offline result for the revision under review. Do not repeat the full suite merely because a push or fix round occurred. Broaden or repeat checks when changed code, a failure or an unresolved risk warrants it.

| Change | Normal evidence |
| --- | --- |
| Documentation or mechanical edits | Relevant formatting, links and diagrams; review changed commands. Executable prompts and queries are behavior inputs even when stored in Markdown. |
| Internal refactor | Affected existing behavioral tests and callers, plus the broad offline result. Add cases for meaningful coverage gaps; an equivalent implementation does not itself require new tests. |
| Changed parser or schema semantics | Focused fictional semantic and rejected-input cases for the changed behavior, plus affected callers. Fixtures need not preserve known defects. |
| Material prompt, model or inference change | Schema/wiring checks and observed outputs from a bounded representative sample or suitable corpus evidence, with configuration recorded. Purely mechanical edits do not automatically require fresh model calls. |
| Writer, identity, migration or query boundary | Repeatable fictional checks against the real affected writer/query engine; verify returned evidence and relevant repeated or interrupted writes. |
| Privacy, exclusions or review gates | Positive and negative boundary cases inspecting actual outbound model inputs, persisted statements or emitted exports, as applicable. |
| Cancellation, locking or concurrency | Fake clocks for scheduling logic; controlled real threads, processes or locks for operating-system behavior. |
| Performance-sensitive work | Focused measurements against a relevant budget or comparable baseline. A generous hang guard is not a performance test. |

Tests establish intended behavior: provenance, uncertainty, identity, review, isolation and recovery. Prompt literals, exact query syntax, helper structure and historical prose are not substitutes for semantic results. Replace obsolete assertions with the affected implementation rather than preserving defects or requiring a repository-wide test rewrite first.

A small semantic acceptance set may allow multiple valid interpretations or require abstention. Deterministic acceptance checks are sufficient for #709; real-model evaluation is a separate bounded activity. A canned engine establishes orchestration, not inference quality. Whole-corpus evidence is required when cross-document interactions or material uncertainty remain beyond bounded checks; record the reason and affected stages. A prompt, model or pre-write code edit alone does not require a corpus run. The procedure and local-only boundary are in [AGENT_PROTOCOL.md §5](AGENT_PROTOCOL.md#5-integration-and-corpus-evidence).

## Current test layers and limits

**Offline unit tests.** `tests/unit` is the default for `uv run pytest -q` or `make test`; `pyproject.toml` excludes the `live` marker unless requested. CI runs lint and the offline suite on pull-request pushes and main commits. It currently installs all optional extras and runs `pytest -n auto --dist loadfile`; it does not run the full Wikibase stack or real models.

The fixtures in `tests/unit/conftest.py` isolate host settings, model login and instance records. Tests provide their own configuration. Most filesystem sync calls are replaced; use `@pytest.mark.real_fsync` when actual sync behavior matters. Anomaly timers are recorded rather than started. A test that calls `monkeypatch.undo()` removes these function-scoped protections too; use `monkeypatch.context()` for a temporary patch.

The network guard rejects non-loopback lookups and connections, removes proxy variables and fails the test even if code catches the refusal. It is not a complete process sandbox: UDP sends, child processes and earlier-scoped fixtures are not covered. Tests and subprocesses must use fictional inputs and explicit temporary paths. Unit tests must not reach archive credentials or runtime state.

**Offline pipeline coverage.** `tests/unit/test_fixture_corpus.py` exercises per-document stages, the barrier, clustering, response reuse, reports and fixture identity traps with a canned engine behind the real LLM cache. The autouse `_no_write_side` fixture removes reset, registration, writing, places and site from the driver's corpus stages unless a test uses `write_side`. Separate write-side unit tests use an in-memory instance and stand-ins for reset, locking or site generation. These checks do not demonstrate a complete real-stack ingestion, query result or model interpretation. Existing fixture traps and strict expected failures need review when their subsystem changes.

**Live integration.** Tests under `tests/integration`, marked `live`, exercise the configured stack or a real model. Their current availability checks can skip tests. The separate, required fictional integration lane is pending #722; see below. Query-string unit assertions do not establish the rows returned by an actual engine.

**Real-model checks.** `tests/integration/test_live_chunked.py` exercises extraction and conversation resumption through the claude CLI and response cache. It needs a logged-in CLI and skips if unavailable. `$YIZKR_LIVE_CACHE` selects its cache; the default is `var/live-cache`. It does not acquire the stack lock. These checks do not establish broad extraction quality across the corpus, and a reused response establishes only the request/configuration it matches.

## Focused and broad runs

```sh
uv run pytest -q tests/unit/test_names.py
uv run pytest -q tests/unit/test_names.py::test_name
uv run pytest -q tests/unit -k 'name_expression'
uv run pytest -q --lf
```

Replace the example test name or expression with an actual target. Use `uv run pytest -q` when a broad local run resolves a risk or CI evidence is unavailable. Otherwise rely on CI's broad result for the reviewed revision. `make lint` runs Ruff and the Markdown formatting check; use the relevant checks on changed files while working.

CI uses `uv run pytest -n auto --dist loadfile`, keeping each file in one worker. Local runs are serial unless `-n` is given. Use parallelism when machine capacity permits; concurrent test-heavy sessions can make every run slower. Real-stack tests share a lock and are not an xdist workload.

For concurrent behavior, coordinate with events, barriers or pipes rather than timing sleeps. Keep bounded hang guards so failed synchronization terminates. The existing `test_no_real_time.py` checker enforces fake-clock conventions, an explicit allowlist and module-level `HANG_GUARD` values of 30–600 seconds. This is an implementation constraint of the current suite, not evidence of performance. In particular, the transcription backtracking check catches catastrophic hangs but can miss a several-fold slowdown; meaningful performance changes need a suitable measurement.

## Live integration and the transition

The target for real writer/query verification is a disposable Wikibase/WDQS instance populated exclusively from repository fictional fixtures. #722 must provide separate volumes, explicit connections, instance-state records and locks, without archive credentials, mounts or inherited connection defaults. Its required invocation must fail if the intended checks cannot run. It may run centrally or in CI under the [fictional-instance exception](AGENT_PROTOCOL.md#6-the-data-boundary). This lane must be runnable and demonstrated before #159 retains the currently shared instance; documentation of this policy does not implement the lane.

Until then, a centrally coordinated local session may use an explicitly disposable existing target. Corpus-derived graphs remain local-only even when a test adds fictional entities. Test runs, mutants and development resets must never mutate retained data. A boundary change waits when necessary evidence cannot be obtained; unrelated work need not wait.

The existing command is:

```sh
make test-live
# Equivalent, or select an individual relevant module:
uv run pytest -m live tests/integration -v
```

These commands use the configured stack; they do not create or prove an isolated target. Check the target before running them. `tests/integration/conftest.py` skips for missing configuration or an unreachable stack, so a zero exit status alone is insufficient: inspect whether the intended tests ran. Record the commit, relevant tests and result as integration evidence.

Current sessions acquire the live-stack lock and clear the instance-contents key at entry and exit, so a scratch run cannot mistake fixture writes for its own contents. Fixtures mark their own entities, and repeated writes within a session check reuse. Global graph invariants still inspect other sessions' data; an invalid fixture or mutant can contaminate later runs despite unique labels. Recovery is a supported reset of the disposable target, not a reset of retained data.

`test_live_scratch_reset.py` and `test_live_scratch_write.py` require `YIZKR_LIVE_RESET=1`, run in separate sessions from other modules, and refuse retained instances. A failed reset stops the session. Keep those safeguards; the general live suite does not provide equivalent isolation merely because these modules do.

Whole-graph readers reject unsupported dates such as BCE or non-Gregorian dates. A fixture that violates a global supported-input constraint can therefore prevent unrelated queries or exports from running. Check refusal without leaving invalid shared state, or use a disposable target whose reset is part of the test's controlled lifecycle.

## Optional mutation testing

A reproduced original failure, a convincing negative control or a targeted mutant can show that an assertion detects a consequential regression. Mutation testing is optional; there is no mandatory spec or table for each edited test. A mutant failing during setup does not prove that the intended assertion detected the fault. Inspect the failure and explain the behavior it establishes.

`scripts/mutate.py` applies exact text edits, runs selected tests and restores the source. Use a worktree-specific environment so pytest imports the code being mutated:

```sh
uv run python scripts/mutate.py SPEC
uv run python scripts/mutate.py --help
```

A TOML spec has one table per mutation; replace these illustrative paths and texts with the real target:

```toml
[[mutation]]
name = "blank names are kept"
file = "src/yizkr/example.py"
old = 'if not name.strip():'
new = 'if False:'
tests = ["tests/unit/test_example.py::test_blank_names_are_dropped"]
# count = 2     # replace this many occurrences instead of one
# live = true   # run selected live tests on an authorized disposable target
```

Commit the target file first: the harness refuses dirty, untracked or symlink targets and ambiguous replacements. Nothing else should edit tracked files while it runs. It checks for stray checkout writes, restores the mutated file's bytes/mode/mtime, invalidates bytecode and uses process groups to clean up children. Tests write under temporary paths and tear down anything they detach from those groups.

The baseline runs once per distinct test list and must pass with at least one test passing rather than skipping. Collection or syntax errors are not valid kills. Other failures can be reported as kills, so inspect their relevance. The harness prints a table and optionally JSON; share the spec and relevant result when using them as evidence. Specs need not be checked into the repository.

A `live = true` mutation requires a configured stack and holds the live-stack lock across baseline and mutant runs. Arrange it centrally against an explicitly disposable target. An uncatchable termination such as SIGKILL can leave the mutated file and child run in place; do not impose a tool timeout that prevents the harness's own cleanup. No mutation may target retained data.

## The query-engine sandbox

`scripts/synthetic_graph.py` writes a deterministic fictional graph in the query service's Wikibase representation and a vocabulary map. `scripts/wdqs_sandbox.py` loads it into a disposable query engine. This supports actual row checks and query comparisons without using the archive. It does not test the real writer or a complete ingestion.

With `DIR` replaced by a scratch directory outside the repository:

```sh
uv run python scripts/synthetic_graph.py --persons 250 --seed 7 \
    --out DIR/g.ttl
make wdqs-sandbox-up
uv run python scripts/wdqs_sandbox.py load DIR/g.ttl
uv run python scripts/wdqs_sandbox.py query \
    src/yizkr/queries/disagreement-report.rq --repeat 3 --max-rows 20
make wdqs-sandbox-down
```

The graph covers source-attributed dates, precision and ranges, contradictions, containment, residences, spouses, ranks and redirects. The same arguments produce the same bytes on a given Python version. Labels are fictional neutral placeholders.

The sandbox container has no network, ports or volumes and defaults to 768 MB and one CPU. It has its own per-checkout state and lock, and never takes the archive's live-stack lock. `query` renders templates using the loaded vocabulary map; `--raw` accepts a finished query. Query and load timeouts bound individual requests, not an entire repeated command.

Cancellation stops the command's own query. An interrupted load can wait up to 20 seconds for the engine to finish or abort its write. Cleanup ignores further interrupts until complete. Compare variants within a session with repeats; sandbox timings do not predict deployed performance. Run `down` when finished to release its memory.

## The live-stack lock

The current stack harness takes an exclusive flock at `$YIZKR_LIVE_LOCK`, default `~/.cache/yizkr/live-stack.lock`, from the first stack fixture until session end. Unit runs and model-only tests do not take it. The path must be absolute (`~` expands); the default is per user, so worktrees share it. Configure a distinct lock for a separate instance, and ensure all users of one instance agree on its lock.

A waiting session reports the holder and fails setup after `$YIZKR_LIVE_LOCK_TIMEOUT` (default 7200 seconds). Never delete a held lock file: replacing it would allow a second independent lock. The kernel releases the flock when all holders close it; a detached or surviving child can keep it held after the original process dies.

The mutation harness can pass its hold through `$YIZKR_LIVE_LOCK_FD`; the fixture validates the descriptor and marks it close-on-exec. An uncatchable harness termination can leave pytest holding the lock, so inspect the recorded holder and its child run rather than deleting the file.

Live tests, the mutation harness, scratch reset and the scratch write-side stack context acquire the lock. Bootstrap and other writers do not all acquire it. Orchestration must still serialize those operations against the same stack. Locking coordinates access; it does not establish that the configured instance is disposable or safe for tests.
