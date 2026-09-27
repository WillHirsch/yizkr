# ADR-0025: Development by whole-corpus scratch runs

Status: **accepted** — records decisions the owner made on 2026-09-27
(#64). Like ADR-0024, it does not pass through a `proposed` stage: the
owner's merge of the PR that adds it is its acceptance. It reaffirms and
extends ADR-0022 §7, and amends ADR-0024 in place (§2, §3, §4, §7 and the
principles table, with matching edits to §1 and its consequences). For
the development stage only, it lets scratch runs write without the human
gates of ADR-0007, ADR-0009 and ADR-0019; faces stay human-gated under
ADR-0005 in every stage. The operational rules are in
[`docs/AGENT_PROTOCOL.md`](../AGENT_PROTOCOL.md) (§2, §3, §5 and §6), and
CLAUDE.md carries the short form. Small changes amend this ADR in place
with a dated "Amended by #n" line; reversing a ruling takes a new ADR.

Superseded in part by
[ADR-0032](0032-output-reuse-at-the-engine-boundary.md) (#225,
2026-09-27): reuse moves from §2's stage store and stage-version registry
to a cache of model responses at the engine boundary, keyed on what each
call sends. §2's goals stand: reuse of unchanged work, visible
equivalences, and nothing completed lost on abort. Where this ADR speaks
of the stage store, stage keys, stage or method versions and reused stage
outputs, ADR-0032 rules.

## Context

ADR-0024 §7 set up a *development stage*: until the owner declares a
keep-worthy state, graph and run state are regenerated rather than
restored. It left open how the pipeline is developed and checked in that
stage, and reserved this ADR for it. The plan at the time had five parts:

- a separate dev Wikibase stack (#75);
- a gold set of four to six annotated documents (#79 to #81), an offline
  eval harness (#77, #78) and an eval gate with thresholds in
  docs/EVAL.md (#83), run against every prompt, schema or model change
  (`needs:eval`);
- rehearsals on the fixture corpus and on the real corpus (#122, #148);
- production write passes, each with its own cluster review and
  ratification (#150, #152 to #154);
- carrying the owner's review decisions forward across regenerations by
  semantic fingerprint (#125).

The owner replaced that plan on 2026-09-27, for four reasons.

- **A handful of documents cannot stand for the corpus.** The corpus
  spans several source kinds and more than one language, and the
  failures that matter show up across it, not in a chosen few
  documents: transcription that accepts a tool's failure text as the
  transcription (#175) is one. A gold set also costs the owner
  annotation time before it catches anything.
- **ADR-0022 §7 had already chosen the live flow over a synthetic
  eval.** Every intermediate is a durable, inspectable file, so an error
  is visible and attributable to the pass that made it. What was missing
  was a way to run the live flow over the whole corpus often enough to
  develop against.
- **Owner review before the pipeline settles is wasted.** Each prompt or
  process fix regenerates the outputs it touches. A cluster review or a
  ratification taken on one run then either lapses, or is carried
  forward by machinery that is exact only for outputs that did not
  change. The owner's attention is the scarce resource (ADR-0024 §3).
- **Recomputing everything for each change is too slow and too costly,
  and the current caches cannot say what is still valid.** Chunk and
  manifest outputs are cached on the transcription's hash alone, not on
  the prompts, manifest or model that also shape them. Freshness gates
  compare file modification times, and one pipeline step reads its
  inputs from the live graph (#128). So after a change there is no sound
  way to know which outputs it left valid.

## Decision

### 1. The development stage develops the pipeline by whole-corpus scratch runs

- **A scratch run is fully unattended, end to end.** It takes every
  non-excluded source of the whole corpus through every stage:
  transcription and its check, extraction (entity manifest, chunks,
  critique), the GEDCOM adapter, corpus clustering and flags, and then
  reset, register, write, place-hierarchy reconciliation and the site.
  It has no human gate. There is no cluster review and no ratification:
  confident clusters merge, ambiguous ones stay split and are counted,
  and flags are computed and reported but gate nothing. One command, the
  scratch driver (#194), runs it.
- **It writes to the one existing instance, which one command resets and
  repopulates per run** (#199). The reset stops the stack, removes its
  graph volumes, starts it, bootstraps the manifest, publishes the
  queries and runs the smoke test. It clears write-phase state but never
  the stage store, the corpus or run reports, and it takes no backup
  (ADR-0024 §7). There is no separate dev stack. The scratch write stage
  refuses unless its own run reset the instance, so scratch writes never
  mix with other state (#200).
- **Scratch writes keep every non-negotiable rule that applies to a
  write.** Every claim is written at normal rank with both references:
  `stated in` the artifact or fragment with its locator, and `inferred
  from` the run item (CLAUDE.md rules 1, 2 and 4). The run item is marked
  as an unratified scratch run, and the LAN site says the instance holds
  a development-stage scratch run and names it.
- **Scratch graphs are development artefacts, never confirmations.**
  Nothing in one is kept, published or exported: no backup, public
  export or public site is ever made from it, and the next reset
  discards it. Nothing written by a scratch run counts as a human
  confirmation for any purpose, privacy included.
- **Faces and depicts are outside scratch runs.** The driver never runs
  face detection, and no scratch run writes a depicts statement; depicts
  proposals are only counted in the run report. Faces stay human-gated
  as ADR-0005 rules, in every stage, and the faces work is parked
  (#160).
- **Place-hierarchy reconciliation runs as an unattended post-write
  stage** (#201). Its `part_of` proposals (ADR-0009, ADR-0017) are
  applied at normal rank, with ADR-0009's provenance and cycle guard and
  no review CSV. The stages before write use only the containment the
  manifests assert; graph containment is a product of the write.
- **Problems are fixed in the pipeline and regenerated, never repaired in
  the graph.** A problem found in a run report, a diff or the views of a
  scratch graph is filed as an issue with the `stage:*` label of the
  stage it changes, fixed in code, prompts, the manifest or config, and
  regenerated by the next scratch run. A hand edit to a scratch graph
  would vanish at the next reset and hide the defect. Any write to the
  instance that the scratch write stage did not make (a live test, a
  manual edit, an experiment) clears the recorded instance-contents key,
  so the next scratch run resets first (#199).
- **No owner decisions about the data are taken or carried in the
  development stage.** There is nothing to carry forward, so fingerprint
  carry-forward (#125) applies between kept runs only, from the keeper
  run on.

The human gates this bypasses are those of the per-document path:
ADR-0007's identity gate, ADR-0019's two gates per artifact and
ADR-0009's review of containment proposals. That path keeps them until
#119 removes it. From the keeper run on, cluster review and ratification
(§6) are the human confirmation that R3's review step and CLAUDE.md's
"do not auto-confirm" rule ask for.

### 2. Stage-level reuse

Whole-corpus runs are affordable only if a run recomputes just what a
change affects. The stage store (#87), under `var/`, holds each stage's
output: per document (transcription, transcription check, entity
manifest, chunk outputs, critique, batch) and per corpus (identity map,
flags). The raw output of every LLM call is stored beside its parsed
output. The store holds pipeline intermediates, which are proposals, not
a second store of assertions: the graph stays the system of record.

- **A stage output's key hashes:**
  - its input content: the media SHA-1 for the first stage; after that,
    the canonical hash of the upstream outputs, with volatile fields left
    out, so an identical recompute upstream does not invalidate
    downstream stages;
  - the stage's declared version;
  - the per-document route, where a stage has several methods. A
    deterministic routing step picks a method per document or page (for
    transcription, a PDF's text layer or vision, #176), and each method
    has its own version, so a new or changed method recomputes only the
    documents routed to it;
  - the hashes of the prompt templates and of the manifest the stage
    uses;
  - the full model id (never an alias), effort and parameters.
- **Keys never include QIDs, mtimes or timestamps.** QIDs change with
  every reset, mtimes with every copy, and timestamps with every run;
  any of them would stop a key from ever matching again.
- **Stages before write take inputs only from the stage store, the
  corpus and config, and never read the wiki** (#128). The instance is
  reset for every run, so anything read from it is an input that no key
  can name. A wiki stub that refuses reads and writes proves it.
- **A stage-version registry declares the version of each stage and of
  each method.** A unit test checks the registry against each stage's
  source hash and fails when the source changed without a version bump.
  An edit that does not change behaviour (a comment, a rename, a
  refactor) is recorded as a *functionally neutral* entry for the new
  source hash instead, so the key stays the same and the outputs are
  reused. The PR review checks that a neutral entry really is neutral.
- **An output is reused only on an exact key match, or through a
  recorded, visible equivalence.** Archived transcriptions (#104) are
  the case for equivalences. One is adopted only after the transcription
  failure checks (#175) pass. It is stored under its own *backfilled*
  key, built from its own recorded engine and version, date and, where
  derivable, prompt hash; it never claims the current prompt or model. A
  recorded equivalence maps that key to the current transcription key,
  and reports and diffs show *adopted* as a reuse reason distinct from a
  key match. A transcription stage-version bump drops the equivalence, so
  the adopted outputs are then recomputed.
- **Aborting a run loses no completed stage output.** Outputs are written
  atomically, so an interrupted write never leaves a partial output that
  looks complete. Every completed output survives an abort, a pause, a
  failure or a timeout, and re-running the same command resumes with
  nothing completed redone.
- **The write side is keyed too.** The write stage's key covers the
  identity-map hash, the batch keys, and the writer and manifest
  versions. A run skips reset and write only when that key equals the
  recorded instance-contents key and no other write has cleared it
  (#200).

This keeps the stateless, file-based core of ADR-0019 §4: a run
re-derives what to do from durable files and resumes by re-running. What
changes is how validity is decided: by content keys, not by modification
times, and never by reading the live graph.

### 3. Run feedback

An unattended run has to show what it is doing, stop itself when it is
futile, and explain itself afterwards.

- **Events.** Every stage output that is computed, reused (with the
  reason: key match or adopted), failed (with the reason) or needs
  attention appends a per-stage, per-document event to the run's
  `events.jsonl`. The live monitor (#195) reads them while the run
  unfolds.
- **Canary ordering.** One document of each source kind goes through
  every per-document stage first, so a misalignment shows within the
  first calls (#194).
- **Anomaly classes** (#196). Cheap checks with no LLM run as each output
  is stored, and again over reused and adopted outputs when they are
  dispatched, so a defect in a reused output is never silent. The
  classes are: failure or refusal text in a transcription, or one
  implausibly short for its pages; language anomalies; zero entities or
  claims from a non-trivial transcription, and fallback manifests or
  segmentation; chunk failures, truncations and schema rejects above a
  per-document rate; entity, claim or critic-finding rates far outside
  the run's distribution; and a stage's failure rate over a rolling
  window. Each alert names the stage, the check, the document by SHA-1
  prefix, the measured value, and whether it is *new* (on a computed
  output) or *carried* (on a reused one). Thresholds live in one
  documented config, and changing them re-evaluates stored outputs
  without recomputing any stage. Alerts are signals for the operator and
  the report. They never alter or drop a claim (ADR-0002).
- **Per-slice fail-fast pauses.** A slice is a stage or a source kind. A
  check marked fail-fast pauses only its slice, through the run's control
  file, and other slices keep running. If the operator does not resume
  it within a configurable timeout, the paused slice is aborted, so an
  overnight run never stalls indefinitely.
- **Abort and resume.** Ctrl-C, `yizkr scratch abort`, or a request in
  the control file from the monitor, an anomaly check or pacing stops
  dispatch for the whole run or for one slice. In-flight calls finish or
  are cancelled, every completed output stays stored (§2), and re-running
  the same command resumes. Subscription limits pause a run and never
  fail a document (#101).
- **The run report** (#124). `yizkr scratch report` works on a completed,
  running or aborted run. Per stage it shows what was computed, reused by
  reason, failed or needs attention, and the key component that forced
  each recompute (input, stage or method version, prompt, manifest,
  model or parameters); anomalies by check, new and carried; calls and
  wall time; and content counts (documents, entities, claims, rejects,
  critic findings, clusters, and held depicts proposals). It writes
  `report.html` and `report.json`, which stay local, and an
  aggregate-only `report.md` that is safe to post.

### 4. Checking prompt, schema and model changes: `needs:scratch-diff`

- **A change to a key component of a stage before write is checked by a
  whole-corpus scratch run.** That covers a stage's source with a
  version bump, a prompt, the manifest or claim schema, and a model or
  its parameters. The run reuses every stage the change leaves
  unaffected. The evidence is its run report plus a diff against the
  latest completed run on main (#198), read by the owner or by a
  reviewer agent in a local session. The PR and its issue carry the
  `stage:*` label of each stage changed, and `needs:scratch-diff` when
  the stage already runs.
- **The diff says what changed and why.** Per stage, it shows what was
  reused by reason or recomputed, and which key component changed. It
  gives count deltas, new and resolved anomalies, failures, flags and
  contradictions. It lists claims added, removed and changed per
  document and property, matched by the shared claim fingerprints
  (#197), and cluster merges and splits compared by membership. The
  detailed diff stays local; its aggregate summary is safe to post. A
  reviewer checklist (#198) says what counts as a regression. It
  replaces the planned docs/EVAL.md.
- **Baselines chain.** A PR's run reuses the baseline's unaffected
  stages, and the next run on main after the merge reuses the PR run's
  stages.
- **Several waiting PRs may share one scratch run** (owner, 2026-09-27).
  The diff attributes changes per stage and per claim where it can, and
  a PR whose effect stays unclear gets a run of its own.
- **A reviewer agent that reads anything beyond the aggregate summary is
  a local session.** The HTML and JSON reports, the detailed diff and the
  stage outputs hold corpus content (ADR-0024 §4).
- **Dropped:** the gold set, the offline eval harness, the eval gate, the
  separate dev stack and `needs:eval`. The whole corpus is the test set.
  None of the gold set was drafted, so it leaves the local-only list.

### 5. ADR-0022 §7 is reaffirmed and extended

ADR-0022 §7 ruled that validation is the live flow, not a synthetic
eval. This ADR keeps that ruling and extends it: in the development
stage the live flow is the whole-corpus scratch run. Its report, its
diff against the latest run on main, and the views of its graph are
where errors show. Each error is attributable to a stage and to the key
component that changed, and is fixed in the pipeline. The supersession
note that was planned for ADR-0022 §7 becomes an "extended by ADR-0025"
note.

### 6. Leaving the development stage

- **The trigger.** When remaining development no longer changes the
  stage outputs before write, as the convergence report shows (#155),
  the owner decides to stop restarting from scratch (#159). The
  convergence report also flags merged PRs that changed a stage without
  its `stage:*` label, so unlabelled work cannot hide.
- **The keeper run** (#151) then runs once on the converged stage
  outputs, reusing them, and applies cluster review and ratification
  (ADR-0026, ADR-0027). It is the first run anyone ratifies.
- **The kept-state stage follows.** CLAUDE.md rule 3 applies in full,
  milestone backups start (ADR-0024 §7), and keeper mode writes a
  kept-state marker so the reset command refuses on the instance (#118,
  #199).
- **Development continues without an instance.** From then on,
  `needs:scratch-diff` runs stop `--until` the last stage before write,
  so they need no instance.
- **Anything that needs writes waits for #159's instance decision:** live
  tests, scratch-diff runs that write, and experiments such as #160 and
  #169. The options are a second instance, or write paths frozen after
  the keeper run.

### 7. Development-stage code may run ahead of the proposed drafts

ADR-0026 to ADR-0030 are proposed drafts, accepted in the owner's
rulebook sessions (#72, #74). Development-stage code may implement the
parts of them that scratch runs need, such as content-addressed
artifacts (#102, #103), unattended clustering (#105) and a writer that
mints and resolves only through the identity map (#109). It need not
wait for acceptance. The drafts are accepted before the keeper run, and
keeper mode refuses until every ADR it implements is accepted (#118).
Where an accepted ruling differs from the code, the code follows the
ruling before the keeper run.

### 8. Protocol consequences

- `needs:scratch-diff` replaces `needs:eval`, and `stage:*` labels mark
  work that changes a stage before write. `req:*` labels stay on epics
  only.
- Live tests run on the one instance, which scratch runs reset. A
  live-test session ends by clearing the recorded instance-contents key,
  or by resetting and re-running the write stage of the latest scratch
  run.
- A scratch run holds the claude-code subscription for its LLM stages,
  and holds the live stack only while its reset, register, write,
  place-reconcile and site stages run.
- The #34 edge gates cloud dispatch only: a local session may take an
  issue whose only open blocker is #34.
- The stage store, run directories, live status pages, `report.html`,
  `report.json` and detailed diffs are local-only, all under `var/`.
  Only aggregate reports and diff summaries leave the machine.

## Consequences

- The owner's review time goes into one cluster review and one
  ratification, in the keeper run, on outputs that have stopped
  changing. Until then the owner reads aggregate reports and diffs and
  walks the views of a scratch graph (#141).
- A development-stage graph holds unreviewed machine output. That is
  acceptable only because it is reset every run, carries the scratch-run
  marker, and is never kept, published, exported or treated as
  confirmed.
- Every prompt, schema or model change costs a whole-corpus run of the
  stages it affects. Stage reuse and batched verification keep that
  affordable, and pacing (#101) keeps headroom on the subscription for
  development sessions.
- Stage keys make reuse sound, but only while every input is named in a
  key. The registry test, the stage labels and the convergence report's
  unlabelled-change check are the guards.
- The scratch path and the per-document `ingest run` path coexist until
  #119 removes the latter, and the per-document gates still apply there.
- Where live tests run, and where write-path development happens after
  the keeper run, stays open until #159.

## Alternatives considered

- **The eval gate with a gold set.** Dropped: a few documents cannot
  represent the corpus, annotation costs the owner, and a gate over
  per-document precision misses corpus-wide failures that a scratch
  run's anomalies and diffs catch.
- **A separate dev stack** (#75). Dropped: in the development stage the
  one instance is disposable anyway, so resetting it per run is simpler
  than running two. A second instance comes only if #159 chooses one.
- **Cluster review and ratification on every run, with carry-forward.**
  Rejected: it spends the owner's time on outputs that will be
  regenerated, and carry-forward is exact only for outputs that did not
  change.
- **Correcting problems in the scratch graph.** Rejected: the fix is lost
  at the next reset, and the pipeline defect stays.
- **Recomputing everything on every run.** Rejected: too slow and too
  costly on the subscription for a development loop.
- **Keying reuse on modification times, or letting early stages read the
  graph.** Rejected: neither is stable across copies and resets, so
  reuse could not be trusted.
