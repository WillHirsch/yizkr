# ADR-0024: Planning and supervised agent execution

Status: **accepted**. This records the current rationale for issue-based work, owner merges and the private-data boundary, including the owner-approved policy in #707. The authoritative rules are the [current contract](../CONTRACT.md), [agent protocol](../AGENT_PROTOCOL.md) and [testing policy](../TESTING.md). Consult those documents for operating instructions; this ADR explains the boundaries they protect.

## Context

Parallel agent work needs explicit ownership, reviewable changes and coordination of shared resources. Historical family records also require a boundary between repository work using fictional fixtures and sessions authorized to handle real records. Neither a large rulebook nor a passing test suite substitutes for those controls.

## Decision

### 1. The plan lives in GitHub-native entities

Issues describe the current task; parent issues, native blockers, milestones and Project fields express scope, dependencies and status. Cite issues by number, requirements by R-number and current policy or design by its relevant document. Keep issue bodies accurate when scope changes.

Requirements describe user outcomes. The current contract distinguishes those outcomes from implementation choices and links to relevant rationale. A concrete conflict calls for an explicit proposal and decision, not compensating infrastructure or a silent exception.

### 2. The owner merges every PR; agent PRs stay drafts until reviewed

Repository changes proceed through an issue, an isolated branch, self-check, draft PR and review. The owner merges. An agent PR becomes ready only after review and its required fixes; nobody pushes directly to main. Local hooks help catch mistakes but are not access control.

Each change should be reviewable on its own. Verification follows the affected behavior and risk, as specified in [TESTING.md](../TESTING.md): meaningful writer/query checks for persistence boundaries, focused semantic checks for local behavior, and whole-corpus evidence when cross-document risk warrants it. A pre-write file change does not itself require a corpus run, and an edited test does not itself require mutation testing.

### 3. Dispatch: a standing go-ahead, limited by non-interference

The [agent protocol](../AGENT_PROTOCOL.md) governs dispatch, execution labels, ownership and review. Coordinate overlapping edits and shared resources before dispatching independent work. The corpus instance, model subscription and host capacity are finite; use the existing locks and avoid overlapping expensive work on the same resource. A large number of open draft PRs is not itself evidence of interference.

Parked work and owner decisions are not agent implementation tasks. Batch genuine owner decisions into concrete, reviewable touchpoints. Authorization for repository work does not authorize access to private inputs or a destructive operation on a retained archive.

### 4. The data boundary is structural

Corpus inputs, corpus-run state and caches, private archives, backups, exclusion lists, detailed reports and real-data graphs remain local-only. Only sessions authorized for that data may read them; they are not copied into repository changes, GitHub text or cloud sessions. Only aggregate report and diff summaries that satisfy the disclosure rules may leave the private area.

Repository fictional fixtures are different: an appropriately configured cloud session may use a separately isolated fictional Wikibase/query instance under the execution controls. That permission does not extend to the archive or corpus-run deployment. The repeatable integration lane is tracked in #722; its approval does not mean it is already implemented.

Never-ingest exclusions remain a fail-closed boundary. Sending source content to a model is subject to the owner's processing authorization, independently of where orchestration runs.

### 5. Real data never leaves the private area

Examples in code, prompts, tests, documentation and GitHub text come from the [fictional universe](../EXAMPLES.md), never real family data or a disguised copy of it. A session that has handled private data reviews its outgoing changes and text before sending them. References to private work follow the protocol's pseudonymous, aggregate-only rules.

Fictional fixtures and contextual review protect more than a token denylist can: an identifying relationship or document pattern can leak without containing a listed name. Existing private repository history is not permission to add new real-data examples; any history cleanup or publication remains a separate owner decision.

### 6. Decision records

Start with the requirements and current contract, then consult the model and ADRs relevant to the behavior being changed. Significant architectural decisions need concise rationale and explicit owner acceptance where required. Routine implementation choices do not each need a new ADR.

Keep current documentation internally consistent and describe implemented behavior separately from agreed but unimplemented policy. Record remaining consolidation work in an issue-linked TODO rather than requiring readers to reconstruct the current rules from amendment logs. Issue and PR history supplies the chronology.

### 7. Planning principles

- Prefer a bounded change that establishes a useful outcome; do not make completion of a general framework or every adapter a prerequisite for a narrow working path.
- Preserve human decisions and evidence during reprocessing. Disposable rebuilds and retained ingestion are permanent modes, not phases separated by the end of extraction development ([ADR-0025](0025-development-by-scratch-runs.md)).
- Retained readiness is demonstrated for supported workflows, including safe retries, correction/recovery, privacy controls and workable backups/restore. The owner separately authorizes the first retained dataset under #159; convergence is diagnostic, not a prerequisite.
- Keep owner-made work that cannot be regenerated recoverable. Disposable graphs may be regenerated, but that does not make human decisions or the retained archive disposable.
- Planning automation remains idempotent, resumable and reviewable. Coordinate and throttle external mutations under the protocol rather than duplicating task state in local planning files.

## Consequences

The owner reviews concrete changes and controls consequential decisions. Agents can make progress independently within their authorized scope, while private-data access and shared-resource use remain explicit. Verification can focus on meaningful risks without turning every edit into a corpus experiment or a test-framework project.
