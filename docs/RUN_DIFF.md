# The run-to-run diff, and the reviewer checklist

A change to a stage before write (its code, a prompt, the manifest, a
model or its parameters) is checked by a whole-corpus scratch run and a
diff against the latest completed run on main (ADR-0025 §4,
docs/AGENT_PROTOCOL.md §5, `needs:scratch-diff`). The diff says what
changed between the two runs, and why. This page says how to make it,
what it holds, and how to read it: the reviewer checklist below is what
the owner, or a reviewer agent, reads a scratch-diff by. It takes the
place of the planned docs/EVAL.md: there is no gold set and no
synthetic eval, and the whole corpus is the test set.

Code: `yizkr.pipeline.rundiff`. It builds on the run report
([`docs/RUN_REPORT.md`](RUN_REPORT.md)) and the fingerprints of
`yizkr.pipeline.fingerprints` (ADR-0025 §4, as amended by #197).

## Using it

```
uv run yizkr scratch diff [<baseline>] [<run>] [--runs DIR] [--main REF]
                          [--cache DIR] [--rebuild]
```

- With no run named, it diffs the latest run in the runs directory
  (`--runs`, default `var/scratch`); with one, that run; with two, the
  second against the first. A run is a run id in the runs directory or
  a run directory's path.
- **The baseline**, unless named, is the latest completed run on main
  older than the diffed run: the latest run of the runs directory whose
  id sorts before the run's (the run's own directory name, however its
  path is given: `.`, or a link of another name) (run ids sort in the order runs start in),
  whose latest invocation went through every stage (`scratch.complete`),
  run on clean code (`code.dirty` false) of a commit git holds in the
  history of `origin/main`, else `main` (`--main REF`, repeatable, picks
  others). A run that an invocation took past its barrier on other code
  (its `scratch.code`, docs/RUN_MANIFEST.md) is a baseline only if that
  code is clean and on main too, since its corpus stages were made by
  it. Git is asked in the checkout the package runs from, whatever the
  working directory, and a commit that is no SHA-1 is never given to it.
  A run made on a PR's branch is not on main until the PR merges, and a
  squash merge makes a new commit, so a PR's run is never the baseline
  of another PR, and the first run on main after a merge is the next
  baseline. With no such run, or when git knows none of the refs, the
  command says so: name the baseline.
- `--rebuild` makes both runs' reports afresh rather than read the
  `report.json` each holds (below).
- `--cache DIR` names the LLM cache the recomputes are attributed from;
  by default the one the run's `run.json` names.

It writes two files into the run's `.diff/<baseline>/` folder, which it
makes private to its owner (mode 700, each file 600), as the report
writes its own, and prints where:

| File | Holds | Leaves the machine |
|---|---|---|
| `diff.md` | the aggregates alone: counts, rates and the pipeline's own names | yes: it is the diff summary that is posted |
| `diff.json` | the aggregates, the names of the settings that differ but `diff.md` only counts, and per document every change: its SHA-1 and file names, each claim added, removed or changed with its values, each mention with its label, each anomaly and failure, and each merge and split with its members | never |

Run again, it replaces each file whole.

**Chained baselines.** Every scratch run starts from fresh state
directories, and every model call goes through the one LLM cache
(ADR-0032). So a PR's run reuses every call of the baseline its change
leaves the same, and the next run on main after the merge reuses the
PR run's calls: nothing needs copying from run to run. The diff says
whether the two runs used the same cache (`Same LLM cache`); if they did
not, the run could reuse nothing of the baseline's.

## What it reads

The two run directories, and of the LLM cache only the components of the
keys their events name. Never the wiki or its query service, the engine
or the corpus: a unit test makes a diff under the refusing wiki of
`tests/unit/test_no_wiki_before_write.py` with an engine factory that
fails if asked. From each run it reads:

- `.report/report.json`, the run report as `yizkr scratch report` stored
  it. It is built afresh in memory (and not written) when the run has
  none, when it cannot be read, when it was made before the run's latest
  invocation ended (or while it ran), or with `--rebuild`. `diff.md`
  says which each run's was: `stored` or `built`;
- `run.json`, for the settings and the cache;
- `events.jsonl`, for the cache's events, which key each call a stage
  made for a document;
- `alerts.jsonl`;
- each document's views (`transcription.json`, `batch.json`,
  `critique.json`), found as the report finds them;
- the clustering stage's `corpus/clustering/identity-map.json`, when its
  decisions hash to its hash (`clustering.read_identity_map`).

From the LLM cache it reads, through the client factory's view of the
cache as it is (`llm.read_cache`: no lock taken, nothing written, so it
runs beside a running pass), for each key the events name the entry
served under it, else one moved aside under it (rejected or replaced,
the view's `asides`),
and uses of that entry only its `components`, and only when they hash
to its key: never a response. A cache that is not there, or one the
cache would refuse (a folder a git work tree would track, or one open
to other users), leaves the recomputes unattributed (`not available`),
and the rest of the diff as it is.

## What it compares

**Runs.** Each run's id, its code's commit (its first 12 hex digits)
and whether the code differed from it, the same of the code an
invocation took it past its barrier on, whether it went to the end, and
whether its report was `stored` or `built`.

**Settings.** The fields of the two `run.json`s' `engine`, `roles`,
`code` and `config` that differ, of which `diff.md` names only an
allow-list (`rundiff.SHOWN_SETTINGS`): each role's model, effort and
epoch (as the role records them, as its client applies them, and as the
configuration names them), the client's configuration hash, the engine's
name and CLI version, and the code's commit and whether it differed from
it. Each shows its value only when it is of its kind (a model id, an
effort, an epoch, an engine, a version, a flag), else `other`; a commit
and a configuration hash show their first 12 hex digits, as `report.md`
does. A model id and a version follow `report.md`'s rule (a value of a
model id's or a version's form is shown as it is): these fields are
written by the operator's configuration and the engine, never by the
corpus, so an operator who posts `diff.md` should not name a model after
anything in the corpus. Every other setting that differs (a language,
the hash of the code's difference from its commit, any free text) is
only counted in `diff.md` ("Other settings that differ, not shown"), its
name and values never shown there; `diff.json` lists their names
(`detail.settings_not_shown`), and their values are in the two
`run.json` files, both local.

**Stages.** Per stage, how each document's stage came about in each run
(`computed`, `reused: <reasons>`, `needs attention`, `failed`, ...), as
the reports give it, and:

- *recomputed by*: for each document's stage the run computed, what sent
  it to the engine. Each call the cache missed is set beside the
  baseline's calls for the same document and stage, and the components
  of its key that differ from the closest of them (the one differing in
  the fewest) are named: `prompt`, `files`, `model`, `effort`, `config`,
  `system`, `call`, `parent`, `opening`, `attempt`, `epoch` or `engine`
  (docs/LLM_CACHE.md, Keys). An extraction's prompt holds its document's
  transcription, so a changed transcription shows as the extraction's
  `prompt`, and so on down. Besides those: `refresh` and `rejected`, a
  miss whose answer was there; `replay changed`, a conversation's opening
  turn that answered otherwise; `same key`, a key the baseline also
  made, whose answer the cache no longer serves; `no baseline call`, a
  document or stage the baseline made no call for; `no call`, a stage
  that makes none (a text file's transcription, the GEDCOM adapter);
  `no cache`; and `not available`, where a key's components cannot be
  read. The stage's row counts documents by their attributions joined,
  and its calls by each;
- *views*: each document's view of the stage in the two runs, compared by
  content hash with the volatile fields left out
  (`orchestrate.TRANSCRIPTION_VOLATILE`, `BATCH_VOLATILE`, as the views'
  freshness leaves them out): `same`, `changed`, `baseline only` or
  `run only`. A recompute that made the same view again is `same`.

**Figures.** Every count, rate and share of the two reports' aggregates
(their `documents`, `stages`, `content`, `anomalies` and `calls`
sections), side by side with the delta; `diff.md` lists those that
differ. Times are left out, and so are the documents per language,
since the corpus's languages are never posted (#458). The per-stage
count deltas, the extraction counts (entities, claims, rejects, chunks,
fallback tiers) and the mention records and judgement notes
(`content.records`, `content.notes`, with their `absent.*` figures) are
among them, and so are the write's counts (`content.write`), the claims
the writer refused for an item value that is their subject's item
(`content.write.claims_self_referencing`, #491) apart from its other
refusals and from extraction's rejects.

**Absent is not zero.** `report.json`'s `format` stays 1 as figures are
added, so a report made by older code lacks the newer figures: one made
before mention records and judgement notes were counted holds no
`content.records` or `content.notes`. A figure under a section a report
does not hold is `absent` on that side, never 0, and has no delta
(`n/a`). A name a tally leaves out (a property no claim gave, a reason no
miss had) is 0: a tally keeps only the names it counted. The report's
own `absent.*` figures count extractions whose coverage predates a
count; they are compared like any other figure.

**Anomalies.** Each finding (a document or the run, a stage and a check,
as the report counts them) is *new* when only the run has it, *resolved*
when only the baseline has it, or in both; by check and by stage. This
is not the alert's own `new` or `carried`, which says whether its output
was answered by the engine in its run.

**Failures.** The documents that fail in the run and not the baseline
(newly failing), and the other way round (no longer failing), by kind
and by failure label, beside each report's count of failed documents.

**Place hierarchy.** The place stage's counts (#201; docs/RUN_REPORT.md,
Place hierarchy), figure by figure, in a section of their own:
questions, calls, answered, uncertain and unanswered, stated links left
out, links written, referenced and already there, links a record alone
states and of them those written, links unresolved, answers rejected by
the schema, and cycles and conflicts refused. A run whose report has no
such counts (its place stage has not run) is `absent` on its side, and a
diff where neither has them says the section is not available.

**Flags and contradictions.** Compared figure by figure when both reports
count them; otherwise `not available`, with why. Today no stage makes
flags (#112), and different-source contradictions are read from a
written graph, so both are not available until a report counts them.

**Claims**, per document and per property, matched by their claim
fingerprints as multisets (ADR-0025 §4, as amended by #197): a claim only
the baseline holds is *removed*, one only the run holds *added*. Of
those, a removed and an added claim that share their subject, property
and artifact are paired as *changed* (each with the one of its group it
differs from in the fewest parts, ties going to the first by
fingerprint, and the removed ones taken by fingerprint, so the pairs
and their tally do not depend on the order a batch lists its claims
in), and counted by what changed: the
`value`, the `qualifiers`, the `locator`. Values compare in NFC with
whitespace kept, so values that differ only in whitespace differ. The
excerpt is no part of a fingerprint: a matched claim whose excerpt
differs is counted apart (`excerpt changed`). Two identical claims of
one batch are one fingerprint counted twice. A claim's subject is its
mention's fingerprint, so a claim whose mention changed (another
label, kind or line span) is removed and added, never changed. The
aggregates count documents with a batch in both runs, in one only, and
with claim changes, and those whose batch, where its stage completed, is
missing or cannot be read (`unreadable`), whose claims count on neither
side; `diff.json` lists each claim with its values, and
its subject's label.

**Mentions**, by their fingerprints: added and removed, by kind.

**Clusters**, by membership over the mentions both identity maps hold: a
cluster of the run whose mentions come from several clusters of the
baseline is a *merge*; one of the baseline whose mentions go to several
of the run's, a *split*; with the mentions in each, by kind, the merges
by the method of the run's cluster (`confident`, or `adjudicated` for
one the adjudicator's verdicts made, #106), and the mentions whose method
changed (`confident → ambiguous`, `ambiguous → adjudicated`, ...).
`Same decisions` says whether the two maps' decisions hash alike. Claim
fingerprints never name a cluster, so a clustering that changes no claim
shows here and nowhere among the claims. Every merge and split is
listed, with its method and its members (fingerprint, document, label
and kind), in `diff.json` alone, which stays on the machine; `diff.md`
gives the counts. The run report's figures, set side by side, add the
adjudication's own counts: the pairs asked, the calls, the verdicts by
kind and the answers refused.

## Known limits

- **A batch without an entity manifest is order-sensitive.** In a batch
  whose extraction fell back to line ranges, a mention's stand-in label is
  the first a ref gives, so claims listed in another order, or a ref
  labelled otherwise in an earlier claim, can change its fingerprint, and
  every claim on it shows as removed and added (fingerprints.py, ADR-0025
  §4). Two mentions equal in every other component swap fingerprints
  when their manifest lists them in the other order.
- **Attribution is per call, against the closest baseline call** of the
  same document and stage. A stage of many calls (an extraction's
  chunks) whose chunking changed has calls with no close partner, which
  name every component they differ in.
- **The settings name what changed, not why**: a code change between the
  runs shows as `code.commit`, and its effect in the stages it reached.

## diff.md: aggregates alone

`diff.md` is rendered from the diff's `aggregate` section alone, and
holds counts, rates, shares and the names of the pipeline's own
vocabulary: stages (the report's), properties and kinds (the model
manifest's; any other is `other`), the anomaly checks, failure labels,
key components, cluster methods, and the reports' own aggregate names.
It never holds a document, a SHA-1, a file name, a path, a label, a
value, an excerpt, a quote or a language code. Unit tests
(`tests/unit/test_run_diff.py`) diff two fictional runs whose file
names and labels come from the example universe, and check that none of
them, no document's SHA-1 and no path is in `diff.md` or in any string
of the aggregates, while `diff.json` holds them. Still, re-read
`diff.md` before posting it (docs/AGENT_PROTOCOL.md §7).

`diff.json` is local-only data, as the run directory is
(docs/AGENT_PROTOCOL.md §6): never posted, committed or sent to a cloud
session.

## The reviewer checklist

For the owner, or a reviewer agent. **A reviewer agent that reads
anything beyond `diff.md` and `report.md` (`diff.json`, `report.json`,
`report.html`, a run directory, the cache) must be a local session**
(ADR-0025 §4, docs/AGENT_PROTOCOL.md §5, §6). A cloud reviewer reads the
posted `diff.md` and `report.md` alone, and asks a local session for the
rest.

Coexisting assertions are a healthy state (ADR-0002): a new claim that
contradicts another is not a regression in itself, and neither is a
claim that differs from the baseline. The question is whether the
change does what its PR says and nothing it does not.

**1. Is the diff the one asked for?**

- The baseline is a completed run on main (`complete: yes`, `differs
  from commit: no`), and the run is at the PR's head, or a local merge
  of the waiting PRs it batches. Both runs' reports are `stored` and
  current, or `built`.
- `Same LLM cache: yes`. If not, every call was asked again, and the
  stages say nothing of reuse.
- The settings that differ are the PR's, and only the PR's: a model,
  effort or epoch change the PR does not make, or a CLI version that
  moved, accounts for recomputes the PR does not cause. Where `diff.md`
  counts other settings that differ and does not show them, a cloud
  reviewer asks a local session which they are (`diff.json`'s
  `settings_not_shown`, and the two `run.json` files).

**2. Did only the stages the change touches recompute, for the reason
it gives?**

- Each stage's *recomputed by* names the components the change touches:
  a prompt edit shows as `prompt` in its stage, and in the stages fed by
  its output where the output changed. A stage the change cannot reach
  that recomputed is a regression (or a sign the baseline was not the
  right one).
- `same key` or `not available` in numbers is a cache problem, not the
  change's: say so.
- A stage that recomputed but whose views are all `same` changed nothing
  downstream: fine, and cheap.

**3. What counts as a regression.** Any of these, unless the PR says it
expects it and why:

- documents newly failing, or a failure label that rose;
- new anomaly findings, above all in a check the change should not touch
  (a `transcription-sparse`, `zero-claims` or `fallback-manifest` finding on a
  document that had none);
- rejects up (`content.rejects.rejected_claims`,
  `content.rejects.unread_objects`, `content.rejects.failed_extractions`),
  fallback tiers up (`content.fallbacks.*`), zero-claim documents up;
- claims removed in bulk from documents the change should not reach, or
  from properties it should not touch (the claims' by-property table);
  claims changed in their `locator` where the change is not about pages
  or regions;
- mentions removed in bulk, or mention records set aside up
  (`content.records.set_aside_records`), recordless mentions up;
- clusters: merges where the change should split, or the reverse; a rise
  in `conflicting` or hard conflicts; `methods_changed` toward
  `ambiguous` in bulk; adjudicated merges (`merges_by_method`) the
  change does not explain, or verdicts that swing from one kind to
  another with no change to the prompt, the model or the batches
  (read the merges in `diff.json`: each adjudicated merge should be one
  person, place or family by its members' labels and documents). A
  union or an event whose people adjudication merged links confidently,
  so its merge shows as `confident`: read a rise in confident merges of
  unions and events beside the adjudicated merges of people;
- calls to the engine far above what the change explains (a cache miss
  for every call of a stage the change does not touch).

**4. What reads as an improvement**, when the PR claims it: findings
resolved, documents no longer failing, rejects and fallbacks down, the
claims the PR targets added or changed in the property it names, and
splits or merges where it says.

**5. A batched run.** When one run checks several waiting PRs, attribute
each stage's changes to the PR that touches it (its `stage:*` label and
the components named), and each claim change to the PR whose stage and
property it is in. A PR whose effect stays unclear gets a run of its
own.

**6. The verdict** goes on the PR with `diff.md` and `report.md`: which
changes it accepts as the PR's, each regression with its figure, and
what the PR must fix. A regression is fixed in the pipeline on the PR's
branch and run again; nothing is repaired in the graph.

**Absent figures** are no change: a figure `absent` on one side was not
counted there (an older report), and neither proves nor disproves a
regression. Read it from the side that counts it, or rebuild the
baseline's report (`--rebuild`), which counts it where the run
directory holds what it needs.
