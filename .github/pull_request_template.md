Closes #

<!--
Agent PRs open as drafts (`gh pr create --draft --base main`) and are
marked ready only after the adversarial review passes and its fixes land
(docs/AGENT_PROTOCOL.md §4). One issue, one PR, one reviewable diff.
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
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`
- needs:live-test: `make test-live` results from a local session on the one
  instance, and how the session cleared the instance-contents key
  (docs/AGENT_PROTOCOL.md §5)
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

## Checklist

- [ ] **No real data (self-reviewed).** I re-read the whole commit range
  (`git log -p origin/main..HEAD`) and this text. Nothing quotes,
  paraphrases or identifies corpus content, and every example comes from
  the fictional universe.
- [ ] Issues are cited by #number and decisions by ADR number; no
  plan-internal codes.
- [ ] Comments and docstrings in `src/yizkr/` describe how the code
  works now, with no development history (no issue or PR numbers,
  "previously" notes or bug-hunt accounts); ADR references are fine.
- [ ] **Stages.** This PR changes no stage before write, or only in ways
  that cannot change its outputs (a comment, a rename, a refactor). Or it
  can change them (its code, a prompt, the manifest, a model, effort or
  parameters), and this PR and its issue carry the matching `stage:*`
  label, plus `needs:scratch-diff` if the stage already runs.
- [ ] Docs move with code (ADRs, MODELING.md, CLAUDE.md, READMEs).
- [ ] Owner actions, if any, are listed in one place above.
