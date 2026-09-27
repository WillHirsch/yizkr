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
- needs:live-test: `make test-live` results from a local session
- needs:eval: metrics against the docs/EVAL.md gate from a local session
-->

## Checklist

- [ ] **No real data (self-reviewed).** I re-read the whole commit range
  (`git log -p origin/main..HEAD`) and this text. Nothing quotes,
  paraphrases or identifies corpus content, and every example comes from
  the fictional universe.
- [ ] Issues are cited by #number and decisions by ADR number; no
  plan-internal codes.
- [ ] Docs move with code (ADRs, MODELING.md, CLAUDE.md, READMEs).
- [ ] Owner actions, if any, are listed in one place above.
