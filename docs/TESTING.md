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

The [small semantic acceptance set](#semantic-acceptance) checks deterministic archive behavior; real-model evaluation is a separate bounded activity. A canned engine establishes orchestration, not inference quality. Whole-corpus evidence is required when cross-document interactions or material uncertainty remain beyond bounded checks; record the reason and affected stages. A prompt, model or pre-write code edit alone does not require a corpus run. The procedure and local-only boundary are in [AGENT_PROTOCOL.md §5](AGENT_PROTOCOL.md#5-integration-and-corpus-evidence).

## Current test layers and limits

**Offline unit tests.** `tests/unit` is the default for `uv run pytest -q` or `make test`; `pyproject.toml` excludes the `live` marker unless requested. CI runs lint and the offline suite on pull-request pushes and main commits. The offline job installs all optional extras and runs `pytest -n auto --dist loadfile`. A separate `fictional-integration` job runs the required real writer/query lane described below; neither job evaluates real models.

The fixtures in `tests/unit/conftest.py` isolate host settings, model login and instance records. Tests provide their own configuration. Most filesystem sync calls are replaced; use `@pytest.mark.real_fsync` when actual sync behavior matters. Anomaly timers are recorded rather than started. A test that calls `monkeypatch.undo()` removes these function-scoped protections too; use `monkeypatch.context()` for a temporary patch.

The network guard rejects non-loopback lookups and connections, removes proxy variables and fails the test even if code catches the refusal. It is not a complete process sandbox: UDP sends, child processes and earlier-scoped fixtures are not covered. Tests and subprocesses must use fictional inputs and explicit temporary paths. Unit tests must not reach archive credentials or runtime state.

**Offline pipeline coverage.** `tests/unit/test_fixture_corpus.py` exercises per-document stages, the barrier, clustering, response reuse, reports and fixture identity traps with a canned engine behind the real LLM cache. The autouse `_no_write_side` fixture removes reset, registration, writing, places and site from the driver's corpus stages unless a test uses `write_side`. Separate write-side unit tests use an in-memory instance and stand-ins for reset, locking or site generation. These checks do not demonstrate a complete real-stack ingestion, query result or model interpretation. Existing fixture traps and strict expected failures need review when their subsystem changes.

The substitutions are explicit boundaries of offline coverage:

| Stage or service | Default offline substitution | Boundary left to integration |
| --- | --- | --- |
| Transcription, extraction and critique in the fixture corpus | Authored engine responses through the real response cache | Actual model interpretation is not evaluated. |
| Corpus reset, registration, writing, place reconciliation and site generation | `_no_write_side` removes every `touches_wiki` corpus stage; `write_side` tests provide their own fake wiki and service stand-ins | Real Wikibase acceptance, persistence and query results are not established. |
| Host settings, login and instance records | `_no_host_settings`, `_no_claude_config` and the instance-record fixtures use only test-supplied settings and temporary paths | No permission to use archive settings, credentials or runtime state is implied. |
| Network, filesystem flushes and pause timers | The network guard rejects off-host access; flushes are replaced unless `real_fsync`; pause timers are recorded | These defaults do not establish external-service, crash-durability or real-timer behavior. |

**Live integration.** `tests/fictional_integration`, marked `live`, exercises the required disposable writer/query lane and fails when its setup is missing. Tests under `tests/integration`, also marked `live`, exercise an explicitly configured stack or a real model; their availability checks can skip tests. These are separate invocations. Query-string unit assertions do not establish the rows returned by an actual engine.

**Real-model checks.** `tests/integration/test_live_chunked.py` exercises extraction and conversation resumption through the claude CLI and response cache. It needs a logged-in CLI and skips if unavailable. `$YIZKR_LIVE_CACHE` selects its cache; the default is `var/live-cache`. It does not acquire the stack lock. These checks do not establish broad extraction quality across the corpus, and a reused response establishes only the request/configuration it matches.

## Semantic acceptance

Run the compact deterministic set with:

```sh
uv run pytest -q tests/unit/test_semantic_identity.py tests/unit/test_semantic_evidence.py
```

These tests run in the ordinary offline suite and inherit its host-settings and network isolation. [The reusable scenarios](../tests/fixtures/semantic_acceptance.py) select fictional evidence from [the example universe](EXAMPLES.md). They separate source text and intended outcomes from the current `ClaimBatch` adapters. No model supplies the deterministic inputs.

| Scenario | Required observation | Production boundary exercised |
| --- | --- | --- |
| Gustav's register extract gives 7 January 1919; his naturalisation certificate gives 1 July 1919 | Both birth dates retain their precision, normal rank and separate source/location/process support. Repeating the write preserves both without duplicate support. | Claim review and writer serialization |
| Two Ursula Löbigs: the daughter born in 1911 and the unrelated widow in the 1912 directory | The identical name does not merge these incompatible people, including after temporary IDs and input order change. | Corpus identity construction |
| Two mentions proposed as the same Gustav | A confident machine verdict alone changes no identity. Only an explicitly approved displayed match resolves to the chosen item; rejected/unreviewed matches do not. The contradictory evidence and its provenance survive resolution. | Equivalence review and application |
| A face-region proposal for a known fictional person | Unreviewed and bulk approval cannot authorize the association. Explicit approval permits the displayed person/region with provenance; a changed association cannot reuse that approval. | Face proposal construction, claim review and writer enforcement |

The writer tests use the real `BatchWriter` and WikibaseIntegrator serialization with the existing detached in-memory transport from `test_pipeline.py`. Assertions inspect emitted statements and references; the transport supplies scripted reads and storage. This establishes transformation and gate behavior, not Wikibase acceptance, durable persistence, WDQS results, recovery or concurrency. [The isolated integration lane](#live-integration-and-the-transition) remains necessary for those boundaries. The face case uses synthetic detections and embeddings; it does not establish recognition quality.

When replacing evidence, stage or identity implementations, keep the source cases and semantic expectations and adapt their small input/output adapters. Compare attributed values, identity membership and approved associations; do not require prompt literals, generated IDs, record order or helper calls. Extend the set for a consequential new behavior rather than duplicating every existing unit case. These checks do not claim that all [target ingestion cases](INGESTION_CONTRACT.md#fictional-contract-cases) pass: assignment-aware graph writing, retained-state splits and end-to-end incremental recovery still need their implementing tasks and boundary checks. Focused durable-review tests cover approval carry-forward after verified rereading, while the assignment-authority case below checks atomic acceptance and retry at the server boundary. Do not freeze known defects as expected semantic results.

### Optional real-model evaluation

This evaluation is separate from the deterministic checks and is not a prerequisite for using them in refactors. Its purpose is to observe interpretation quality against independently specified expectations. The source texts in the scenario fixture are usable inputs; their prepared batches, expected outcomes and curator decisions are gold data, never model input.

1. Declare the question and sample before calling a model. A bounded extraction sample is `birth-extract-1938`, `naturalisation-1952`, `family-tree-1984` and `directory-1912` from the scenario fixture. Use each complete selected reading, preserving its language, source identity and location; do not combine unrelated sources into one witness. Face approval is a deterministic policy check, not an extraction-quality score. A face-recognition evaluation needs separately specified fictional images and expected regions; the synthetic embeddings here are insufficient.
2. Record a reproducible configuration: repository commit (including prompts, vocabulary and adapters), fixture source keys and content hashes, engine/provider, requested full model ID and actual reported model, client/CLI version, effort and any sampling controls the provider supports, extraction strategy, chunk/retry/concurrency settings, and cache policy. Record unavailable controls explicitly. Retain the exact requests and raw responses, parsed batches, rejects, incomplete-output reports and any later review decisions in a dedicated evaluation directory outside tracked files. These records let another evaluator reconstruct the calls; they cannot guarantee identical new samples from a hosted model.
3. Feed the fixture's transcription adapter to `text_stages.extract` with an explicit `ExtractionRequest` naming that transcription, its artifact and `strategy="single"`. This concrete procedure uses a configured client from `yizkr.pipeline.llm.make_client("claude-code", ...)`, with dedicated transcript storage, and the request's `raw_dump`, `rejects_dump` and `diagnostic_dump` paths. Inspect the returned completeness status; diagnostic batches are not completed proposals. If the change concerns chunking, critique or reconciliation, name and invoke that actual stage and record its additional inputs/settings instead. Do not infer its quality from a single-call extraction check. Close the client after the run. Stop before graph writes.
4. Inspect outputs against the expectations below, resolving temporary references by source and described role, not generated identifiers. Record each obligation as satisfied, failed or unassessable, with the observed evidence and missing information. Abstention can be correct for an unsupported identity; omitting an explicitly stated birth date is a coverage failure. A parsing error, failed call or skipped input is not a passing abstention. Keep machine proposals separate from later human decisions.
5. A useful initial budget is three fresh repetitions of each selected input per configuration, serially. Use a separate initially empty response cache per repetition, or disable response reuse; record actual engine calls and cache hits. A replay is useful for reproducibility but is not another independent sample. Report all attempts, including failures and retries, and per-case variability rather than selecting the best answer or treating a small pass rate as a general quality estimate. If a different budget is needed, declare it before inspecting outputs.

| Evaluation obligation | Required or permitted result |
| --- | --- |
| Contradictory dates | Extract each explicitly stated date with day precision and the correct source. When the two readings are considered together, retain both; do not choose a canonical birth date or invent an explanation for the disagreement. Other supported assertions from the reading are allowed. |
| Homonyms | Preserve the daughter's birth evidence and the widow's stated status with their own attribution. Keep their identities separate or unresolved; matching names alone do not justify equivalence. Do not invent the widow's birth date or a family relationship. |
| Provenance and uncertainty | Preserve available source locations and trace machine interpretations to their process. Do not fabricate citations, precision or independent corroboration. Distinct defensible names/representations are allowed if their evidence remains inspectable. |
| Human decisions | A proposed identity match or face link may remain unresolved. No machine confidence or textual name match counts as curator approval. Model outputs must not acquire human approval state. |

A canned response can prove request/response wiring and downstream handling but cannot establish whether a model read the source correctly. These few fictional readings also do not measure handwriting, layout, multilingual coverage, face recognition or corpus-wide interactions. Use suitable media samples for those questions and complementary corpus review when the risk requires it, following the local-only procedure above. Report deterministic results, observed model results and corpus evidence separately; an unrun optional evaluation contributes no quality evidence.

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

The designated lane is CI's `fictional-integration` job, run on pull requests, main pushes and manual workflow dispatch. The same required invocation runs locally or in an authorized cloud session:

```sh
make test-integration
# Select an explicit evidence/lock directory when needed:
make test-integration ARGS="--state /tmp/yizkr-fictional-evidence"
```

The launcher needs Python 3.11+, Git and Docker with Compose on the local socket; host package installation and `deploy/.env` are unnecessary. It stages only its allowlisted repository source, fixtures and deployment controls into a build context, builds the test image with the locked Python dependencies, and starts a new MariaDB, Wikibase and WDQS stack. Setup failure, test failure, a skipped case or omission of any required case all fail the invocation. The default offline command deselects these tests; invoking a test directly without its dedicated setup fails rather than falling back to another deployment.

[The dedicated Compose file](../deploy/fictional-integration/compose.yml) is independent of the archive deployment. Its fixed fictional credentials and connection settings, project-specific volumes, instance records and locks never inherit archive defaults. The services use an internal network with no published ports; no archive credentials, corpus directories, runtime state or Docker socket are mounted into them. The only host bind mount is a staged copy of the tracked upload configuration. The checkout and selected state directory determine a separate project identity, and a nonblocking lock refuses concurrent use of that lane. Different worktrees have separate projects and locks.

The required case in [test_write_query.py](../tests/fictional_integration/test_write_query.py) bootstraps the real model, uploads the two authored fictional birth-source texts, creates their person through a declared identity map and writes both contradictory birth dates through `BatchWriter`. Fresh API reads verify two normal-rank values at day precision with separate source and process references, page locations and excerpts. Before querying, the test waits until WDQS's `schema:version` reaches each entity's current MediaWiki revision, including the query vocabulary. It then runs the production disagreement-report query and checks both attributed rows. A fresh client repeats bootstrap, registration and both writes; entity IDs, statements, references and query results must remain unchanged.

The required [container case](../tests/fictional_integration/test_container_sources.py) inventories a fictional presentation and PDFs, then uses the common source projection with the real registrar and writer. It checks exact original JPEG deduplication across formats and PDF reading routes, repeated placement references, distinct body and notes citations, and caption ownership on the embedded source. Repeated writes and a changed reading encoding preserve the existing owner and references; a newly excluded source cannot be registered through the projection. A generated image matching an existing artifact's bytes cannot adopt that owner without its original container/part composition evidence. These authored cases make no model calls and do not establish extraction quality or complete pipeline execution.

The required [creation-recovery cases](../tests/fictional_integration/test_write_recovery.py) interrupt real item creation before its receipt is published, change the item's display terms, and resume through a fresh client without allocating another QID. They also finish a source's self-referenced registration after interruption, verify repeated writes, and race independent clients for one server dispatch reservation. A conflicting same-field update checks that the curator's newer value survives; nonconflicting stale edits may be merged by Wikibase. These checks establish the implemented creation and per-item conflict boundaries, not atomic or arbitrarily concurrent batch writes.

The required [assignment-authority case](../tests/fictional_integration/test_assignment_authority.py) verifies entity-ID lookup after curator label changes, atomic publication of a two-mention reviewed delta, competing independent clients and retry after a lost successful publication response. It preserves earlier assignment revisions and checks that the entity set and existing sourced statements, ranks, references and item revisions remain unchanged. The test uses real SQLite review stores and immutable MediaWiki pages. It does not establish assignment-aware graph writing, migration or later-cohort candidate completeness.

The required [identity-migration case](../tests/fictional_integration/test_identity_migration.py) starts with a genuinely bound fictional map entity, curator-edited terms/ranks and nonempty sourced statements. It verifies current explicit assignment review, refusal of missing creation evidence and ambiguous occurrence correspondence, migration without replacing QIDs, and graph-only method/record/confirmation reads. Interrupted metadata publication resumes without duplicate process items or reference blocks; historical claims and existing source/run references remain intact. The historical adjudication metadata is deliberately absent and queried as a limitation, not inferred from current settings. This case does not exercise assignment-routed historical claim application, which remains #758.

Every invocation starts from empty volumes and removes its own containers, volumes and network after success or failure. It keeps `evidence.json`, JUnit `results.xml` when the test process ran, and `services.log` under the selected state directory (by default a checkout-specific directory beneath the system temporary directory). Evidence records the tested Git revision, dirty-tree flag, source-content hash, image IDs and actual non-skipped cases. CI uploads these files as `fictional-integration-evidence`. A dirty-tree run identifies its source hash but is not evidence for an unchanged commit; record the relevant revision and command in the PR. Before #159 retains the shared instance, retain a successful demonstration of this lane and its real checks.

The launcher handles ordinary interruption with teardown. After a hard kill or failed cleanup, recover only this lane with the same state selection:

```sh
make integration-down
# If the run selected an explicit directory, use that same directory:
make integration-down ARGS="--state /tmp/yizkr-fictional-evidence"
```

Recovery refuses while another invocation holds that lane's lock. It does not reset or stop the archive deployment. The fixture clears only its own instance-contents record at entry and exit. Missing integration evidence still blocks affected persistence changes, not unrelated work.

### Additional live checks on an explicit disposable target

The optional existing suite remains available to a centrally coordinated local session that has checked its configured target is disposable. Corpus-derived graphs remain local-only even when a test adds fictional entities. Test runs, mutants and development resets must never mutate retained data:

```sh
make test-live
# Equivalent, or select an individual relevant module:
uv run pytest -m live tests/integration -v
```

These commands use the configured stack; they do not create or prove an isolated target. Check the target before running them. `tests/integration/conftest.py` skips for missing configuration or an unreachable stack, so a zero exit status alone is insufficient: inspect whether the intended tests ran. Record the commit, relevant tests and result as integration evidence.

These configured-stack sessions acquire the live-stack lock and clear the instance-contents key at entry and exit, so a scratch run cannot mistake fixture writes for its own contents. Fixtures mark their own entities, and repeated writes within a session check reuse. Global graph invariants still inspect other sessions' data; an invalid fixture or mutant can contaminate later runs despite unique labels. Recovery is a supported reset of the disposable target, not a reset of retained data.

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
