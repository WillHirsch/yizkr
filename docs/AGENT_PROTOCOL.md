# Agent protocol

How work moves from an issue to main, and how real data stays in the
private area. It applies to every session that changes the repository or
its GitHub issues and PRs, agent or orchestrator. A *local session* runs on
the owner's machine and may read private data; a *cloud session* runs
elsewhere and sees only the repository. The decisions and their reasons
are in [ADR-0024](adr/0024-planning-and-agent-execution.md) and, for
development by whole-corpus scratch runs,
[ADR-0025](adr/0025-development-by-scratch-runs.md); CLAUDE.md carries the
short form.

## 1. Plan and cite in GitHub terms

- Cite issues and PRs by `#number`, decisions by ADR number (ADR-0024),
  and requirements by R-number (docs/REQUIREMENTS.md). Never write the
  planning file's internal keys or codes into GitHub or the repository.
- The plan lives in GitHub: issues; epics (parent issues with native
  sub-issues); milestones; native blocked-by links; labels; and the
  Project's Status, Size, Owner touchpoint and Iteration fields.
  ROADMAP.md is retired (#73).
- **Keep issue bodies true.** An issue body is its task's source of truth.
  When scope or a fact changes, correct the body and add a dated comment
  ("Updated 2026-10-02: ...") that says what changed and why. An agent
  edits only the issues its task names, and reports stale bodies elsewhere
  to its orchestrator.

## 2. Labels

| Label | Meaning |
|---|---|
| `exec:local-agent` | Needs a local session: touches the corpus, `var/`, private data or a live or dev graph |
| `exec:cloud-agent-ok` | Code only; a cloud agent may take it once the cloud-dispatch gate (#34) closes |
| `exec:owner` | An owner action or decision; never dispatched |
| `needs:live-test` | The PR needs `make test-live` evidence from a local session before merge (§5) |
| `needs:scratch-diff` | The PR is checked by a whole-corpus scratch run and a run diff before merge; several waiting PRs may share one run (§5) |
| `stage:*` | The stage before write whose outputs the issue or PR can change: `stage:transcription`, `stage:extraction`, `stage:clustering`, `stage:flags` |
| `area:*` | The part of the repository the issue edits; used by the conflict rule (§3) |
| `kind:*` | bug, feature, decision, adr, experiment, chore, ingestion-pass, docs, research, review (of run output or evidence) |
| `req:*` | The requirements an epic serves; on epics only, never on their issues |
| `parked` | Deferred until the trigger in its body fires; no milestone; never dispatched |
| `gate:cloud-dispatch` | Marks #34 |
| `epic` | A parent issue grouping sub-issues |

**Stage labels.** Every issue or PR that can change what a stage before
write produces carries the matching `stage:*` label: a change to the
stage's code, a prompt template it uses, the manifest, or the model,
effort or parameters it runs with (ADR-0025 §2, ADR-0032). A PR that
changes a stage that already runs also carries `needs:scratch-diff` (§5).
An edit that cannot change any output (a comment, a rename, a refactor)
needs neither label; the review checks that it is neutral. New stages get
their own `stage:*` label as they are added. The convergence report
(#155) lists the open issues under each `stage:*` label and flags merged
PRs that changed a stage without one.

## 3. Dispatch and claiming

- **Ready.** An issue is dispatched only when all its native blocked-by
  links are closed. The #34 edge gates cloud dispatch only: a local session
  may take an issue whose only open blocker is #34. Parked and
  `exec:owner` issues are never dispatched. `exec:cloud-agent-ok` issues go
  to cloud agents only after #34 closes; until then a local session may
  take them.
- **Standing go-ahead, no WIP ceiling** (owner, 2026-09-27). Every ready
  issue is dispatched without asking, however wide the total active
  surface gets, as long as it does not interfere with work in flight under
  the two rules below.
- **Conflict rule.** At most one in-flight issue that edits the repository
  per `area:*` label; an issue with several area labels holds each of them.
  Tasks that edit nothing in the repository (audits, experiments that only
  report, spot-checks) don't count.
- **Shared resources.** One agent at a time uses the live Wikibase stack
  (live tests, the scratch reset, bootstrap, anything that writes to it).
  One LLM-heavy task at a time runs on the claude-code subscription. A
  scratch run holds the subscription for its LLM stages, and holds the live
  stack only while its reset, register, write, place-reconcile and site
  stages run; its stages before write never touch the stack.
- **Claiming.** Set the issue's Project Status to *In progress* and add a
  dated comment naming the kind of session that took it (local or cloud).
  Every agent acts as the owner's GitHub account, so the assignee carries
  no information.
- **Stalls.** A task that stalls goes back to *Ready* or *Backlog*, with a
  dated comment saying why.

## 4. From issue to main

One issue, one PR, one reviewable diff. Code issues are size S or M;
split anything larger. Size L is only for local work that produces no code
diff: scratch runs, the keeper run and reviews of their output.

1. **Isolate.** Work in your own git worktree, never in the owner's
   checkout. Run `git fetch origin`, then cut a branch named
   `<issue-number>-<slug>` from `origin/main`.
2. **Stay in scope.** Change only the files and GitHub entities your task
   names. Docs move with code.
3. **Check.** Before pushing, `uv run ruff check .`,
   `uv run ruff format --check .` and `uv run pytest -q` pass. Then do the
   self-review (§7).
4. **Push only your branch.** Never push to main, force-push, rewrite
   pushed history or push to another task's branch. Once a PR has merged,
   its branch is finished: pushing to it would recreate the deleted branch
   on GitHub, so follow-up work goes on a new branch cut from a fresh
   origin/main. Check `gh pr view <n> --json state` before pushing to an
   existing PR's branch.
5. **Open a draft PR** with `gh pr create --draft --base main`, following
   the PR template: the body starts `Closes #n`, names the exec label and
   the requirement, and gives the evidence. Move the issue's Status to
   *In review*.
6. **Review.** The PR gets an adversarial review, and required fixes are
   pushed to the same branch. Alongside correctness, scope and real data
   (§7), the review checks that comments and docstrings in `src/yizkr/`
   describe how the code works now and carry no development history such
   as issue or PR numbers; ADR references are fine (CLAUDE.md, Engineering
   conventions). The verdict names the commit it reviewed; any commit
   pushed after that is re-checked by the reviewer before the PR is marked
   ready.
7. **Mark it ready.** The orchestrating session runs `gh pr ready` once
   the verdict is in and the fixes are checked. Until then the PR stays a
   draft. This is the standard for all agentic work: a draft means "not
   ready to merge".
8. **The owner merges** PRs marked ready, by the method of their choice.
   Nothing may depend on branch SHAs surviving a merge, so cite issue and
   PR numbers instead.

**Hooks.** `make hooks` installs the pre-commit hook (ruff) and the
pre-push hook (`scripts/git-hooks/pre-push`). The pre-push hook refuses
any push that would create, move or delete main on any remote, including
pushes of several refs at once. Hooks live in the common git directory, so
one run in the owner's checkout covers every worktree. `make hooks` will
not overwrite a pre-push hook that is not yizkr's. The hook is a tripwire,
not an access control: `git push --no-verify` skips it, which is for a
deliberate owner action only. Agents never use it.

**Templates.** New issues use the forms in `.github/ISSUE_TEMPLATE/` (bug,
feature, decision, ingestion pass). An agent that files an issue with
`gh issue create` follows the matching form's fields. PRs use
`.github/pull_request_template.md`.

## 5. Verification that needs a local session

- **`needs:live-test`** goes on any PR that changes a write path. A local
  session runs `make test-live` and posts the results on the PR before
  merge. Live tests run on the one instance, which scratch runs reset
  (ADR-0025), and hold the live stack while they run (§3). They write
  fictional fixture data only. The session ends by clearing the recorded
  instance-contents key, or by resetting and re-running the write stage of
  the latest scratch run, so that no scratch run mistakes the instance for
  one it wrote (#199). Once the instance is kept, live tests wait for
  #159's decision on where they run.
- **`needs:scratch-diff`** goes on any PR that changes a stage before
  write that already runs (§2). The procedure:
  1. **Run.** A local session runs a whole-corpus scratch run at the PR's
     head, or at a local, never-pushed merge of several waiting PRs onto
     current main. Batching is allowed (owner, 2026-09-27). The run starts
     from fresh state directories, and the LLM cache answers every model
     call the change leaves the same as in the baseline, the latest
     completed run on main (ADR-0032). It holds the subscription
     (§3). In the development stage it may go on through write, resetting
     the instance and holding the live stack for its write-side stages;
     once the instance is kept, it stops `--until` the last stage before
     write.
  2. **Report and diff.** The session runs `yizkr scratch report <run>` and
     `yizkr scratch diff <baseline> <run>`. The HTML and JSON reports and
     the detailed diff stay under `var/` (§6).
  3. **Read.** The owner, or a reviewer agent, reads them with the
     reviewer checklist that the diff issue adds (#198). A reviewer agent
     that reads anything beyond the aggregate summary must be a local
     session.
  4. **Post.** The aggregate `report.md`, the diff summary and the verdict
     go on the PR, and for a batch on each PR in it, with changes
     attributed per stage and per claim where the diff can. A PR whose
     effect stays unclear gets a run of its own.
  5. **Fix.** A regression is fixed in the pipeline on the PR's branch and
     run again. Nothing is repaired in the graph.

  Until the scratch driver, report and diff land (#194, #124, #198), no
  scratch-diff evidence exists, and PRs with the label wait.
- While a PR waits for this evidence, its Project Status is *Needs local
  verify*. Cloud agents never provide the evidence; they leave the label
  for a local session.

## 6. The data boundary

**Local-only data.** Only local sessions read these, and nothing from them
is copied into the repository, GitHub or a cloud agent's session. Any
issue whose work reads them is `exec:local-agent`:

- the corpus: `import/`, or the directory `YIZKR_CORPUS_DIR` names once
  #29 moves it out of the tree;
- `var/` and `bundle/`. `var/` holds the LLM cache (model responses and
  session transcripts), run directories (events, alerts, control files,
  document views), live status pages, `report.html` and `report.json`,
  detailed run diffs, batches, review sheets, galleries and plan state.
  Only a run's aggregate `report.md` and diff summary may leave the
  machine (§7);
- `~/yizkr-private/` and everything in it: the archives, the private audit
  inventory, and private experiment outputs;
- backups (`yizkr backup` output and stack archives) and the exclusion
  list;
- any live or dev graph: the Wikibase stack, its query service, and their
  volumes and dumps.

Cloud agents see the repository and its fictional fixtures, nothing else.
(Sending corpus content to a model during ingestion is a separate matter,
governed by CLAUDE.md rule 7.)

**The cloud-dispatch gate (#34).** No cloud agent is dispatched until the
owner closes #34. Every `exec:cloud-agent-ok` issue reaches #34 through
blocked-by links, directly or transitively. When you file a new
`exec:cloud-agent-ok` issue, link it to #34 or to an issue that is already
blocked by it. The #34 edge gates cloud dispatch only; it does not stop a
local session (§3).

**Never ingest what is excluded (fail closed).**

- A document on the exclusion list is never ingested: not registered,
  transcribed, extracted or sent to a model.
- Every ingestion entry point calls the one shared exclusion check (#29).
  Production and extract-all modes refuse to run when no exclusion list is
  configured. Never work around the refusal.
- The owner keeps the list, outside the repository. If a document looks as
  if it should be excluded, stop and report it; never ingest it to find
  out.

## 7. Real data in anything that leaves the machine

**Examples** in code, comments, prompts, tests, docs, commit messages,
issues, PRs and comments come only from the fictional universe
(docs/EXAMPLES.md). Never use real family data, not even partially
anonymised: no real name with a changed surname, no real date shifted by a
year, no real town, address or firm, and no invented document that mirrors
a real one by kind and date. If an example needs a pattern the universe
lacks, add it to the universe first.

**Never quote, paraphrase or identify corpus content** outside the private
area.

**GitHub text is pseudonymous.**

- People are cited by QID only.
- Artifacts are cited by QID; by slug only when the slug holds no personal
  name; otherwise by SHA-1 prefix plus a generic descriptor ("`3f2a9c1`, a
  typed letter").
- No real names or dates of family members.
- Metrics are aggregates only: counts and rates, never rows about one
  person or one document.

**Self-review before anything leaves the machine.** A session that has
handled real data, before every push and every post:

1. re-reads the whole commit range being pushed, messages and diffs
   (`git log -p origin/main..HEAD`);
2. re-reads every issue, PR and comment text it is about to post;
3. confirms that none of it quotes, paraphrases or identifies anything
   from the corpus, that it contains no plan-internal codes, and that
   issues are cited by #number;
4. records that it did so, in the PR checklist or its report.

If real data turns up in a commit that is not yet pushed, fix the commit
before pushing. If it is already pushed or posted, stop and tell the owner.
Don't force-push, delete or edit it yourself: an edit leaves the old text in
GitHub's revision history, so scrubbing is the owner's call.

Enforcement is by this convention and self-review, not by a token
denylist; ADR-0024 §5 says why.

## 8. Decision records (ADRs)

- Claim the next free ADR number in the pinned registry (#19) before
  cutting a branch.
- Drafts merge with status `proposed`, and the owner accepts them in
  batched sessions (#72, #74). The issue that drafts an ADR closes when the
  draft merges. Open decision clauses live in the session issues, not in
  the drafts. An ADR that records decisions the owner has already made
  (ADR-0024, ADR-0025) is accepted by the owner's merge instead.
- Small changes amend an ADR in place, with a dated "Amended by #n" line in
  its status header. Reversing a ruling always takes a new ADR.
- Cite decisions by ADR number everywhere, and keep the registry's status
  column current.

## 9. The owner's attention

- Group everything that needs the owner into touchpoints (the Project's
  Owner touchpoint field), each with a time estimate. The budget is about
  four per iteration, plus two fixed PR-review slots a week.
- A report or PR lists the owner's actions in one place, each with what to
  run or decide.

## 10. Scaffolding scripts

Scripts that create or edit labels, milestones, Project fields or issues
must be safe to re-run. They pause at least 1 s between GitHub mutations,
write a resumable checkpoint (plan key to node id) after each step, and
self-review every title and body for real data before sending it.
