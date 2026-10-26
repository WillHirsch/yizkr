# Current project contract

[REQUIREMENTS](REQUIREMENTS.md) defines the user outcomes. This document defines cross-cutting policy; [MODELING](MODELING.md) records implemented semantics, and focused architecture guides and runbooks describe supported operations. Read the relevant documents for a change, not the entire ADR collection. A policy stated here is not evidence that its implementation exists. [The ingestion and identity contract](INGESTION_CONTRACT.md) specifies source ownership, durable assignments, scoped approvals and shared stage boundaries for the implementing tasks.

## Evidence, interpretations and correction

Historical assertions carry source provenance and the most reliable available location. Machine interpretations also identify their process. Derived conclusions identify their inputs and method; personal attestations identify their curator and context. Schema and run bookkeeping need appropriate origin and versioning, not invented citations to historical artifacts.

Preserve genuine source disagreement, including within one source. Machine alternatives may remain after review as labeled interpretations; they are not independent witnesses. The presence of an assertion does not establish its truth, its subject's identity or permission to disclose it. Views must work without a canonical value and make disagreement visible, while allowing honest summaries with access to attributed details.

The target correction contract permits audited correction, retraction or supersession of machine errors, duplicates and misattribution while preserving evidence and recoverable history. It does not make every erroneous machine statement permanently authoritative. Existing retained-state correction, identity and disclosure guards remain until supported replacements are verified: [the ingestion contract](INGESTION_CONTRACT.md), [#130](https://github.com/WillHirsch/yizkr/issues/130), [#142](https://github.com/WillHirsch/yizkr/issues/142). This policy authorizes no destructive operation on a retained graph.

## Human authority and privacy

Retained writes require bounded, inspectable approval. Identity assignments and changes, merges, splits and face associations require affirmative confirmation of what is proposed. A batch may confirm several explicitly shown decisions; a general run approval cannot silently confirm faces. Preserve approvals for unchanged results. New corroborating evidence alone does not invalidate an approval; material changes to content, attribution, identity assignment or its basis require scoped review. [The ingestion contract](INGESTION_CONTRACT.md#decisions-and-approval-carry-forward) defines the change and approval boundaries.

Keep archive data private by default. Publication and access by additional audiences require deliberate authorization. Existing privacy predicates and cutoffs remain until their own approved replacements are implemented; a non-privileged export is not a public-safe archive. Privacy decisions in [#70](https://github.com/WillHirsch/yizkr/issues/70) and [#72](https://github.com/WillHirsch/yizkr/issues/72) do not block unrelated cleanup. The [agent data boundary](AGENT_PROTOCOL.md#6-the-data-boundary) governs development access.

## Rebuild and retained operation

Disposable rebuilds and retained-database ingestion are permanent modes. A scratch run may write unattended, unratified development proposals to an explicitly disposable instance. It must not write face/depicts associations, authorize disclosure or make the database retained implicitly. Retained ingestion must preserve persistent identities, human edits and approvals, and refuse destructive reset.

The current whole-corpus driver and map-bound writers do not establish a complete incremental or retained workflow. The [ingestion contract](INGESTION_CONTRACT.md) defines stage and identity boundaries; durable assignments, incremental writes and correction remain work under [#130](https://github.com/WillHirsch/yizkr/issues/130), [#723](https://github.com/WillHirsch/yizkr/issues/723) and [#142](https://github.com/WillHirsch/yizkr/issues/142). Operational instructions must describe the supported path and its limits, not present those tasks as completed features.

Keeper readiness depends on safe supported workflows, bounded review, retries, correction and recovery, privacy and backups. It does not require a fixed number of unchanged extraction runs. The owner still authorizes the actual keeper transition in [#159](https://github.com/WillHirsch/yizkr/issues/159).

Before the shared instance becomes retained, [#722](https://github.com/WillHirsch/yizkr/issues/722) must provide a repeatable, isolated Wikibase/WDQS integration lane using repository-fictional data. It needs separate settings, volumes, state and locks, and its mandatory invocation must fail when infrastructure is unavailable rather than pass with all tests skipped. A genuinely isolated fictional deployment may run in CI or a cloud session under the dispatch gate, without archive credentials, mounts or implicit archive defaults. Corpus-derived graphs remain local-only.

Until that lane exists, checks against the shared instance use only the explicitly designated disposable target and the current local lock. Missing integration evidence blocks affected persistence changes; it does not block unrelated documentation or offline work. Never reset a retained instance to run tests.

## Implementation boundaries and freedom

- **Identity:** distinguish source bytes, placements, source parts, readings, mentions and persistent entities. Labels, filenames and cluster snapshots are not durable entity identifiers. [The ingestion contract](INGESTION_CONTRACT.md#source-identity-and-attribution) specifies their identities and durability; common container handling is developed in [#714](https://github.com/WillHirsch/yizkr/issues/714)–[#716](https://github.com/WillHirsch/yizkr/issues/716).
- **Grounding:** source reading and extraction remain grounded in the source. Reconciliation and planning may use an explicitly recorded read-only graph snapshot or projection, with relevant write preconditions checked before mutation. That permission is not an implemented snapshot-aware incremental writer; implementation belongs to the contract and retained-ingestion tasks.
- **Reuse:** persisted document products, review and decisions must remain usable without the model-response cache. A response key represents the actual inputs and configuration; conversation reuse must account for its opening answer and context. Session ownership and cancellation are explicit. Current cache/session limitations are addressed in [#726](https://github.com/WillHirsch/yizkr/issues/726) and [#724](https://github.com/WillHirsch/yizkr/issues/724).
- **Platform:** Wikibase and the Python package remain the current implementation. Maintain one authority for accepted assertions; this does not require all media, operational metadata or proposals to live in the graph. No platform replacement or general-purpose framework is required by this contract.

## Proportionate verification

Choose evidence for the changed behavior and plausible failure modes. Use focused tests while iterating and broad offline CI for the reviewed revision. Repeat or broaden local testing when failures or unresolved risk justify it, not automatically after every push. Documentation and mechanical changes need relevant formatting, link and consistency checks; Markdown prompts and queries are behavior resources, so assess their actual effects.

Keep meaningful safeguards. For refactors, exercise affected existing callers and tests; add cases for actual coverage gaps. Parser and schema changes need targeted valid and rejected fictional cases. Write and query changes need checks at the real boundary. Privacy, exclusions and review gates need positive and negative cases at their enforcement boundary. Concurrency checks should exercise controlled threads or processes and locks; scheduling may use a fake clock. Performance claims need measurements, not merely a generous timeout.

Mutation testing is selective and optional. A valid negative control or reproduced failure can demonstrate that a regression test discriminates; a setup failure cannot. New test cases do not each require a mutation table.

The [small deterministic semantic acceptance set](TESTING.md#semantic-acceptance) complements corpus evidence; it validates selected contracts without real-model evaluation. Canned model responses demonstrate wiring, not extraction quality. Material prompt, model or inference changes need schema/wiring checks and observed outputs from a bounded representative sample or suitable existing corpus evidence, with configuration recorded. A purely mechanical edit does not automatically require a model call.

Use whole-corpus evidence where corpus interactions, material uncertainty, unexplained regressions or keeper readiness warrant it. Apply `needs:scratch-diff` with a reason and the relevant `stage:*` label; it is not automatic for every change before write. Reuse compatible responses and run only necessary downstream work; stop before writes when graph effects are irrelevant. Several related changes may share a run. Brittle-test replacement and CI simplification remain scoped work in [#727](https://github.com/WillHirsch/yizkr/issues/727) and [#729](https://github.com/WillHirsch/yizkr/issues/729), not prerequisites for all development. [TESTING](TESTING.md) supplies commands and current limitations.

## Current-state documentation

Keep the repository's current tree limited to current goals, contract, supported behavior, limitations and necessary present-tense rationale. Development chronology, abandoned designs, amendment logs and supersession history belong in Git, PRs and issues. Update, merge or delete existing guidance; do not require a new ADR for every architecture change or retain tombstones or an in-repository archive.

Supported compatibility and migration instructions remain current documentation while needed. Temporarily retained deprecated material must be clearly non-authoritative for new policy and carry a nearby removal TODO linked to an open issue with a concrete removal condition. One coherent task can cover multiple passages; the work remains outstanding until the material is removed. Remaining ADR consolidation is tracked in [#730](https://github.com/WillHirsch/yizkr/issues/730)–[#734](https://github.com/WillHirsch/yizkr/issues/734), and modeling-guide cleanup in [#735](https://github.com/WillHirsch/yizkr/issues/735).

Source evidence, curator audit history and meaningful regression inputs are current data with continuing purposes; this policy does not authorize deleting them. Approved target requirements must be distinguished from implemented capabilities with a limitation and implementation task.

Revisit a requirement when representative use cases or operational evidence reveal a mismatch. Record a provisional default and a concrete follow-up for unresolved questions, and block only work whose imminent decisions depend on them. The issue/branch/draft-PR/review/owner-merge workflow remains in [AGENT_PROTOCOL](AGENT_PROTOCOL.md); no additional universal planning gate is implied.
