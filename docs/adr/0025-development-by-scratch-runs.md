# ADR-0025: Disposable rebuilds and retained ingestion

Status: **accepted**. The owner-approved policy in #707 establishes rebuilds and retained ingestion as continuing modes. The [current contract](../CONTRACT.md) is authoritative for lifecycle and review policy; [TESTING.md](../TESTING.md) governs evidence, and the [agent protocol](../AGENT_PROTOCOL.md) governs execution and data access. This ADR records the rationale and the implementation boundary.

## Context

Whole-corpus rebuilds expose interactions between sources, identity decisions and downstream views. They are useful development evidence, but requiring extraction to stop changing before retaining data prevents the archive from improving incrementally. Conversely, an unattended development graph must not acquire the authority of human-reviewed evidence merely because it persists between commands.

## Decision

<a id="1-the-development-stage-develops-the-pipeline-by-whole-corpus-scratch-runs"></a>

### 1. Disposable rebuilds

`yizkr scratch run` implements an unattended corpus rebuild: per-document transcription, extraction and critique (or deterministic GEDCOM adaptation), then a corpus barrier, clustering, reset, registration, writing, place-container reconciliation and the private site. Images independently read from containers can join as excerpt documents. Failed documents remain visible in the report; only completed documents feed corpus stages. The [stage guide](../SCRATCH_WORKFLOW.md) describes the actual inputs and outputs.

Scratch writes carry unratified development provenance, have no human-approval stop and omit `depicts`. They are not human confirmations for identity, privacy or any other purpose. The output is disposable development material, not an authorized shared/public archive. Fix pipeline defects in the pipeline and regenerate the experiment; do not mistake a manual repair of its graph for a pipeline fix.

The implemented scratch write path and reset guards refuse an instance marked kept. A resumed scratch run can reuse or continue an intact recorded write phase; a new run or an invalidated instance state requires a reset. This is not an incremental retained writer. The runbook documents the [write-side guards](../runbooks/scratch-run.md#the-write-side).

### 2. Stage-level reuse

Document views persist separately from the model-response cache. The implemented reuse rules are in [ADR-0032](0032-output-reuse-at-the-engine-boundary.md) and [LLM_CACHE.md](../LLM_CACHE.md): content hashes track view inputs, while response keys track the calls actually sent to the engine. A new scratch run makes fresh document views and can reuse responses from earlier runs. Current views do not automatically invalidate for every prompt, model or code change; use the documented refresh/new-run controls.

Current document stages and corpus clustering read no live wiki. Their inputs are source material, recorded views and configuration. The contract also permits reconciliation/planning to consume an explicitly acquired, recorded graph snapshot or relevant projection, with relevant write preconditions checked before application. Source extraction must still derive source assertions from the source. The retained snapshot path and durable identity/review contracts are implementation work under #85, #107 and #130, following the [ingestion contract](../INGESTION_CONTRACT.md); they are not supplied by the present empty reconciliation step in `ingest run`.

The scratch write key and instance watermark guard reset/register/write reuse. The key currently includes whole batches, their process stamps and a broad package digest; narrowing its dependencies is separate work. Interrupted or outside writes can invalidate the watermark and require rebuilding. Keep those protections until a verified replacement exists.

### 3. Run feedback

Run manifests, stage events, control requests, anomalies, reports and diffs make completed, failed and reused work inspectable. The [runbook](../runbooks/scratch-run.md), [report guide](../RUN_REPORT.md), [diff guide](../RUN_DIFF.md) and [monitor guide](../MONITOR.md) describe the current commands.

Anomalies and model critique are advice, not human approval, source truth or permission to discard legitimate contradictions. Detailed outputs remain local-only; only approved aggregate summaries may be posted. Cancellation should preserve completed work, but current subprocess/worker cancellation and abandoned-run detection have limitations; #724 and #725 track those improvements.

### 4. Checking prompt, schema and model changes: `needs:scratch-diff`

Select evidence by the changed behavior and unresolved risk, following [TESTING.md](../TESTING.md). Focused fictional semantic checks, bounded model-output evaluation and real writer/query checks serve different purposes. A canned engine verifies orchestration, not model quality.

Whole-corpus evidence is required when relevant risk depends on cross-document interactions or remains materially uncertain after bounded evidence, for example broad identity/source-boundary changes or unexplained corpus regressions. Record the reason and affected stages. A changed pre-write file, prompt or model alone does not impose a whole-corpus run. Several related changes may share a run; stop before writes when graph effects are irrelevant. Preserve useful stage labels and reuse compatible model responses.

<a id="5-adr-0022-7-is-reaffirmed-and-extended"></a>

### 5. Complementary evaluation

A small semantic acceptance set and corpus review are complementary. Fictional cases can require preservation of conflicting sources, explicit uncertainty, reviewed identities and recoverable corrections without declaring one historical account true. Model evaluation can admit multiple acceptable readings or required abstention. The contract does not prohibit synthetic evaluation or make the corpus the only test set.

<a id="6-leaving-the-development-stage"></a>

### 6. Readiness for retained state

Retained ingestion preserves existing identities, source evidence and human decisions while adding or reprocessing sources. Admission requires scoped human authorization; ordinary assertions can be approved together, new identity equivalences must be explicit in reviewed assignments, and face associations require affirmative confirmation. Unchanged approvals carry forward. New corroborating evidence alone does not invalidate an approval; material changes to content, attribution, identity assignment or its basis reopen only affected decisions. Approval to retain does not authorize publication.

The owner authorizes the first retained dataset under #159 after readiness is demonstrated for supported workflows: reviewed admission, stable identity, safe retries, correction/recovery, privacy controls and workable backup/restore. No fixed number of unchanged runs or end to extraction development is required. Missing advanced operations can remain explicit refusals when the supported workflow is safe and useful.

The code already reads both the `Project:Kept state` page and the host's `kept.json` marker, and `core.instance.mark_kept` can write both. Reset refuses if either marks the instance kept or the necessary state cannot be established. These helpers do not constitute a complete keeper or incremental-ingestion workflow. Current writes through a single identity map must not be treated as support for evolving retained identities; #723 is the narrow incremental addition milestone.

<a id="7-development-stage-code-may-run-ahead-of-the-proposed-drafts"></a>

### 7. Implementation and readiness boundaries

The contract is agreed policy; future code is not assumed implemented. Shared stage APIs, freshness planning, durable identity/review state and provider-session separation remain tracked work. Existing approval, exclusion, provenance and reset guards stay in force until replacements are implemented and verified. Approving policy does not authorize a keeper run or a migration of real data.

### 8. Protocol consequences

Real writer/query checks use the [required fictional integration lane](../TESTING.md#live-integration-and-the-transition) in CI or through `make test-integration`. Its disposable instance uses repository fictional fixtures and stays separate from archive and corpus-run volumes, settings, instance records and locks. Additional centralized checks may provide evidence on an explicitly disposable target; never target retained data. A persistence-boundary change waits if its necessary evidence is unavailable, but unrelated code work need not wait.

An appropriately configured cloud session may use the fictional test environment under the execution controls. Corpus runs, real-data graphs and detailed corpus evidence remain local-only under [AGENT_PROTOCOL.md](../AGENT_PROTOCOL.md). Continue coordinating access to the current shared stack and model subscription.

## Consequences

Rebuilds remain valuable after data is retained. Scoped review preserves human work without requiring a click on every ordinary assertion, and durable recovery becomes a supported-workflow requirement rather than a promise to solve after convergence. A separate fictional integration target adds infrastructure cost but prevents retained data from blocking write-path development.
