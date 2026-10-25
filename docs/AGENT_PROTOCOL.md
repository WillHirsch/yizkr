# Agent protocol

How work moves from an issue to main, and how real data stays in the private area. It applies to every session that changes the repository or its GitHub issues and PRs, agent or orchestrator. A _local session_ runs on the owner's machine and may read private data; a _cloud session_ runs elsewhere and sees only the repository. The decisions and their reasons are in [ADR-0024](adr/0024-planning-and-agent-execution.md) and, for development by whole-corpus scratch runs, [ADR-0025](adr/0025-development-by-scratch-runs.md); CLAUDE.md carries the short form.

## 1. Plan and cite in GitHub terms

- Cite issues and PRs by `#number`, decisions by ADR number (ADR-0024), and requirements by R-number (docs/REQUIREMENTS.md). Never write the planning file's internal keys or codes into GitHub or the repository.
- The plan lives in GitHub: issues; epics (parent issues with native sub-issues); milestones; native blocked-by links; labels; and the Project's Status, Size, Owner touchpoint and Iteration fields. ROADMAP.md is retired (#73).
- **Keep issue bodies true.** An issue body is its task's source of truth. When scope or a fact changes, correct the body and add a dated comment ("Updated 2026-10-02: ...") that says what changed and why. An agent edits only the issues its task names, and reports stale bodies elsewhere to its orchestrator.

## 2. Labels

| Label | Meaning |
| --- | --- |
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

**Stage labels.** Every issue or PR that can change what a stage before write produces carries the matching `stage:*` label: a change to the stage's code, a prompt template it uses, the manifest, or the model, effort or parameters it runs with (ADR-0025 §2, ADR-0032). A PR that changes a stage that already runs also carries `needs:scratch-diff` (§5).  
**The pre-baseline exception:** until the first whole-corpus scratch run (#145) starts, there is no baseline to diff against, so a PR that changes a stage before write carries its `stage:*` label but not `needs:scratch-diff`, and the first run's baseline includes the change (as on #228, #247 and #273). The exception ends when that run starts: a stage PR still open then gets `needs:scratch-diff`, and is diffed against that run. An edit that cannot change any output (a comment, a rename, a refactor) needs neither label; the review checks that it is neutral. New stages get their own `stage:*` label as they are added. The convergence report (#155) lists the open issues under each `stage:*` label and flags merged PRs that changed a stage without one.

## 3. Dispatch and claiming

- **Ready.** An issue is dispatched only when all its native blocked-by links are closed. The #34 edge gates cloud dispatch only: a local session may take an issue whose only open blocker is #34. Parked and `exec:owner` issues are never dispatched. `exec:cloud-agent-ok` issues go to cloud agents only after #34 closes; until then a local session may take them.
- **Standing go-ahead, no WIP ceiling** (owner, 2026-09-27). Every ready issue is dispatched without asking, however wide the total active surface gets, as long as it does not interfere with work in flight under the rules below.
- **Conflict rule.** At most one in-flight issue that edits the repository per `area:*` label; an issue with several area labels holds each of them. Tasks that edit nothing in the repository (audits, experiments that only report, spot-checks) don't count.
- **Shared resources.** One agent at a time uses the live Wikibase stack (live tests, the scratch reset, bootstrap, anything that writes to it); live test sessions and the scratch reset also serialise on the live-stack lock (§5). One LLM-heavy task at a time runs on the claude-code subscription. A scratch run holds the subscription for its LLM stages, and holds the live stack only while its reset, register, write, place-reconcile and site stages run; its stages before write never touch the stack.
- **Waves.** Agents also share the machine. The orchestrating session dispatches in waves that keep test-heavy agents (implementers, fixers and reviewers that run tests, mutation specs or the query-engine sandbox) to a moderate number at once, because contention slows every test run and causes timing flakes (#249). Waves pace the agents that run at once; there is still no ceiling on open draft PRs.
- **Claiming.** Set the issue's Project Status to _In progress_ and add a dated comment naming the kind of session that took it (local or cloud). Every agent acts as the owner's GitHub account, so the assignee carries no information.
- **Stalls.** A task that stalls goes back to _Ready_ or _Backlog_, with a dated comment saying why.

## 4. From issue to main

One issue, one PR, one reviewable diff. Code issues are size S or M; split anything larger. Size L is only for local work that produces no code diff: scratch runs, the keeper run and reviews of their output.

1. **Isolate.** Work in your own git worktree, never in the owner's checkout. Run `git fetch origin`, then cut a branch named `<issue-number>-<slug>` from `origin/main`.
2. **Stay in scope.** Change only the files and GitHub entities your task names. Docs move with code.
3. **Self-check.** Before the first push for review, work through the pre-review self-check (below). Before every push, `uv run ruff check .` and `uv run ruff format --check .` pass, together with the Markdown check in `make lint`, and the self-review (§7) comes last.
4. **Push only your branch.** Never push to main, force-push, rewrite pushed history or push to another task's branch. Once a PR has merged, its branch is finished: pushing to it would recreate the deleted branch on GitHub, so follow-up work goes on a new branch cut from a fresh origin/main. Check `gh pr view <n> --json state` before pushing to an existing PR's branch.
5. **Open a draft PR** with `gh pr create --draft --base main`, following the PR template: the body starts `Closes #n`, names the exec label and the requirement, and gives the evidence and the self-check. Move the issue's Status to _In review_.
6. **Review.** The PR gets an adversarial review, under the rules in _Reviews_ below, and required fixes are pushed to the same branch. Alongside correctness, scope and real data (§7), the review checks that comments and docstrings in `src/yizkr/` describe how the code works now and carry no development history such as issue or PR numbers; ADR references are fine (CLAUDE.md, Engineering conventions). The verdict names the commit it reviewed; every commit pushed after that, merges of main included, is re-checked by the reviewer, in a bounded re-review (below), before the PR is marked ready.
7. **Mark it ready.** The orchestrating session runs `gh pr ready` once the verdict is in and the fixes are checked. Until then the PR stays a draft. This is the standard for all agentic work: a draft means "not ready to merge".
8. **The owner merges** PRs marked ready, by the method of their choice. Nothing may depend on branch SHAs surviving a merge, so cite issue and PR numbers instead.

**The pre-review self-check.** On 2026-09-27 roughly 40% of agent time went to second and later review or fix rounds (#249). So that a first review finds only what needs a second reader, the implementer works through the reviewer's usual checklist before the first push for review, and records it in the PR template's self-check:

1. **Docs moved with the code.** Every doc the change makes untrue is updated in the same PR (CLAUDE.md, Engineering conventions), except CLAUDE.md itself, which changes only with the owner's approval: the PR proposes its new wording under Owner actions instead.
2. **Each new or changed test fails under the regression it guards.** Show it with the mutation harness, `scripts/mutate.py` (#250, docs/TESTING.md): a mutation that puts the bug back, or takes the behaviour out, is killed by the test. The PR gives the harness's table and the spec it ran. A test whose mutant survives does not guard that regression: fix the test before review. For a new or changed live test, the spec marks its mutation `live = true`, and since live tests run centrally, the PR gives that spec under Mutation evidence and the central live run (§5) runs it.
3. **Edge cases.** The PR lists the edge cases considered, each with the test that covers it or the reason none is needed.
4. **Current with main.** After `git fetch origin`, if `git log --oneline HEAD..origin/main` lists any commit, run `git merge origin/main`: a merge commit, never a rebase, so that no commit a reviewer has seen or a verdict names drops out of the branch. Merge before running the full suite.
5. **Fictional examples only** (§7, docs/EXAMPLES.md), in code, tests, docs, commit messages and the PR text.
6. **Comments and docstrings in `src/yizkr/` describe current behaviour only**: no issue or PR numbers, "previously" notes or bug-hunt accounts; ADR references are fine. `tests/unit/test_comment_standard.py` catches the numbers; narration needs a read.
7. **Targeted tests while iterating, then the full suite once per round.** While iterating, run only the tests that cover the change; run the full `uv run pytest -q` before the push that opens the PR for review, and once per fix round (docs/TESTING.md).
8. **The self-review (§7)**, last.

A fix round runs the same checklist over what its own commits change, with the full suite once before its push.

**Reviews.**

- **A strict required-fix boundary.** A first review requires a fix only for a rule violation (CLAUDE.md, an accepted ADR or this protocol), a correctness bug, a regression, a vacuous or broken test, or an inaccurate claim in the code, docs, commit messages or PR text. Everything else, such as naming, structure, extra coverage or ideas for later, goes in the verdict as a note or a proposed follow-up, and the orchestrating session files the follow-ups as issues. A demand that new code match the speed of what it replaces is a note too, unless the change breaks a real budget, such as the 120 s that the site build and the live tests give each query.
- **Bounded re-review.** After a fix round, the re-review (step 6) covers every commit pushed since the verdict, merges of main included, and judges them under the same boundary: whether the required fixes landed, and any regressions those commits caused. It does not review the PR afresh.
- **CI runs the full suite; the reviewer doesn't run it again.** The reviewer relies on CI's result for the commit under review: `gh pr checks <n>` while that commit is the PR's head (`--watch` waits for a run in progress), or, once it isn't, `gh run list --commit $(git rev-parse <sha>)`, since the filter matches only the full 40-character SHA. It runs targeted tests and its own probes instead: the tests the PR adds or changes, the PR's mutation spec or one of its own, and edge cases the PR doesn't list. A spec with live mutations is the exception: it runs centrally, under the live-stack lock (§5), and a worktree without a configured stack refuses it anyway. If CI fails for that commit, the failure is a required fix. CI never runs live tests; they run centrally (§5).
- **Resume, don't restart.** Where the orchestrating session can, every fix round on a PR resumes the same fixer, and every re-review the same reviewer, rather than starting a new agent, so their context carries over from round to round. Where it can, it keeps the implementer's and the reviewer's worktrees until the PR merges, so that both can be resumed; a workflow harness may remove a reviewer's worktree that holds no changes.

**Hooks.** `make hooks` installs the pre-commit hooks (Ruff for Python and Prettier for Markdown) and the pre-push hook (`scripts/git-hooks/pre-push`). The pre-push hook refuses any push that would create, move or delete main on any remote, including pushes of several refs at once. Hooks live in the common git directory, so one run in the owner's checkout covers every worktree. `make hooks` will not overwrite a pre-push hook that is not yizkr's. The hook is a tripwire, not an access control: `git push --no-verify` skips it, which is for a deliberate owner action only. Agents never use it.

**Templates.** New issues use the forms in `.github/ISSUE_TEMPLATE/` (bug, feature, decision, ingestion pass). An agent that files an issue with `gh issue create` follows the matching form's fields. PRs use `.github/pull_request_template.md`.

## 5. Verification that needs a local session

- **`needs:live-test`** goes on any PR that changes a write path, and on any PR whose mutation evidence has a live spec (a mutation with `live = true`), so that the central live run runs the spec. A local session runs `make test-live` and posts the results on the PR before merge. Live tests run on the one instance, which scratch runs reset (ADR-0025), and hold the live stack while they run (§3). They write fictional fixture data only. The live-test harness clears the recorded instance-contents key as each session starts and as it ends, and any write voids the record anyway, so no scratch run mistakes the instance for one it wrote (#199; docs/runbooks/scratch-run.md, Reset the instance). Once the instance is kept, live tests wait for #159's decision on where they run.
- **Live tests run centrally, as soon as the draft opens.** The orchestrating session runs them for a PR that needs them, or hands the run to one session, in parallel with the first review rather than after the review rounds; no other agent starts live tests of its own. The results name the commit they ran at, and a fix round that changes a write path gets another central run. The run also runs the PR's live mutation spec with the harness, which holds the live-stack lock for it, and posts the table, which shows each new or changed live test failing under the regression it guards (§4, docs/TESTING.md). #227 went through six agents and about 11.5 agent-hours before its first live run, which failed at once: HTTP 414, because the query service refuses a GET request line longer than about 8 KB, so long SPARQL must be POSTed.
- **Live sessions take turns by themselves.** A session takes the live-stack lock (#253, docs/TESTING.md) when its first test that uses the stack sets up, and holds it to the end; a second session waits, saying who holds the lock. No one checks by hand for another live session. Live test sessions, the mutation harness and the scratch reset take the lock; bootstrap and other writers don't, and get the stack from the orchestrating session (§3).
- **`needs:scratch-diff`** goes on any PR that changes a stage before write that already runs (§2). The procedure:
  1. **Run.** A local session runs a whole-corpus scratch run at the PR's head, or at a local, never-pushed merge of several waiting PRs onto current main. Batching is allowed (owner, 2026-09-27). The run starts from fresh state directories, and the LLM cache answers every model call the change leaves the same as in the baseline, the latest completed run on main (ADR-0032). It holds the subscription (§3). In the development stage it may go on through write, resetting the instance and holding the live stack for its write-side stages; once the instance is kept, it stops `--until` the last stage before write.
  2. **Report and diff.** The session runs `yizkr scratch report <run>` and `yizkr scratch diff <baseline> <run>`. The HTML and JSON reports and the detailed diff stay under `var/` (§6).
  3. **Read.** The owner, or a reviewer agent, reads them with the reviewer checklist in docs/RUN_DIFF.md (#198). A reviewer agent that reads anything beyond the aggregate summary must be a local session.
  4. **Post.** The aggregate `report.md`, the diff summary and the verdict go on the PR, and for a batch on each PR in it, with changes attributed per stage and per claim where the diff can. A PR whose effect stays unclear gets a run of its own.
  5. **Fix.** A regression is fixed in the pipeline on the PR's branch and run again. Nothing is repaired in the graph.

  Until the first whole-corpus scratch run (#145) starts, the pre-baseline exception applies (§2): a PR that changes a stage carries its `stage:*` label but not this one, and the first run's baseline includes the change. The scratch driver, the report and the diff (#194, #124, #198) make the evidence: `yizkr scratch run`, `yizkr scratch report` and `yizkr scratch diff`.

- While a PR waits for this evidence, its Project Status is _Needs local verify_. Cloud agents never provide the evidence; they leave the label for a local session.

## 6. The data boundary

**Local-only data.** Only local sessions read these, and nothing from them is copied into the repository, GitHub or a cloud agent's session. Any issue whose work reads them is `exec:local-agent`:

- the corpus: `import/`, or the directory `YIZKR_CORPUS_DIR` names once #29 moves it out of the tree;
- `var/` and `bundle/`. `var/` holds the LLM cache (model responses and session transcripts), run directories (events, alerts, control files, document views), live status pages, `report.html` and `report.json`, detailed run diffs, batches, review sheets, galleries and plan state. Only a run's aggregate `report.md` and diff summary may leave the machine (§7);
- `~/yizkr-private/` and everything in it: the archives, the private audit inventory, and private experiment outputs;
- backups (`yizkr backup` output and stack archives) and the exclusion list;
- the record of the instance (`~/.local/state/yizkr/instances`, or `$YIZKR_INSTANCE_STATE`): the instance-contents record, the host-side kept-state marker, where the stack runs from, the write stage's state, and what each scratch reset set aside, the wiki's `LocalSettings.php` among it;
- any live or dev graph: the Wikibase stack, its query service, and their volumes and dumps.

Cloud agents see the repository and its fictional fixtures, nothing else. (Sending corpus content to a model during ingestion is a separate matter, governed by CLAUDE.md rule 7.)

**The cloud-dispatch gate (#34).** No cloud agent is dispatched until the owner closes #34. Every `exec:cloud-agent-ok` issue reaches #34 through blocked-by links, directly or transitively. When you file a new `exec:cloud-agent-ok` issue, link it to #34 or to an issue that is already blocked by it. The #34 edge gates cloud dispatch only; it does not stop a local session (§3).

**Never ingest what is excluded (fail closed).**

- A document on the exclusion list is never ingested: not registered, transcribed, extracted or sent to a model.
- Every ingestion entry point calls the one shared exclusion check (#29). Production and extract-all modes refuse to run when no exclusion list is configured. Never work around the refusal.
- The owner keeps the list, outside the repository. If a document looks as if it should be excluded, stop and report it; never ingest it to find out.

## 7. Real data in anything that leaves the machine

**Examples** in code, comments, prompts, tests, docs, commit messages, issues, PRs and comments come only from the fictional universe (docs/EXAMPLES.md). Never use real family data, not even partially anonymised: no real name with a changed surname, no real date shifted by a year, no real town, address or firm, and no invented document that mirrors a real one by kind and date. If an example needs a pattern the universe lacks, add it to the universe first.

**Never quote, paraphrase or identify corpus content** outside the private area.

**GitHub text is pseudonymous.**

- People are cited by QID only.
- Artifacts are cited by QID; by slug only when the slug holds no personal name; otherwise by SHA-1 prefix plus a generic descriptor ("`3f2a9c1`, a typed letter").
- No real names or dates of family members.
- Metrics are aggregates only: counts and rates, never rows about one person or one document.

**Self-review before anything leaves the machine.** A session that has handled real data, before every push and every post:

1. re-reads the whole commit range being pushed, messages and diffs (`git log -p origin/main..HEAD`);
2. re-reads every issue, PR and comment text it is about to post;
3. confirms that none of it quotes, paraphrases or identifies anything from the corpus, that it contains no plan-internal codes, and that issues are cited by #number;
4. records that it did so, in the PR's self-check or its report.

If real data turns up in a commit that is not yet pushed, fix the commit before pushing. If it is already pushed or posted, stop and tell the owner. Don't force-push, delete or edit it yourself: an edit leaves the old text in GitHub's revision history, so scrubbing is the owner's call.

Enforcement is by this convention and self-review, not by a token denylist; ADR-0024 §5 says why.

## 8. Decision records (ADRs)

- Claim the next free ADR number in the pinned registry (#19) before cutting a branch.
- Drafts merge with status `proposed`, and the owner accepts them in batched sessions (#72, #74). The issue that drafts an ADR closes when the draft merges. Open decision clauses live in the session issues, not in the drafts. An ADR that records decisions the owner has already made (ADR-0024, ADR-0025) is accepted by the owner's merge instead.
- Small changes amend an ADR in place, with a dated "Amended by #n" line in its status header. Reversing a ruling always takes a new ADR.
- Cite decisions by ADR number everywhere, and keep the registry's status column current.

## 9. The owner's attention

- Group everything that needs the owner into touchpoints (the Project's Owner touchpoint field), each with a time estimate. The budget is about four per iteration, plus two fixed PR-review slots a week.
- A report or PR lists the owner's actions in one place, each with what to run or decide.

## 10. Scaffolding scripts

Scripts that create or edit labels, milestones, Project fields or issues must be safe to re-run. They pause at least 1 s between GitHub mutations, write a resumable checkpoint (plan key to node id) after each step, and self-review every title and body for real data before sending it.
