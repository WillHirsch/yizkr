Closes #

<!--
Open agent PRs as drafts; mark ready after review and required fixes.
Follow docs/AGENT_PROTOCOL.md. Use fictional examples only. Never include
corpus content; real-data references use QIDs and aggregate metrics.
-->

**Exec label:** <!-- exec:local-agent | exec:cloud-agent-ok | exec:owner -->  
**Requirement:** <!-- R1–R6, privacy, or none: infrastructure/governance -->

## Change

<!-- The problem, resulting behavior and material limitations. -->

## Verification

<!--
Give relevant commands/results and explain what they establish. Use the
risk-based guidance in docs/TESTING.md. Rely on CI's broad offline result
for the reviewed revision; no duplicate full local run is required merely
because a push or fix round occurred. Report meaningful gaps or failures.
Mutation evidence is optional, not a required table for every edited test.
For real-boundary checks, name the tested commit and intended tests that
actually ran. An all-skipped run is not integration evidence.
-->

<!--
When needs:scratch-diff applies, include the reason bounded evidence was
insufficient, tested commit and baseline, stages, any batched PRs, aggregate
report/diff summary and verdict. Detailed corpus reports remain local-only.
Otherwise omit corpus-run evidence; changing a pre-write file alone does
not require it.
-->

## Owner actions

<!-- Required decisions or commands in one place, or "none". Identify any
CLAUDE.md wording that still needs approval; do not request it again when
already authorized. The owner merges after review. -->

## Self-review

- [ ] The diff stays in scope; affected current documentation is accurate.
- [ ] Relevant behavior and edge cases have evidence, with limitations reported above.
- [ ] Examples are fictional; outgoing text and commits disclose no private corpus content.
- [ ] Required owner actions are listed above, and existing approvals are identified.
