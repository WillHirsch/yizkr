# Extraction, critique and identity proposals

Extraction turns a located source reading into proposed claims. It does not establish historical truth, persistent identity or human approval. The [contract](CONTRACT.md) governs those boundaries; [modeling conventions](MODELING.md) define vocabulary and evidence semantics. [Shared document operations](ARCHITECTURE.md#shared-document-stage-operations) serve both the whole-corpus driver and the [reviewed text-source workflow](TEXT_INGESTION.md).

## Entity-first and chunked extraction (ADR-0006, ADR-0007)

`pipeline.text_stages.extract(ExtractionRequest, client)` takes a `Transcription`, matching `ArtifactRef`, explicit strategy/settings and optional interpretation context. It borrows the caller's model client and has no graph client. Context is a separate declared input: it can explain a continuation across sources, but cannot supply another source's quotation as evidence owned by the target. Source ownership and page/placement locations come from [verified reading context](SOURCE_READING.md#pages-media-and-source-ownership), not model-invented identifiers. Structured genealogy exports use [deterministic GEDCOM derivation](GEDCOM.md).

| Stage | Process | Input → output |
| --- | --- | --- |
| Entity manifest | Model + validation | Numbered reading and vocabulary → document-local entities, mention records and optional preparation reading. |
| Packing | Deterministic | Extractable entities and mention lines → bounded assignments. |
| Claim extraction | Model, independently per chunk | Assigned subjects plus source context → claim/gap JSON objects and terminal sentinel. |
| Merge | Deterministic | Chunk answers and manifest → validated `ClaimBatch`, rejects and coverage diagnostics. |
| Critique | Model + validation | Batch, reading and the same interpretation context → findings and completion status. |
| Identity matching | Deterministic blocking + optional model | Source mentions and explicit corpus/graph context → identity proposals, separately reviewed before retained application. |

The shared API defaults to `chunked`; run ingestion explicitly uses it for its entity manifest. Standalone `ingest extract` defaults to `auto`, selecting chunked above 4,000 transcription characters and single-call otherwise. `single` has no entity-manifest pass. `file` validates prepared claims while retaining their citations. A verified container reading with no container-authored text can produce a complete empty batch deterministically, avoiding duplicate extraction of independently owned images.

## Entity manifest and fallback

The opening model turn sees the whole reading. Each `EntityRecord` carries a document-scoped ID, label, aliases, kind, summary, best-effort mention lines and an `extract` flag; it may also carry a structured mention record, judgment note and `type_hint`. [Names and mention records](MENTIONS_AND_MATCHING.md) describes these representations and their validation/cleanup limits. The manifest proposes alias unification and gives every chunk the same reference vocabulary. This makes cross-chunk references consistent, but cannot prove that the model found or distinguished every entity. Local IDs and labels are not durable identities.

Validation requires unique IDs, nonempty labels and supported entity kinds. Artifact kinds cannot be invented as manifest entities; a document mentioned by the source is distinct from a registered source artifact. Birth/death event kinds reserved for curation are dropped, and claims referencing them are rejected. Invalid mention structures or judgment notes are set aside and counted without discarding an otherwise usable entity; invalid place containers are checked separately. Out-of-range mention lines are removed. An empty or null entity list is accepted immediately as the model's claim-free reading, without another manifest attempt or fallback.

`extract: false` preserves an entity for audit while excluding it from extraction assignments. Current prompts apply this to logistics/boilerplate and to administrative participants in official records who have no other relevant role; the issuing authority and participants who are also kin or parties remain eligible. This is extraction scope, not a declaration that the source never mentions those entities.

An unusable manifest gets a second attempt. If an attempt answered but neither answer is usable, extraction falls back to model-proposed line ranges. Segmentation requires unique, nonoverlapping, in-bounds units, allows audited gaps and records deliberately skipped units. Two unusable segmentation answers lead to fixed line windows with empty contextual summaries. These degraded tiers lose the entity manifest and its cross-chunk reference guarantees. An engine outage with no answer fails the stage; a safety-classifier refusal stops the affected call without retry or fallback.

## Packing, context and validation

Chunking bounds expected output: dense claim JSON can greatly exceed the source text. Entities pack greedily in manifest order by the summed text of valid mention lines, with a count-based estimate when none is usable. Line-range units pack in document order. The default budget is 2,000 characters; a single oversized entity/unit occupies its own chunk, so this is a heuristic rather than a response-size guarantee. Independent calls can run concurrently under the caller's configured limit.

Each entity chunk is instructed to emit claims whose subjects it owns, using manifest IDs for references even when the object or evidence is elsewhere. Calls prefer independent forks of the accepted opening conversation. If no conversation exists when chunks are planned, they receive the document inline when it fits the 100,000-character threshold; the large-document route supplies assigned text and contextual summaries. A later fork failure uses its prebuilt standalone prompt, which can lack the full document. Rejected opening answers are never used as fork context. Provider restoration, replay and release belong to the [session service](ARCHITECTURE.md#provider-conversation-ownership).

Answers use separate JSON objects for claims and gaps, ending with `{"done": true}`. A missing sentinel or provider-reported output-limit stop is incomplete, regardless of parsable claims. Chunk retries default to two additional attempts. If every claim lacks the same required field, one separately keyed attempt names that field; a usable replacement is validated normally, otherwise the first answer remains for validation. Safety-classifier refusals are never retried. An unusable cached response is rejected so that it cannot masquerade as completed work; [cache semantics](LLM_CACHE.md) describe reuse and explicit resampling.

Merge normalizes manifest references to their declared labels and kinds and rejects unknown IDs. The line-range path prefixes local IDs by chunk instead. Exact duplicate claims collapse; differing assertions coexist. Per-claim schema/vocabulary validation salvages the valid subset and records rejects with reason codes. Mechanical shape repairs do not invent missing values: for example, an excerpt language can move from `source` into its excerpt, but an absent language is not guessed. Limited trailing-comma tolerance is counted.

Quotation checks and source-support binding are distinct. Without interpretation context, an excerpt absent from the reading remains an audit finding for critique. With other sources supplied as context, primary-evidence checks reject quotations belonging outside the target reading; unresolved attribution can make the result incomplete. A successful parse or sentinel is never proof of semantic completeness.

## Completion, diagnostics and reuse

`ExtractionResult` exposes either a complete proposed `batch` or a separate `diagnostic_batch` with incomplete reasons. `require_complete()` refuses diagnostics. Failed chunks, output truncation and unresolved required attribution cannot publish an accepted result. An extraction with no usable claims fails if chunk failures, unread content or validation loss explain the emptiness. A chunked extraction whose accepted answers genuinely say nothing was found can instead complete empty, with an attention reason. Single-call model extraction requires at least one valid claim.

Standalone extraction publishes `--out` only on completion. Incomplete salvage goes to `<out>.partial.json`; an earlier accepted output becomes a uniquely named stale file after failure/interruption. The shared stage leaves publication and recovery policy to its caller. Durable text products and saved run views remain independent of the response cache; reparsing compatible cached answers need not repeat model work. There is no per-chunk sidecar resume protocol.

For chunked extraction, an explicitly supplied state directory receives `coverage.json` and applicable `rejects.json`, `gaps.ndjson`, `containers.json`, `quotes.json` and `preparation.json`. Coverage records fallback reasons, validation/repair counts, mention-record health and entities without claims; line-range coverage instead reports unsegmented lines and units without matching subjects. These are advisory diagnostics, not an automatic re-extraction loop or a completeness certificate. Consult the [workflow product map](SCRATCH_WORKFLOW.md) for run paths; keep source-bearing diagnostics outside Git.

When preparation reading is enabled, the manifest also reads any statement that the document's own text was machine-assisted. A curator pointer must quote an actual passage; it directs the reading without deciding its meaning. Deterministic postprocessing removes model-supplied markers and adds `machine_assisted` only under the validated preparation reading, with the statement's separate source reference. A passage-limited statement marks only claims whose excerpts occur wholly within its scope; unlocated/straddling cases remain unmarked and visible. No marker is inferred from writing style or inherited by independently sourced embedded material.

## Schema gaps and critique (ADR-0013)

### A. Unclassified entities

`unclassified_entity` is a holding kind for a mentioned entity no modeled kind fits. The prompt asks for an English `type_hint`; existing modeled kinds take precedence. This proposal can reach the graph with source/process provenance through the applicable writer and review route. The [unclassified-entities query](../src/yizkr/queries/unclassified-entities.rq) groups nondeprecated hints for vocabulary review. It does not reclassify entities automatically.

### B. Relation gaps

Chunked extraction also accepts gap objects containing `subject`, free-text `predicate`, `object` and quoted `evidence`. These become `gaps.ndjson` and curator-facing CSV, never graph relations without a vocabulary property. `yizkr gaps rollup [<run>]` groups logged predicates using Unicode/case/whitespace/hyphen/underscore normalization and records counts, variants and example evidence in `gaps-rollup.csv`. Its `decision` and `notes` columns are for inspection; nothing applies them. Missing facts are not guaranteed to become gaps: this channel records what the model actually reports.

### C. Critique

The critic reads the manifest, claims, whole numbered transcription and located citations. It flags kind/alias mistakes, misapplied properties and unsupported readings without rewriting the batch. Findings identify an entity and optionally a claim, alias or container, with an axis (`subject`, `predicate`, `value`, `existence`) and validated category. Invalid findings remain verbatim with validation reasons. Findings appear in review notes and reports; they supply neither corrections nor approval.

An unfinished critic retains available findings and an explicit incomplete status. The reviewed text-source workflow requires completion before registration/review, while a completed critic's warnings remain advisory. Critic silence is not evidence that all claims are correct, and an empty batch needs no critic model call.

## Machine uncertainty and judgment (ADR-0023)

Source uncertainty and machine uncertainty mean different things. Preserve the source's precision, explicit ranges and circa wording in the assertion. Do not narrow a date beyond the source, or round it to disguise uncertainty about the reading. An ambiguous interpretation must not become an apparently resolved value or falsely coarser source precision. See [time modeling](MODELING.md#uncertainty-and-time) for supported values and bounds.

Optional claim confidence is review metadata, not graph confidence or approval. An entity or claim can also carry a judgment note: a category (`kind`, `relation`, `date_reading`, `identity`, `other`) and a sentence explaining a call a careful reader could make differently. Notes describe the extractor's interpretation; they are not calibrated probabilities, source uncertainty or a substitute for evidence. Absence of a note establishes no certainty.

The claims writer can store a note as `judgement` and `judgement_note` in the extraction-run reference block, separate from the historical source reference. An entity note accompanies its sourced kind statement. Notes do not choose the claim's value or rank and confer no consent. [The reference pattern](MODELING.md#reference-pattern-see-adr-0003) describes encoding and refusal limits. Critic findings remain separate advisory products, not automatically written corrections.

Genuine disagreement may occur within one source as well as between sources. Reviewed machine alternatives can remain labeled interpretations, but are not independent witnesses. The [disagreement report](MODELING.md#contradictions) currently compares different artifacts; that view scope is not a prohibition on preserving same-source evidence. The [under-witnessed query](../src/yizkr/queries/README.md#queries) is a structural review queue based on source targets and run references, not a measure of truth, actual scrutiny or independent corroboration. Human identity confirmation has its own [durable provenance](IDENTITY_REVIEW.md).

## Reconciliation and authority

Graph matching blocks candidates by kind and shared whole-name forms. [Candidate generation and structural exclusions](MENTIONS_AND_MATCHING.md#matching-candidates-and-structural-exclusions-adr-0017) describes the name keys, supplied-fact limitations and distinction from corpus clustering. Birth-date disagreement remains evidence for review, not an automatic graph-candidate exclusion. Adjudication sees explicit candidates and relational evidence rather than searching the graph itself.

Use [recorded graph matching and durable review](IDENTITY_REVIEW.md#record-graph-context-and-inspect-later-cohort-matches) for replayable later-cohort proposals. A saved projection or an empty candidate list grants no creation permission. Retained assignment and historical-claim application recheck current state and require separate scoped approvals. Labels and cached model verdicts supply neither authority.

The standalone `ingest reconcile`/`resolve --equivalences` path remains available for one-batch CSV proposals (`same`, `uncertain`, `distinct`). Only explicitly approved rows map local references; other references remain unresolved or use the supplied identity map. It has no automatic migration into durable consent. `reconcile-graph` exports duplicate candidates but executes no merge. Neither command establishes a supported retained correction/merge/split workflow; see [the integration boundary](IDENTITY_REVIEW.md#integration-boundary).

Corpus clustering instead compares source mentions across batches. Confident clusters and rechecked confident components of ambiguous clusters enter the proposal map. Optional adjudication asks unresolved pairs for `same`, `different` or `uncertain`; conflicting clusters never merge. In disposable operation, settled verdicts can join components, but cannot override structural contradictions; `same` excuses only a date conflict on its own pair. Event principals and union members held apart also constrain their events/unions. Uncertain or missing verdicts leave the existing confident components and remaining singletons. Methods remain distinct in provenance. These map results are unratified proposals, not durable human decisions; see [corpus clustering](../src/yizkr/pipeline/README.md#corpus-clustering-clusteringpy-the-scratch-runs-identity-map).

Implementation: [chunking](../src/yizkr/pipeline/chunking.py), [extraction validation](../src/yizkr/pipeline/extract.py), [preparation](../src/yizkr/pipeline/preparation.py), [critique](../src/yizkr/pipeline/critique.py), [gap rollup](../src/yizkr/pipeline/gaps.py), [graph reconciliation](../src/yizkr/pipeline/reconcile.py), [clustering](../src/yizkr/pipeline/clustering.py) and [adjudication](../src/yizkr/pipeline/adjudication.py).
