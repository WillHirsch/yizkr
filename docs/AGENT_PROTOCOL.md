# Agent protocol

How work moves from an issue to main, and how private data stays within its authorized environment. This protocol applies to sessions that change the repository or its GitHub issues and PRs. Start with [REQUIREMENTS.md](REQUIREMENTS.md) and [CONTRACT.md](CONTRACT.md); [TESTING.md](TESTING.md) explains verification and the available tools.

## 1. Plan and cite in GitHub terms

- The plan lives in GitHub issues, native sub-issues and blocked-by links, milestones, labels, and the Project's Status, Size, Owner touchpoint and Iteration fields. Cite issues and PRs by `#number`, requirements by R-number, and current contracts by document link. Do not copy planning-file internal keys into the repository or GitHub.
- **Keep issue bodies true.** When scope or a fact changes, correct the body and add a dated comment explaining the change. An agent edits only the issues its task names and reports stale bodies elsewhere to its orchestrator.
- Scope additional work in the relevant existing issue or a new issue. An ordinary implementation choice or bounded fix does not require reopening a wider policy decision. Block only work that depends on the unresolved decision.

## 2. Labels

| Label | Meaning |
| --- | --- |
| `exec:local-agent` | Reads the corpus, private runtime state or a corpus-derived graph (§6) |
| `exec:cloud-agent-ok` | Repository work or an isolated fictional test instance; subject to the cloud-dispatch gate (§6) |
| `exec:owner` | An owner action or decision; never dispatched |
| `needs:live-test` | Requires evidence from the affected real writer/query boundary before merge (§5) |
| `needs:scratch-diff` | Requires whole-corpus evidence for a stated risk; several PRs may share a run (§5) |
| `stage:*` | Identifies stages whose outputs can change; does not itself require a corpus run |
| `area:*` | The part of the repository the issue edits; used by the conflict rule (§3) |
| `kind:*` | The kind of work, such as bug, feature, decision, experiment, chore, ingestion-pass, docs, research or review |
| `req:*` | The requirements an epic serves; on epics only |
| `parked` | Deferred until the trigger in its body fires; no milestone; never dispatched |
| `gate:cloud-dispatch` | Marks #34 |
| `epic` | A parent issue grouping sub-issues |

Use the matching `stage:*` labels for changes that can affect stage outputs, including prompts or model configuration. Choose verification from the risk and affected boundary, not the label or file extension. Record why a `needs:scratch-diff` run is necessary and which stages it must cover.

## 3. Dispatch and claiming

- **Ready.** Dispatch ready issues without another permission request when their native blockers are closed. The #34 edge gates cloud dispatch only: a local session may take an issue whose only open blocker is #34. Parked and `exec:owner` issues are never dispatched.
- **Conflict rule.** At most one in-flight repository-editing issue per `area:*` label; an issue with several area labels holds each. Read-only audits do not hold an editing area.
- **Shared resources.** Coordinate access to each stack. Live tests, the mutation harness and scratch reset use the live-stack lock; bootstrap and other writers also need orchestration because they do not all acquire it. One LLM-heavy task at a time uses the claude-code subscription. Pace concurrent test-heavy agents to avoid contention; there is no separate ceiling on open draft PRs.
- **Claiming.** Set the issue's Project Status to _In progress_ and add a dated comment naming the session kind. If work stalls, return it to _Ready_ or _Backlog_ with the reason.

## 4. From issue to main

One issue, one PR, one reviewable diff. Code issues are size S or M; split larger work. Size L is for local work without a code diff, such as corpus runs and their review.

1. **Isolate.** Use a worktree separate from the owner's checkout. Fetch origin and create `<issue-number>-<slug>` from `origin/main`.
2. **Stay in scope.** Change only the files and GitHub entities the task authorizes. Update documentation that the change makes inaccurate. CLAUDE.md changes require owner approval; an existing approval of the wording is sufficient.
3. **Verify and self-review.** Use the relevant evidence in [TESTING.md](TESTING.md#verification-by-risk). Check formatting for the changed files, inspect the diff, and complete the privacy self-review in §7 when applicable. Use CI's broad offline result for the revision under review; a push or fix round alone does not require a duplicate full local run.
4. **Keep the branch current.** Fetch before review. If main has moved, merge `origin/main` rather than rebasing pushed history. Recheck the behavior affected by the merge.
5. **Push only the task branch.** Never push to main, force-push, rewrite pushed history or push to another task's branch. Check an existing PR's state before pushing. After it merges, follow-up work uses a fresh branch.
6. **Open a draft PR.** Use the PR template, start the body with `Closes #n`, identify the exec label and requirement, and report relevant evidence and limitations. Move the issue to _In review_.
7. **Review and fix.** The reviewer names the commit reviewed. Every subsequent commit, merges included, receives a bounded re-review of its changes and the required fixes before the PR becomes ready.
8. **Ready and merge.** The orchestrator marks the PR ready after review and fixes are complete. The owner merges. Cite issues and PRs rather than relying on branch SHAs surviving the merge.

**Review scope.** Required fixes address a current contract or permission violation, a correctness bug, a regression, a broken or vacuous test, or an inaccurate claim. Other suggestions are advisory or separately scoped work. Performance is a required fix when evidence shows a relevant budget is broken, not merely because an implementation differs. Re-review covers the new commits and their consequences rather than restarting the review. Reuse the implementer and reviewer sessions where practical.

**Verification in review.** Required checks must pass for the revision under review. Read CI's result, then run focused probes where they resolve a concern. Mutation testing is optional; no mutation table or spec is required for every edited test. Required integration evidence must exercise the intended checks: a passing command with those checks skipped is not sufficient. Record remaining limits rather than claiming a fake writer or canned model exercised the real boundary.

**Hooks and templates.** `make hooks` installs Ruff/Prettier pre-commit hooks and the pre-push hook that refuses changes to main. Hooks are shared across worktrees and are a tripwire, not access control; agents never bypass them with `--no-verify`. New issues follow `.github/ISSUE_TEMPLATE/`, including when filed through a CLI. PRs use `.github/pull_request_template.md`.

## 5. Integration and corpus evidence

**Real boundaries.** Writer, query, migration and persistent-identity changes need repeatable fictional checks against the affected real boundary where unit tests cannot establish the behavior. Apply `needs:live-test` when that evidence is required and report the tested commit, command, relevant checks and result. Arrange central runs early enough to inform implementation and review; repeat them when a subsequent change affects their conclusion.

**Isolated integration target.** The dedicated fictional Wikibase/WDQS lane is not implemented; #722 tracks it. It must use separate volumes, explicit connection settings, instance-state records and locks, without inheriting archive credentials, mounts or connection defaults. A required invocation must fail when its intended checks cannot run. The lane may run centrally or in CI, subject to §6. It must be runnable and demonstrated before #159 retains the currently shared instance.

Until that lane is available, centralized live checks may supply evidence on an explicitly disposable target. A persistence-boundary change waits when its necessary evidence cannot be obtained; unrelated work need not wait. Test runs, mutants and development resets must not mutate retained data. The existing live fixtures can skip when configuration or the stack is missing, so verify which checks actually ran. [TESTING.md](TESTING.md#live-integration-and-the-transition) describes the current harness and its limits.

**Corpus evidence.** Require a whole-corpus run when risk depends on cross-document interactions or remains materially uncertain after bounded evidence, such as broad identity/source-boundary changes, an unexplained corpus regression or keeper readiness. A changed pre-write file, prompt or model alone does not impose this requirement. Small semantic acceptance checks and bounded observed model outputs are permitted alongside corpus review.

When whole-corpus evidence is needed:

1. A local session records the reason, affected stages, tested revision and baseline. Several related PRs may share a local, unpushed integration merge; attribute the result and separate a PR whose effect remains unclear.
2. Run the affected stages and necessary downstream work, reusing compatible model responses. Stop before writes when graph effects are irrelevant. Any write-side run uses an explicitly disposable target; retaining another dataset does not authorize resetting it.
3. Generate `yizkr scratch report <run>` and `yizkr scratch diff <baseline> <run>`. Review using [RUN_DIFF.md](RUN_DIFF.md). Detailed reports and diffs remain local-only.
4. Post only the aggregate report, diff summary and verdict. Correct pipeline regressions in code and regenerate affected outputs; do not conceal them by repairing the disposable graph.

Use _Needs local verify_ while a PR awaits local-only evidence. Cloud sessions may supply evidence from permitted fictional fixtures, but never read corpus reports to supply the local review.

## 6. The data boundary

A _local session_ runs on the owner's machine and may read authorized private data. A _cloud session_ sees repository material and, under the exception below, an isolated fictional test instance.

**Local-only data.** Only local sessions read these; never copy their contents into the repository, GitHub or a cloud session:

- The corpus in `import/` or the configured `YIZKR_CORPUS_DIR`.
- Private runtime state in `var/` and `bundle/`: caches, model responses and transcripts, runs, document views, status pages, detailed reports/diffs, review sheets, galleries and plan state. Only an aggregate report and diff summary may leave the machine, subject to §7.
- `~/yizkr-private/`, private archives, audit inventories and experiment outputs, backups and the exclusion list.
- Archive or corpus-run instance records, including `~/.local/state/yizkr/instances` or `$YIZKR_INSTANCE_STATE`, retained-state markers, connection details and reset backups.
- Archive and corpus-derived graphs, query services, volumes and dumps.

**Fictional-instance exception.** An isolated test instance populated exclusively from repository fictional fixtures may run in a cloud session under the existing dispatch and connection controls. Its state and outputs must remain separate from archive and corpus-run state. It must not inherit archive credentials, mounts or connection defaults. This exception does not permit a cloud session to inspect a real development graph because a particular test writes fictional rows into it. Sending personal data to a model requires the owner's separate authorization under [CONTRACT.md](CONTRACT.md).

**Cloud-dispatch gate.** No cloud agent is dispatched until the owner closes #34. A new `exec:cloud-agent-ok` issue links to #34 directly or through a blocker already gated by it. This edge does not block local work.

**Exclusions fail closed.** Never register, transcribe, extract or send an excluded document to a model. Ingestion entry points use the shared exclusion check; production and extract-all modes refuse missing exclusion configuration. Do not bypass that refusal. The owner maintains the list outside the repository. If a document appears to require exclusion, stop and report it rather than ingesting it to investigate.

## 7. Real data in anything that leaves the machine

Examples in code, prompts, tests, documentation, commit messages and GitHub text come from [EXAMPLES.md](EXAMPLES.md) and the fictional fixtures. Never partially anonymize real family data. Add a missing fictional pattern to the example universe before using it.

Never quote, paraphrase or identify corpus content outside the private area. In GitHub text, cite people by QID; artifacts by QID, a slug containing no personal name, or SHA-1 prefix plus a generic descriptor. Give metrics as aggregates, not rows about one person or document. Do not include real names or dates of family members.

A session that has handled real data re-reads the entire commit range it will push, including messages, and every issue, PR and comment text it will post. Confirm that none exposes corpus content or internal planning codes, and record that check in the PR or report. Review aggregate reports for the same boundary.

If private data appears in an unpushed commit, remove it before pushing. If it has been pushed or posted, stop and tell the owner. Do not force-push, delete or edit it yourself: removing it from revision history is an owner action.

## 8. Current documentation

Repository documentation describes the current contract, implemented behavior, active limitations and the rationale needed to understand them. Architectural changes update the relevant current documents; they do not require a new ADR or a supersession history. Historical discussion belongs in Git history, issues and PRs.

Document active compatibility paths and migrations while supported. Deprecated material may remain temporarily only with an open removal TODO naming its issue and a concrete removal condition. Update affected links when removing it. A separately tracked documentation cleanup does not block an unrelated bounded fix.

## 9. The owner's attention

Group owner actions into Project touchpoints with time estimates. A report or PR lists all required owner decisions and commands in one place. Existing authorization persists; do not ask again for an action the owner has already approved.

## 10. Scaffolding scripts

Scripts that create or edit labels, milestones, Project fields or issues must be safe to rerun. Pause at least one second between GitHub mutations, keep a resumable checkpoint after each step, and review every outgoing title and body for private data.
