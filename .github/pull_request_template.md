Closes #

<!--
Agent PRs open as drafts (`gh pr create --draft --base main`) and are
marked ready only after the adversarial review passes and its fixes land
(docs/AGENT_PROTOCOL.md §4). Work through the self-check at the end
before the first push for review. One issue, one PR, one reviewable diff.
Examples come only from the fictional universe (docs/EXAMPLES.md). Cite
people by QID, and give metrics as aggregates only.
-->

**Exec label:** <!-- exec:local-agent | exec:cloud-agent-ok | exec:owner -->
**Requirement:** <!-- R1 to R6, R7-privacy, or "none: infrastructure/governance" -->

## Summary

<!-- What changed and why, in a few lines. -->

## Evidence

<!--
Commands run and their results, aggregates only:
- `uv run ruff check .`, `uv run ruff format --check .`, and the full
  `uv run pytest -q` once, before the final push (docs/TESTING.md)
- needs:live-test: `make test-live` results from the central live run,
  started when the draft opened, and how it cleared the instance-contents
  key (docs/AGENT_PROTOCOL.md §5)
-->

**Mutation evidence:**
<!--
For each new or changed test: the regression it guards, and the Markdown
table from `uv run python scripts/mutate.py SPEC` showing its mutant
killed, with the TOML spec in a collapsed <details> block so a reviewer
can re-run it (docs/TESTING.md). Explain any survivor. Or write "no new
or changed tests".
-->

**Edge cases:**
<!--
The edge cases considered, each with the test that covers it or the
reason none is needed.
-->

## Scratch-diff evidence

<!--
For needs:scratch-diff PRs only (docs/AGENT_PROTOCOL.md §5, ADR-0025). A
local session fills this in; a cloud agent leaves it for one. Aggregates
only: never paste report.html, report.json or the detailed diff.
- Scratch run: its run id and commit, and the other PRs it batched, if any
- Baseline: the latest completed run on main it diffs against, and whose
  cached model calls it reuses
- Stages whose outputs changed, each with the call components that
  changed (the cache entries keep them; ADR-0032)
- The aggregate report.md and the diff summary
- Verdict: who read it (the owner, or a reviewer agent in a local session),
  and whether any change is a regression; a PR whose effect stays unclear
  in a batch gets a run of its own
Write "not applicable" if the PR has no needs:scratch-diff label.
-->

## Self-check

<!--
Worked through before the first push for review: the reviewer's usual
checklist (docs/AGENT_PROTOCOL.md §4). A fix round runs it again over
what its own commits change.
-->

- [ ] Docs move with code (ADRs, MODELING.md, READMEs, docs/). A
  CLAUDE.md change needs the owner's approval, so its proposed wording
  goes under owner actions, not into the diff.
- [ ] **Tests fail under their regressions.** Each new or changed test
  is shown, with `scripts/mutate.py`, to fail under the regression it
  guards (Mutation evidence above).
- [ ] The edge cases considered are listed above.
- [ ] **Current with main.** If origin/main had moved on, it is merged
  in: a merge commit, never a rebase.
- [ ] Every example comes from the fictional universe
  (docs/EXAMPLES.md), in code, tests, docs, commit messages and this
  text.
- [ ] Comments and docstrings in `src/yizkr/` describe how the code
  works now, with no development history (no issue or PR numbers,
  "previously" notes or bug-hunt accounts); ADR references are fine.
- [ ] **Stages.** This PR changes no stage before write, or only in ways
  that cannot change its outputs (a comment, a rename, a refactor). Or it
  can change them (its code, a prompt, the manifest, a model, effort or
  parameters), and this PR and its issue carry the matching `stage:*`
  label, plus `needs:scratch-diff` if the stage already runs. Until the
  first whole-corpus scratch run exists (#145), no PR carries
  `needs:scratch-diff` (the pre-baseline exception,
  docs/AGENT_PROTOCOL.md §2).
- [ ] **Test runs.** Targeted tests while iterating, then the full
  `uv run pytest -q` once, before the final push.
- [ ] **No real data (self-reviewed, docs/AGENT_PROTOCOL.md §7).** I
  re-read the whole commit range (`git log -p origin/main..HEAD`) and
  this text. Nothing quotes, paraphrases or identifies corpus content.
  Issues are cited by #number and decisions by ADR number, with no
  plan-internal codes.
- [ ] Owner actions, if any, are listed in one place above.
